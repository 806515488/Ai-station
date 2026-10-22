"""archive 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
from station.persistence import database


def archive_state_save(project: str, job_id: str, user_id: str,
                       person: str, state: dict, channel: str = "") -> None:
    """保存（或覆盖）某个 channel 的现场：materials/issues 整包 JSON。

    channel="" 写的是卷级元数据行（归属/姓名/stage/active），其余是某家模型的现场。
    ★ ON CONFLICT 只更新 job_id/state/updated：**user_id/person 以第一次写入的为准**
      （建卷时定的归属与姓名，后续任何一家模型的写入都不该改写它）。
    """
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO archive_states(project,channel,job_id,user_id,person,state,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,channel) DO UPDATE SET "
            "job_id=excluded.job_id, state=excluded.state, updated=excluded.updated",
            (project, channel or "", job_id, user_id, person,
             json.dumps(state, ensure_ascii=False), database.now()))


def archive_state_owner(project: str) -> str:
    """某卷的归属 user_id；没有这一卷返回空串。

    ★ 给 server 的 `_owned_project` 用（**页图端点唯一的鉴权**）。
    ★ 多 channel 之后不能随便 fetchone：优先读**卷级元数据行**（channel=''，建卷时写的，
      归属最权威），没有才回落到任意一行 —— 保证同一卷无论有多少家的现场，
      鉴权答案唯一、稳定。
    **业务不要绕过仓库获取裸连接**：绕开 Database 作用域就把"多线程共用一条连接"
    的坑又埋回去了 —— 全景卡一次要拉上百张缩略图，每个都过这道归属校验，正好和聊天
    流里的 `thread.save()` 撞车，表现为**部分缩略图破图**（`<img>` 收到 401/500 的 JSON
    就是一张破图）。
    """
    with database.session() as connection:
        row = connection.execute(
            "SELECT user_id FROM archive_states WHERE project=? "
            "ORDER BY (channel <> '') ASC, updated DESC LIMIT 1",
            (project,)).fetchone()
        return row["user_id"] if row else ""


def archive_state_get(project: str, channel: str = "") -> dict | None:
    """读**指定 channel** 的现场；没有返回 None（channel="" 即卷级元数据行）。"""
    with database.session() as connection:
        row = connection.execute(
            "SELECT state FROM archive_states WHERE project=? AND channel=?",
            (project, channel or "")).fetchone()
        return json.loads(row["state"]) if row else None


def archive_state_channels(project: str) -> list[dict]:
    """这卷有哪些 channel 的现场（不含卷级元数据行），新→旧。

    给"报菜单"（这卷被哪几家读过）与展示切换用。返回 [{"channel","person","updated"}]。
    """
    with database.session() as connection:
        rows = connection.execute(
            "SELECT channel, person, updated FROM archive_states "
            "WHERE project=? AND channel<>'' ORDER BY updated DESC",
            (project,)).fetchall()
        return [{"channel": r["channel"], "person": r["person"],
                 "updated": r["updated"]} for r in rows]


def archive_state_delete(project: str, channel: str = "") -> None:
    """删掉某卷**某个 channel** 的现场（重识别前清旧的空现场用）。

    ★ 默认只有 channel=""（卷级元数据行）会走到这里；**换模型重跑绝不能删别人** ——
      要清某一家时显式传 channel。
    """
    with database.session(write=True) as connection:
        connection.execute("DELETE FROM archive_states WHERE project=? AND channel=?",
                       (project, channel or ""))


def correction_add(project: str, user_id: str, kind: str, payload: dict) -> None:
    """追加一条修正记录（原 corrections.jsonl 的 DB 版；聚合口径提案的数据源）。"""
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO corrections(project,user_id,kind,payload,ts) VALUES(?,?,?,?,?)",
            (project, user_id, kind, json.dumps(payload, ensure_ascii=False), database.now()))


def corrections_list(project: str) -> list[dict]:
    """某卷的全部修正记录（旧→新；read_corrections 的 DB 版）。"""
    with database.session() as connection:
        rows = connection.execute(
            "SELECT kind,payload,ts FROM corrections WHERE project=? ORDER BY id",
            (project,)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["payload"])
            d["kind"] = r["kind"]
            d["ts"] = r["ts"]
            out.append(d)
        return out


def checkpoint_add(project: str, user_id: str, label: str, state: dict,
                   channel: str = "") -> int:
    """存一个检查点（操作前的现场快照），返回检查点 id。

    channel：这份快照属于**哪家模型**的现场（换模型之后各家的回退链互不干扰）。
    """
    with database.session(write=True) as connection:
        connection = connection  # 先建连接，backend() 才能知道这次实际选中了哪一种数据库
        if database.backend() == "postgres":
            cur = connection.execute(
                "INSERT INTO checkpoints(project,channel,user_id,label,state,created) "
                "VALUES(?,?,?,?,?,?) RETURNING id",
                (project, channel or "", user_id, label,
                 json.dumps(state, ensure_ascii=False), database.now()))
            ck_id = int(cur.fetchone()["id"])
        else:
            cur = connection.execute(
                "INSERT INTO checkpoints(project,channel,user_id,label,state,created) "
                "VALUES(?,?,?,?,?,?)",
                (project, channel or "", user_id, label,
                 json.dumps(state, ensure_ascii=False), database.now()))
            ck_id = int(cur.lastrowid)
    return ck_id


def checkpoints_list(project: str, limit: int = 20, channel: str = "") -> list[dict]:
    """某卷**某一家模型**的检查点列表（新→旧；不含 state 本体，省流量）。

    ★ 它是回退的**前置**：回退要 ck_id，而 id 是全局自增号，猜不出来 ——
      没这个函数，检查点就是"每步都在写、却列不出 id"的死链。
    """
    with database.session() as connection:
        rows = connection.execute(
            "SELECT id,label,created FROM checkpoints WHERE project=? AND channel=? "
            "ORDER BY id DESC LIMIT ?", (project, channel or "", limit)).fetchall()
        return [{"id": r["id"], "label": r["label"], "created": r["created"]}
                for r in rows]


def checkpoint_get(ck_id: int) -> dict | None:
    """读一个检查点的完整现场（含它属于哪卷、哪家模型 —— 回退前要校验归属）。

    返回 {"project","channel","state"} 或 None。
    ★ 不能只回 state：ck_id 是**全局自增号**，模型可能编一个编号，而那个编号也许属于
      **另一卷** —— 不校验就把它写进当前卷（09-12 审计发现的真隐患）。
    """
    with database.session() as connection:
        row = connection.execute(
            "SELECT project,channel,state FROM checkpoints WHERE id=?",
            (ck_id,)).fetchone()
        if row is None:
            return None
        return {"project": row["project"], "channel": row["channel"],
                "state": json.loads(row["state"])}
