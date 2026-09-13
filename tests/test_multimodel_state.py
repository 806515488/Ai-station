"""多识图模型现场共存的不变量（09-12）—— 换模型不许覆盖、鉴权不许歧义、迁移要幂等。

新手视角（Java 朋友版）：这是一组"数据契约"测试。背景是现场从
「一卷一行」变成了「一卷 × 一家模型一行」：
  · channel = ''      → 卷级元数据行（归属/姓名/当前展示哪家）
  · channel = 'qwen'  → 那家模型读出来的现场
下面每条钉的都是"改坏了会很难查"的地方 —— 尤其第 2 条：换模型重跑如果把前一家
的结果覆盖/删掉了，用户就再也看不到它了（而这正是这次改动的**目的**）。
"""
from __future__ import annotations

import json
import sqlite3

from station import db
from archive.service import interactive as it


# ── ① 老库迁移 ────────────────────────────────────────────────────

def test_migration_adds_channel_and_keeps_old_rows(tmp_path):
    """老库（archive_states 主键只有 project）跑一次 init_db：结构升级、老数据不丢、可重复跑。"""
    p = tmp_path / "old.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row      # 跟 db.conn() 一致，好按列名取
    con.executescript("""
        CREATE TABLE archive_states(
            project TEXT PRIMARY KEY, job_id TEXT NOT NULL, user_id TEXT NOT NULL,
            person TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE checkpoints(
            id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
            user_id TEXT NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL,
            created REAL NOT NULL);
    """)
    con.execute("INSERT INTO archive_states VALUES(?,?,?,?,?,?)",
                ("P", "j1", "U1", "丁", json.dumps({"materials": [{"uid": "m1"}]}), 1.0))
    con.execute("INSERT INTO checkpoints(project,user_id,label,state,created) "
                "VALUES(?,?,?,?,?)", ("P", "U1", "改类前", json.dumps({}), 1.0))
    con.commit()

    db.init_db(con)                                  # 触发迁移
    cols = {r[1] for r in con.execute("PRAGMA table_info(archive_states)")}
    assert "channel" in cols                         # 新列在
    rows = list(con.execute("SELECT project,channel,person FROM archive_states"))
    assert len(rows) == 1 and rows[0]["channel"] == ""   # 老数据落到"卷级元数据行"
    assert rows[0]["person"] == "丁"                      # 内容没丢
    assert "channel" in {r[1] for r in con.execute("PRAGMA table_info(checkpoints)")}
    assert con.execute("SELECT channel FROM checkpoints").fetchone()["channel"] == ""

    db.init_db(con)                                  # ★ 幂等：再跑一次不炸、不重复搬
    assert con.execute("SELECT COUNT(*) c FROM archive_states").fetchone()["c"] == 1
    con.close()


def test_migration_rebuilds_state_index(tmp_path):
    """重建表会把索引一起删掉 → 迁移必须把它建回来，否则按用户查现场会全表扫。"""
    p = tmp_path / "old2.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("CREATE TABLE archive_states(project TEXT PRIMARY KEY,"
                      " job_id TEXT NOT NULL, user_id TEXT NOT NULL,"
                      " person TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,"
                      " updated REAL NOT NULL);")
    con.commit()
    db.init_db(con)
    names = {r[1] for r in con.execute("PRAGMA index_list(archive_states)")}
    assert "idx_states_user" in names
    con.close()


# ── ② 多模型并存（本次改动的核心不变量）────────────────────────────

def _save(pj, channel, person, mats):
    return it.save_state(pj, mats, [], person, channel=channel)


def test_two_models_coexist_and_switch(tmp_path):
    """两家各存一份 → 各自读得到；切换 active 只改"看哪家"，两家数据都在。"""
    pj = str(tmp_path / "project.json")
    it.register_volume(pj, "丁", "u1")
    _save(pj, "qwen", "丁", [{"uid": "m1", "category": "一"}])
    _save(pj, "glm", "丁", [{"uid": "m1", "category": "九-2"}])

    assert it.load_state(pj, "qwen")["materials"][0]["category"] == "一"
    assert it.load_state(pj, "glm")["materials"][0]["category"] == "九-2"
    # 没指定 active 时回落"最近写入的那一家"（重开这卷看到的是刚跑的那份）
    assert it.load_state(pj)["materials"][0]["category"] == "九-2"

    it.set_active(pj, "qwen")                        # 切过去
    assert it.load_state(pj)["materials"][0]["category"] == "一"
    assert {c["channel"] for c in it.channels(pj)} == {"qwen", "glm"}   # 两家都还在
    assert [c["channel"] for c in it.channels(pj) if c["active"]] == ["qwen"]


def test_invalidate_one_channel_never_touches_the_other(tmp_path):
    """★ 换模型重跑只清自己那一家的现场 —— 把别家一起删了，用户就再也看不到它了。"""
    pj = str(tmp_path / "project.json")
    it.register_volume(pj, "丁", "u1")
    _save(pj, "qwen", "丁", [{"uid": "m1", "category": "一"}])
    _save(pj, "glm", "丁", [{"uid": "m1", "category": "九-2"}])

    it.invalidate_state(pj, "qwen")                  # 只清 qwen
    assert it.load_state(pj, "qwen") is None
    assert it.load_state(pj, "glm")["materials"][0]["category"] == "九-2"   # glm 纹丝不动


def test_owner_is_unambiguous_with_many_channels(tmp_path):
    """页图鉴权（唯一的授权检查）在多行时必须给出同一个、确定的答案。"""
    pj = str(tmp_path / "project.json")
    it.register_volume(pj, "丁", "u1")
    _save(pj, "qwen", "丁", [])
    _save(pj, "glm", "丁", [])
    assert db.archive_state_owner(pj) == "u1"
    assert db.archive_state_owner(str(tmp_path / "没有这卷.json")) == ""


def test_editing_writes_the_active_channel_only(tmp_path):
    """用户改类/并份只作用在**当前展示**那一家上，不会串到别家。"""
    pj = str(tmp_path / "project.json")
    it.register_volume(pj, "丁", "u1")
    _save(pj, "qwen", "丁", [{"uid": "m1", "category": "一"}])
    _save(pj, "glm", "丁", [{"uid": "m1", "category": "一"}])
    it.set_active(pj, "glm")

    it.save_state(pj, [{"uid": "m1", "category": "五"}], [], "丁")   # 不传 channel
    assert it.load_state(pj, "glm")["materials"][0]["category"] == "五"
    assert it.load_state(pj, "qwen")["materials"][0]["category"] == "一"   # 别家没动


# ── ③ 检查点跟着模型走 + 跨卷编号必须被拒 ─────────────────────────

def test_checkpoints_are_per_channel(tmp_path):
    """检查点按模型隔离：另一家的回退链不出现在这一家的列表里。"""
    pj = str(tmp_path / "project.json")
    it.register_volume(pj, "丁", "u1")
    _save(pj, "qwen", "丁", [{"uid": "m1"}])
    _save(pj, "glm", "丁", [{"uid": "m1"}])
    it.save_checkpoint(pj, {"materials": [{"uid": "m1"}]}, "qwen 的第一步", channel="qwen")
    it.save_checkpoint(pj, {"materials": [{"uid": "m1"}]}, "glm 的第一步", channel="glm")

    assert [c["label"] for c in it.list_checkpoints(pj, channel="qwen")] == ["qwen 的第一步"]
    assert [c["label"] for c in it.list_checkpoints(pj, channel="glm")] == ["glm 的第一步"]


def test_restore_rejects_another_volume_checkpoint(tmp_path):
    """★ 拿别卷的检查点编号来恢复必须报错 —— 否则会把那卷的现场写进本卷。

    （ck_id 是全局自增号，模型可能随口编一个；09-12 审计前只靠 system.md 一句
      "别自己编编号"兜着，代码层是不拦的。）
    """
    import pytest
    pj_a = str(tmp_path / "A" / "project.json")
    pj_b = str(tmp_path / "B" / "project.json")
    it.register_volume(pj_a, "甲", "u1")
    it.register_volume(pj_b, "乙", "u1")
    ck = it.save_checkpoint(pj_a, {"materials": [{"uid": "x"}]}, "A 的点", channel="qwen")

    with pytest.raises(ValueError, match="不属于当前这卷"):
        it.restore_checkpoint(pj_b, ck)
