"""用户系统 + SQLite 持久化离线单测。

新手视角（Java 朋友版）：锁住"多用户"这件事的几条核心契约——
  1) 注册/登录：重名拒绝、密码错 401 语义（这里用 ValueError/None 表达）
  2) 数据隔离：A 的会话/任务，B 查不到也拿不到
  3) 持久化：Thread/Job 写进 SQLite 后，"重启"（重新 load）数据还在
  4) 确认门任务带 user_id 落库
全部离线（conftest 已把 DB 指到临时目录）。
"""
from __future__ import annotations

import pytest

from station import db
from station.core.session import Thread
from station.jobs.manager import Job


def test_user_register_and_login():
    """注册成功/重名拒绝；登录对给用户错给 None。"""
    u = db.create_user("alice", "pw1")
    assert u["name"] == "alice" and u["id"]
    with pytest.raises(ValueError):
        db.create_user("alice", "pw2")               # 重名
    assert db.check_login("alice", "pw1")["id"] == u["id"]
    assert db.check_login("alice", "bad") is None    # 密码错
    assert db.check_login("nobody", "pw") is None    # 没此人


def test_user_isolation_threads():
    """A 的会话 B 看不见（thread_list 按用户过滤）。"""
    a = db.create_user("a1", "p"); b = db.create_user("b1", "p")
    t = Thread(skill_id="weekly-report")
    t.user_id = a["id"]
    t.add_user("hello")
    t.save()
    la = db.thread_list(a["id"]); lb = db.thread_list(b["id"])
    assert any(x["id"] == t.id for x in la)
    assert not any(x["id"] == t.id for x in lb)


def test_thread_persist_roundtrip():
    """会话写库→重新 load，消息与 pending 挂起态都在（刷新不失忆的根基）。"""
    t = Thread(skill_id="demo-agent")
    t.user_id = db.create_user("u1", "p")["id"]
    t.add_user("问题")
    t.set_pending("demo.fake_risky", {"x": 1})
    t.save()
    t2 = Thread.load(t.id)
    assert t2 is not None
    assert t2.msgs[0]["content"] == "问题"
    assert t2.pending and t2.pending["tool"] == "demo.fake_risky"


def test_job_user_and_roundtrip():
    """任务带 user_id 落库；附加结果（result）重启后还在。"""
    a = db.create_user("ja", "p"); b = db.create_user("jb", "p")
    j = Job("archive", {"project": "/tmp/x"})
    j.user_id = a["id"]
    j.result = {"project": "/p/project.json", "person": "丁某", "materials": [], "issues": []}
    j.update(status="done", message="识别完成")
    j2 = Job.load(j.id)
    assert j2 is not None and j2.status == "done"
    assert j2.user_id == a["id"]
    assert j2.result["person"] == "丁某"             # 附加结果（工具层可能用）
    # 归属查得到：job_load 回来的是同一个人（列表查询已随 /api/jobs 端点删掉）
    assert Job.load(j.id).user_id == a["id"] != b["id"]


def test_every_db_accessor_holds_the_lock():
    """★ 结构性护栏：db.py 里**每个摸连接**的函数都必须拿 `_lock`（09-11 修的真 bug）。

    背景：全站共用一个 sqlite3 连接。写函数一直有锁，**读函数全都没拿** → 聊天流在写
    （`thread.save()` 每个事件一次）的同时前端在轮询 `/api/sessions`，就会撞出
    `sqlite3.InterfaceError: bad parameter / no more rows available`，**而且读到的行是
    错乱的**（json.loads 收到 None、行元组长度不对）—— 用户看到的是"聊完天左边历史
    整个没了"。实测：修前压在 1.5 秒里撞出 **783 次异常**，修后 0 次。

    为什么查源码而不是跑并发：并发压测是**概率性**的——我试过 2 写 2 读压 0.5 秒，
    把锁摘掉它照样绿（要 3 写 3 读压 1.5 秒才稳定撞出来）。那种测试快的时候绿、
    机器慢的时候红，等于没有。这里直接断言"有没有拿锁"，确定性、永远不含糊。

    别用 "我以为这条路径不会并发" 来绕过它：只要同时有两个请求在跑，就有并发。
    """
    import ast
    from pathlib import Path

    src = Path(db.__file__).read_text(encoding="utf-8")
    offenders = []
    for node in ast.parse(src).body:
        if not isinstance(node, ast.FunctionDef) or node.name == "conn":
            continue
        body = ast.get_source_segment(src, node)
        if "conn(" in body and "with _lock" not in body:
            offenders.append(f"{node.name}(db.py:{node.lineno})")
    assert not offenders, (
        "这些函数摸了 sqlite 连接却没拿 _lock —— 多线程下会读到错乱的行、"
        "表现为数据凭空消失：" + "、".join(offenders))

    # ★ 第二半：db.py **之外**任何地方都不许直接碰连接。
    #   只查 db.py 内部是不够的 —— 曾经 server.py 的 _owned_project 里有一句
    #   裸的 `db.conn().execute(...)`，每个缩略图请求都过它，于是全景卡一次拉上百张
    #   图时和聊天流的写入抢同一条连接 → 部分图拿到 401/500 → **一片破图**。
    #   要用连接就调 db.py 里带锁的函数（如 archive_state_owner）。
    #   用 AST 扫，不扫裸文本：注释里提到 `conn()` 不该被误判（我自己就踩过）。
    repo = Path(db.__file__).resolve().parents[2]
    outside = []
    for py in list(repo.glob("src/**/*.py")) + list(repo.glob("skills/**/*.py")):
        if py.resolve() == Path(db.__file__).resolve():
            continue
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = (f.id if isinstance(f, ast.Name)
                    else f.attr if isinstance(f, ast.Attribute) else "")
            if name == "conn":                      # db.conn() / conn() 都算
                outside.append(f"{py.relative_to(repo)}:{node.lineno}")
    assert not outside, (
        "db.py 之外不该直接使用 sqlite 连接（绕开 _lock 会重现'破图/数据消失'）："
        + "、".join(outside))


def test_archive_state_checkpoint_db():
    """现场/账本/检查点全走 DB：保存→改→检查点→恢复；恢复前自动再存档。"""
    a = db.create_user("ca", "p")
    proj = "/fake/project.json"
    state1 = {"person": "丁", "materials": [{"uid": "m1", "category": "三"}],
              "issues": []}
    db.archive_state_save(proj, "jobx", a["id"], "丁", state1)
    # 改后新现场
    state2 = {"person": "丁", "materials": [{"uid": "m1", "category": "九-2"}],
              "issues": []}
    db.archive_state_save(proj, "jobx", a["id"], "丁", state2)
    assert db.archive_state_get(proj)["materials"][0]["category"] == "九-2"
    # 检查点：存 state1 → 恢复逻辑（restore_checkpoint 在 interactive 层，这里锁 DB 语义）
    ck = db.checkpoint_add(proj, a["id"], "改类前", state1)
    # checkpoint_get 09-12 起返回 {"project","channel","state"} —— 多带归属信息，
    # 好让回退前能校验"这个编号确实属于本卷"（见 db.checkpoint_get 的注释）
    got = db.checkpoint_get(ck)
    assert got["project"] == proj and got["state"]["materials"][0]["category"] == "三"
    # 账本
    db.correction_add(proj, a["id"], "set_category", {"before": {"category": "三"},
                                                      "after": {"category": "九-2"}})
    rows = db.corrections_list(proj)
    assert rows[0]["kind"] == "set_category" and rows[0]["before"]["category"] == "三"
