"""station.config —— 宿主路径/开关/密钥加载（零依赖 .env 读取）。

- 仓库根（repo）由本文件位置推导；data 一律落在 <repo>/data/station/（不入库）。
- 密钥读 <repo>/src/.env（GLM/QWEN/DEEPSEEK_API_KEY 等），与旧 archive 共用一份。

新手视角：所有“程序该把东西放哪、用哪把钥匙、开关是什么”都集中在 config。
  - REPO / DATA_DIR：不用写死路径，代码知道自己在哪个仓库 → 数据放 data/station。
  - load_env()：把 src/.env 里的 KEY=xxx 读进环境变量（密钥不进代码/不进 git 的标准做法）。
  - sub("sessions")：取 data/station/sessions 目录，不存在会自动建。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 路径推导 ---------------------------------------------------------
# __file__ = .../src/station/config.py
# .resolve() 拿到绝对路径；parents[0]=station, [1]=src, [2]=仓库根(workforda)
REPO = Path(__file__).resolve().parents[2]          # 仓库根
SRC = REPO / "src"                                   # 源码目录（.env 在这）
SKILLS_DIR = REPO / "skills"                          # 能力开放目录（仓库根，放所有技能）
DATA_DIR = REPO / "data" / "station"                  # 运行时数据（已在 .gitignore 里，不入库）

# ---- 默认模型通道 -----------------------------------------------------
# 文本/text 默认 glm，视觉/vision 默认 qwen（沿用 archive 口径；可被 .env 覆盖）
# video 是 09-14 加的第四类：目前只有 Agnes AI 一家提供，所以默认就是它。
# ★ 加这条不只是"多个默认值"——channel_for() 是直接 DEFAULT_CHANNEL[kind] 取值的，
#   少一个键就是 KeyError（modelcfg._default_slots 会在零配置时踩它）。
DEFAULT_CHANNEL = {"text": "glm", "vision": "qwen", "video": "agnes"}

# ---- 宿主开关（也能用环境变量临时覆盖，见 .env.example）----------------
MAX_STEPS = int(os.environ.get("STATION_MAX_STEPS", "10"))   # agent 一轮最多工具步数
AUTO_APPROVE = os.environ.get("STATION_AUTO_APPROVE", "0") == "1"  # 危险工具免确认(仅调试)
COMPACT_TOKENS = int(os.environ.get("STATION_COMPACT_TOKENS", "0"))  # >0 才启用超长自动压缩
ROUTE_CHANNEL = os.environ.get("ROUTE_CHANNEL", "qwen-flash")        # L2 意图识别小模型通道
ROUTE_THRESHOLD = float(os.environ.get("ROUTE_THRESHOLD", "0.8"))    # L2 置信度门（不够就降 L3）
ROUTE_TIMEOUT = float(os.environ.get("ROUTE_TIMEOUT", "10"))         # L2 判词**墙钟**预算（秒）：超了降 L3，别让用户干等
# ★ 09-11 从 1.5 改成 10：1.5 秒是**冷启动都不够**的预算 —— 实测进程内第一次调用要
#   2.7~3.4 秒（TLS/建连/对端冷路径），于是判词**每次都超时、每次都白等**，等于 L2
#   从来没生效过，还白搭一段等待。给到 10 秒让它真有机会答；热了之后它其实只要
#   ~350ms，10 秒只是个上限。等待期间前端有"正在理解…"的等待态（见 static/index.html），
#   不会让人以为卡死。这个值可以用 ROUTE_TIMEOUT 环境变量覆盖。
ROUTE_LOG = os.environ.get("ROUTE_LOG", "1") == "1"                  # 1=每次路由判定写 data/station/route_log.jsonl（校准阈值用）


def load_env() -> dict:
    """把 .env 注入 os.environ（不覆盖已存在的同名变量）。返回这次读到了哪些键值。

    手动实现的一个极简 .env 解析器（不引第三方库）。
    查找顺序：仓库 src/.env → 仓库根 .env → 当前目录 .env（第一个命中的生效）。
    """
    candidates = [SRC / ".env", REPO / ".env", Path(os.getcwd()) / ".env"]
    loaded: dict = {}
    for path in candidates:
        if not path.is_file():
            continue                       # 不存在就试下一个
        # read_text 读整份；splitlines 按行拆；utf-8-sig 兼容带 BOM 的文件
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()            # 去首尾空格
            # 跳过空行 / 以 # 开头的注释行 / 没有等号的行
            if not line or line.startswith("#") or "=" not in line:
                continue
            # 按第一个 "=" 拆成 键=值；partition 比 split 安全（值里若含 = 不误伤）
            k, _, v = line.partition("=")
            # 去空格 + 去包裹值的引号（'x' 或 "x" 都行）
            k, v = k.strip(), v.strip().strip('"').strip("'")
            # 环境里已有同名的就不覆盖（让 shell 里的环境变量优先于 .env）
            if k and v and k not in os.environ:
                os.environ[k] = v
                loaded[k] = v
    return loaded


def _is_private_host(host: str) -> bool:
    """这个主机名是不是"对端根本抓不到"的回环/内网地址。

    域名一律放行（localhost 除外）—— 我们没法也不该去解析域名判断它指向哪。
    """
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", "0.0.0.0"):
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False                        # 是域名，放行
    return bool(ip.is_private or ip.is_loopback
                or ip.is_link_local or ip.is_reserved or ip.is_unspecified)


def public_base() -> str:
    """本机**对外**的基址（如 `https://your-host:8443`）；没配或不合法返回 ""。

    为什么需要它：有些能力要把一个 URL 交给**外部服务**去抓（视频生成的人像参考图就是
    —— 对端要求"可由 Agnes 服务公开访问的链接"）。而**服务器自己不知道自己在外面叫什么**：
    容器里绑的是 `0.0.0.0`、本机开发是 `127.0.0.1`，拿它们拼出来的 URL 对端一辈子也抓不到。
    所以只能由部署的人配（`STATION_PUBLIC_BASE`）。

    ★★ 必须写成**函数**而不是模块级常量：`load_env()` 是在 `main()` 里才被调用的，
      写成常量的话 `src/.env` 里配的值会被**静默忽略**（`STATION_MAX_STEPS` 已经踩过这个坑）。
    ★ 返回 "" 的两种情形**一视同仁**（没配 / 配成了回环内网）：对端抓不到就是抓不到，
      调用方据此**提前拒绝**，而不是等生成了半天才失败。

    ★ 校验里**含 IP 私网判定**：`127.*` / `10.*` / `192.168.*` / `172.16-31.*` / `::1`
      这些配上去看着像"我配了"，其实对端一次都抓不到 —— 那比不配更难查。
    """
    load_env()
    u = (os.environ.get("STATION_PUBLIC_BASE") or "").strip().rstrip("/")
    if not (u.startswith("http://") or u.startswith("https://")):
        return ""
    host = u.split("//", 1)[1].split("/", 1)[0].split(":")[0]
    if not host or _is_private_host(host):
        return ""
    return u


def public_base_hint() -> str:
    """没配好时给用户/运维看的那句话（说清在哪配、为什么需要）。"""
    return ("本机还没配**对外地址**（环境变量 STATION_PUBLIC_BASE），"
            "所以没法把照片交给视频服务 —— 它需要一个从公网能访问到的链接。"
            "部署时在 docker-compose.yml 里配成 `https://<你的域名或公网IP>:<端口>`；"
            "本机开发时对端抓不到 localhost，这一项功能用不了。")


def channel_for(kind: str) -> str:
    """kind=text|vision → 返回用哪个通道名。

    优先读 env（STATION_TEXT_CHANNEL 或老的 TEXT_CHANNEL），都没有才用默认。
    这样不用改代码就能临时换模型通道。
    """
    name = os.environ.get(f"STATION_{kind.upper()}_CHANNEL") or os.environ.get(
        f"{kind.upper()}_CHANNEL")
    return name or DEFAULT_CHANNEL[kind]


def sub(*parts: str) -> Path:
    """取 data 子目录并确保存在，如 sub('sessions')、sub('skills', id)。

    每次调用都确保目录在（mkdir parents=True），拿到的 Path 一定能用。
    """
    p = DATA_DIR.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p
