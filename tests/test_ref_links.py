"""能力令牌（临时公开链接）—— 这是**全站唯一一个免登录取数据**的东西。

为什么测得这么细：它把"用户自己的照片"开到了公网上，而三种坏法都**没有症状**——
  · 令牌不够随机 → 被枚举，照片被人扒走，服务器日志里什么都没有；
  · 过期没生效 → 链接永久有效，用户以为早失效了；
  · 撤销失败被静默吞掉 → 照片长期挂在外面。
前两条靠断言钉；第三条靠"撤销抛异常不许反噬任务"钉（在 test_video_skill 里）。
"""
from __future__ import annotations

import time

import pytest

from station import config, db
from station.files import share


@pytest.fixture
def no_public_base(monkeypatch):
    """把"没配对外地址"这件事造准：连 .env 都不读（否则本机 .env 里配了就会漏测）。"""
    monkeypatch.setattr(config, "load_env", lambda: {})
    monkeypatch.delenv("STATION_PUBLIC_BASE", raising=False)


@pytest.fixture
def with_public_base(monkeypatch):
    monkeypatch.setenv("STATION_PUBLIC_BASE", "https://station.example.com")


def _fid(data=b"PHOTO-BYTES", name="me.jpg"):
    from station.files import store as fs
    return fs.save_bytes(data, ".jpg", name=name, owner="u1", skill_id="video")


# ── db 层：签发 / 解析 / 过期 / 撤销 ─────────────────────────────────

def test_ref_link_roundtrip():
    db.ref_link_add("tok-1", "f" * 40, "u1", time.time() + 60)
    assert db.ref_link_resolve("tok-1") == "f" * 40
    assert db.ref_link_resolve("没这个") == ""
    assert db.ref_link_resolve("") == ""


def test_expired_link_does_not_resolve(with_public_base):
    """★ 过期判断在 SQL 里 —— 所以这条在 **db 层**测。

    直接往表里插一条已经过期的行（不经 share），断言的正是"SQL 那句 expires > ?"
    真的在起作用。放到端点层测的话，测的是"端点会不会判过期"，偏了一层。
    """
    db.ref_link_add("tok-old", "a" * 40, "u1", time.time() - 1)
    assert db.ref_link_resolve("tok-old") == ""


def test_revoke_removes_and_is_idempotent():
    db.ref_link_add("tok-a", "a" * 40, "u1", time.time() + 60)
    db.ref_link_add("tok-b", "b" * 40, "u1", time.time() + 60)
    assert db.ref_link_revoke(["tok-a"]) == 1
    assert db.ref_link_resolve("tok-a") == ""
    assert db.ref_link_resolve("tok-b") == "b" * 40       # 只撤指定的那条
    assert db.ref_link_revoke(["tok-a"]) == 0             # 再撤一次不抛
    assert db.ref_link_revoke([]) == 0
    assert db.ref_link_revoke(None) == 0


def test_sweep_clears_only_expired():
    db.ref_link_add("fresh", "a" * 40, "u1", time.time() + 60)
    db.ref_link_add("stale", "b" * 40, "u1", time.time() - 1)
    assert db.ref_link_sweep() == 1
    assert db.ref_link_resolve("fresh") == "a" * 40


# ── share 层：签发条件与令牌强度 ─────────────────────────────────────

def test_tokens_are_unique_and_long(with_public_base):
    """★ 令牌必须**不可猜**：长度够、且两次签发不同。

    这条防的是"哪天有人图省事换成 uuid4().hex[:8] 甚至自增 id" —— 那种改动不报错，
    只是把"不可猜"悄悄降级成"可以撞"。
    """
    fid = _fid()
    a = share.make_ref_link(fid, "u1")
    b = share.make_ref_link(fid, "u1")
    assert a != b
    assert len(a) >= 32, f"令牌太短（{len(a)}），不够不可猜"


def test_make_ref_link_refuses_without_public_base(no_public_base):
    """★ 没配对外地址 → 抛人话，**且表里不留半成品行**。"""
    fid = _fid()
    with pytest.raises(ValueError) as e:
        share.make_ref_link(fid, "u1")
    assert "STATION_PUBLIC_BASE" in str(e.value), "要告诉运维去配哪个变量"
    # 关键的一半：**拒绝的时候不该有任何一行留下来**（先校验再落库）。
    # 只断言"抛了错"是不够的 —— 先落库再抛错同样是抛错，但会在表里积一堆废令牌。
    n = db.conn().execute("SELECT COUNT(*) AS c FROM ref_links").fetchone()["c"]
    assert n == 0, "被拒绝的签发不该在表里留下行"


@pytest.mark.parametrize("base", [
    "http://127.0.0.1:8001", "http://localhost:8001", "http://0.0.0.0:8001",
    "http://192.168.1.5:8001", "http://10.0.0.7:8001", "http://172.16.3.4:8001",
    "ftp://example.com", "example.com", "",
])
def test_private_or_bogus_public_base_is_rejected(monkeypatch, no_public_base, base):
    """★ 回环/内网/非法地址**一律当作"没配"**。

    为什么不放行：这类地址对端**根本抓不到**，配上去看着像"我配了"，
    实际表现是"生成出来的视频里没有这个人" —— 那比不配更难查。
    """
    if base:
        monkeypatch.setenv("STATION_PUBLIC_BASE", base)
    assert config.public_base() == "", f"{base!r} 不该被当成合法的对外地址"


def test_public_base_accepts_real_address(monkeypatch):
    """用**主机名**当例子（`.example.com` 是保留给文档用的）。★ 别改成真实 IP ——
    这个测试文件会进公开仓库，写真实地址等于把它公开（09-14 真踩过）。

    顺带一个容易踩的点：想拿 RFC 5737 的文档保留段（203.0.113.0/24 之类）表示
    "一个合法公网地址"是**不行的** —— Python 的 ipaddress 把那三段也算进 `is_private`
    （实测 `IPv4Address("203.0.113.10").is_private` 是 True），会被这条校验拒掉。
    真正被覆盖的"公网 IP 该放行"由下面那个负例表反证（内网那几个全被拒）。
    """
    monkeypatch.setenv("STATION_PUBLIC_BASE", "https://station.example.com:8443")
    assert config.public_base() == "https://station.example.com:8443"
    monkeypatch.setenv("STATION_PUBLIC_BASE", "https://my-host.example.com/")
    assert config.public_base() == "https://my-host.example.com"     # 去掉末尾斜杠


def test_ref_url_joins_cleanly(with_public_base):
    token = share.make_ref_link(_fid(), "u1")
    url = share.ref_url(token)
    assert url == f"https://station.example.com/api/ref/{token}"


def test_revoke_never_raises(monkeypatch):
    """★ 撤销是善后，**永远不许抛** —— 调用方是在任务成功之后才来撤的，
    这里抛出去会把一个已经生成好的任务判成失败。"""
    def _boom(*a, **k):
        raise RuntimeError("模拟数据库抽了")
    monkeypatch.setattr(db, "ref_link_revoke", _boom)
    assert share.revoke(["whatever"]) == 0


# ── 端点：免登录取图 ─────────────────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from station.app.server import app
    return TestClient(app)


def test_ref_endpoint_serves_without_login(with_public_base):
    """★★ 这条是"免登录"的**唯一真钉子**。

    哪天有人顺手在它前面加一句 require_user，这条立刻红 —— 而那个改动看起来完全合理
    （"怎么有个端点没鉴权？"），在 review 里极容易被放过。
    """
    from station.files import store as fs
    fid = fs.save_bytes(b"PHOTO", ".jpg", name="me.jpg", owner="u1")
    token = share.make_ref_link(fid, "u1")

    c = _client()                       # ★ 全新的 client：没有任何登录 cookie
    r = c.get(f"/api/ref/{token}")
    assert r.status_code == 200
    assert r.content == b"PHOTO"
    assert r.headers["content-type"].startswith("image/jpeg")


def test_ref_endpoint_404_for_unknown_and_expired(with_public_base):
    """未知/已过期**一律 404**（不要 403/410 区分 —— 那是给枚举者的信号）。"""
    c = _client()
    assert c.get("/api/ref/随便编一个").status_code == 404
    db.ref_link_add("expired-tok", "a" * 40, "u1", time.time() - 1)
    r = c.get("/api/ref/expired-tok")
    assert r.status_code == 404
    assert b"a" * 40 not in r.content          # 连内容都不该漏


def test_ref_endpoint_404_when_file_gone(with_public_base):
    """表里有行、但文件被删了 → 404 而不是 500（`FileNotFoundError` 不是 `ValueError`，
    原来的 except 接不住 —— 这条坑 conventions 里记过）。"""
    from station.files import store as fs
    fid = fs.save_bytes(b"PHOTO", ".jpg", owner="u1")
    token = share.make_ref_link(fid, "u1")
    p = fs.path(fid)
    p.unlink()
    (p.parent / (fid + ".json")).unlink()
    assert _client().get(f"/api/ref/{token}").status_code == 404


def test_ref_endpoint_rejects_malformed_fid(with_public_base):
    """★ 表里要是被写脏了（fid 不是正经 sha1），**绝不能**拿着它去拼路径。

    `fs.path()` 是拿 fid 直接拼文件名的 —— 不校验就是一个路径穿越。
    """
    db.ref_link_add("dirty", "../../../etc/passwd", "u1", time.time() + 60)
    assert _client().get("/api/ref/dirty").status_code == 404


def test_ref_endpoint_sets_private_headers(with_public_base):
    """响应头三件套：不许缓存、不许被搜索引擎索引、**不带原始文件名**。"""
    from station.files import store as fs
    fid = fs.save_bytes(b"PHOTO", ".jpg", name="张三的身份证照.jpg", owner="u1")
    token = share.make_ref_link(fid, "u1")
    r = _client().get(f"/api/ref/{token}")
    assert "no-store" in r.headers.get("cache-control", "")
    assert "noindex" in r.headers.get("x-robots-tag", "")
    assert "张三" not in r.headers.get("content-disposition", ""), \
        "文件名里可能带真名，公开链接不该把它回出去"
