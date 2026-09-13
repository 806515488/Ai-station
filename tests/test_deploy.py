"""部署配置的离线单测 —— 不联网、不起服务、不烧 key。

新手视角（Java 朋友版）：这一组测的不是业务逻辑，而是**部署时最容易静默出错的两处**：

  ① **容器里绑错地址** —— 绑 `127.0.0.1` 的话只有容器自己连得上，外面一律连不进来。
     现象是"服务明明起来了、docker ps 也是 Up，但浏览器打不开"，且**没有任何报错**。
  ② **忘了挂 data 卷** —— 容器一删，用户的档案（SQLite + 上传的照片）全没，而且
     容器照常起、界面照常开，只是里面是空的。

这两条的共同点是：**写错了不报错**。所以只能靠把配置本身断言住（跟 `test_gtools.py`
里那条"别把文件工具加回来"、`test_station_core.py` 里那条"每个 db 函数都要拿锁"同源）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


# ── uvicorn 起站参数：环境变量怎么翻 ────────────────────────────

@pytest.fixture()
def clean_env(monkeypatch):
    """清掉这几个变量，保证测的是"默认值"而不是跑测试的机器上碰巧设了什么。"""
    for k in ("STATION_HOST", "STATION_PORT",
              "STATION_SSL_KEYFILE", "STATION_SSL_CERTFILE"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_are_unchanged_for_local_dev(clean_env):
    """★ 默认必须是 127.0.0.1:8001 且不开 TLS —— 本机开发用法一个字都不能变。

    （本机跑 `station-web` 时靠的就是这组默认值；哪天被改成 0.0.0.0，等于把开发机
      上的工作站暴露到局域网里。）
    """
    from station.app.server import _serve_options
    assert _serve_options() == {"host": "127.0.0.1", "port": 8001}


def test_env_overrides_host_and_port(clean_env, monkeypatch):
    from station.app.server import _serve_options
    monkeypatch.setenv("STATION_HOST", "0.0.0.0")
    monkeypatch.setenv("STATION_PORT", "8443")
    assert _serve_options() == {"host": "0.0.0.0", "port": 8443}


def test_tls_only_when_both_files_given(clean_env, monkeypatch):
    from station.app.server import _serve_options
    monkeypatch.setenv("STATION_SSL_KEYFILE", "/certs/key.pem")
    monkeypatch.setenv("STATION_SSL_CERTFILE", "/certs/cert.pem")
    opts = _serve_options()
    assert opts["ssl_keyfile"] == "/certs/key.pem"
    assert opts["ssl_certfile"] == "/certs/cert.pem"


def test_half_configured_tls_still_starts_but_says_so(clean_env, monkeypatch, capsys):
    """★ 只配了一半时必须**照常起站 + 出声告警**，不许静默降级也不许崩。

    静默降级危险在哪：用户以为自己走的是 HTTPS，其实全程明文（账号密码都在里面）。
    崩掉也不好：配错一个变量就完全没服务，而且得看日志才知道为什么。
    """
    from station.app.server import _serve_options
    monkeypatch.setenv("STATION_SSL_KEYFILE", "/certs/key.pem")     # 只给 key，不给 cert
    opts = _serve_options()
    assert "ssl_keyfile" not in opts and "ssl_certfile" not in opts  # 不开 TLS
    assert "STATION_SSL_CERTFILE" in capsys.readouterr().out         # 但要说清缺哪个


# ── compose / Dockerfile 的"写错不报错"项 ───────────────────────

def test_compose_mounts_the_data_volume():
    """★★ 这条是整个部署里最贵的一行：不给 data/ 挂卷 = 容器一删档案全没。

    而且是**静默**的 —— 容器照常起、界面照常开，只是里面空的。所以断言它。
    """
    txt = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/app/data" in txt, "compose 没有把 data/ 挂出来 —— 用户数据会随容器消失"
    # 挂的要是宿主机的真目录（容器内路径右边、宿主路径左边，中间有冒号）
    assert "/srv/station/data:/app/data" in txt


def test_compose_binds_all_interfaces():
    """★ 容器里必须绑 0.0.0.0，否则端口映射过去也没人应答。"""
    txt = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'STATION_HOST: "0.0.0.0"' in txt
    assert ":8443" in txt


def test_dockerignore_keeps_real_archives_out_of_the_image():
    """★ 真档案绝不能进镜像：`data/`（运行时状态）和 `samples/`（李明卷真样本）。

    镜像是会被推来推去的东西，而且**镜像层删不干净** —— 一旦烤进去就等于数据流出去了。
    这两个目录本来也都被 .gitignore 挡着（不在 gitee 上），这里是第二道。
    """
    txt = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in txt.splitlines()]
    assert "data/" in lines, "data/ 没被 .dockerignore 排除 —— 真档案会进镜像"
    assert "samples/" in lines, "samples/ 没被 .dockerignore 排除 —— 真档案会进镜像"


def test_dockerfile_exists_and_uses_the_console_script():
    """镜像入口用的是 console script（`station-web`），它**不走 __main__ 块** ——
    所以 load_env() 必须在 main() 里面，放 __main__ 块里等于漏读 .env。
    """
    txt = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "station-web" in txt
    src = (_ROOT / "src" / "station" / "app" / "server.py").read_text(encoding="utf-8")
    main_body = src.split("def main()", 1)[1].split("\ndef ", 1)[0]
    assert "config.load_env()" in main_body, "load_env 不在 main() 里，station-web 入口会漏读 .env"
