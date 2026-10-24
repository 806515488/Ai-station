"""threads 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
import uuid
from station.persistence import database
from station.persistence.errors import ThreadConflict


def thread_save(record: dict, *, expected=None) -> tuple:
    """按读取时的原始快照条件保存；不让慢请求覆盖已领取/完成的审批。

    expected 是仓库返回的 msgs/meta/updated 原始值，不是模型历史 revision。
    不新增数据库列；直接以 SQL 条件更新实现跨连接、跨实例的比较后写入。
    """
    messages = json.dumps(record["msgs"], ensure_ascii=False)
    metadata = json.dumps(record["meta"], ensure_ascii=False)
    updated = record["updated"]
    owner = record.get("user_id", "") or ""
    with database.session(write=True) as connection:
        if expected is not None:
            cursor = connection.execute(
                "UPDATE threads SET skill_id=?,msgs=?,meta=?,updated=? "
                "WHERE id=? AND user_id=? AND msgs=? AND meta=? AND updated=?",
                (record["skill_id"], messages, metadata, updated, record["id"], owner, *expected))
        else:
            cursor = connection.execute(
                "INSERT INTO threads(id,user_id,skill_id,msgs,meta,created,updated) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (record["id"], owner, record["skill_id"], messages, metadata,
                 record.get("created", database.now()), updated))
        if cursor.rowcount != 1:
            raise ThreadConflict("会话状态已更新，请刷新后重试")
    return messages, metadata, updated


def thread_load(thread_id: str) -> dict | None:
    """读取纯会话记录，不依赖 core.session.Thread。"""
    with database.session() as connection:
        row = connection.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["_version"] = (value["msgs"], value["meta"], value["updated"])
        value['msgs'] = json.loads(value['msgs'])
        value['meta'] = json.loads(value['meta'])
        return value


def ensure_pending_id(thread_id: str, owner: str) -> dict | None:
    """旧挂起记录按需补稳定 ID；只重试元数据竞争，不执行任何工具。"""
    for _ in range(3):
        record = thread_load(thread_id)
        if not record or record["user_id"] != owner:
            return None
        pending = record["meta"].get("pending")
        if not pending or pending.get("id"):
            return record
        record["meta"]["pending"] = {**pending, "id": uuid.uuid4().hex, "state": "waiting"}
        record["updated"] = database.now()
        try:
            record["_version"] = thread_save(record, expected=record["_version"])
            return record
        except ThreadConflict:
            continue
    raise ThreadConflict("会话状态已更新，请刷新后重试")


def claim_approval(thread_id: str, owner: str, approval_id: str, decision: str) -> dict | None:
    """原子领取指定的待批准操作；只有一次 waiting→executing 可以成功。

    不持有长事务等待外部工具；执行中断后保留 executing，禁止不明操作自动重放。
    """
    if decision not in ("allow", "deny"):
        raise ValueError("无效的批准决定")
    record = thread_load(thread_id)
    if not record or record["user_id"] != owner:
        return None
    pending = record["meta"].get("pending")
    if (not pending or pending.get("id") != approval_id
            or pending.get("state", "waiting") != "waiting"):
        return None
    record["meta"]["pending"] = {**pending, "state": "executing", "decision": decision,
                                  "claimed_at": database.now()}
    record["updated"] = database.now()
    try:
        record["_version"] = thread_save(record, expected=record["_version"])
        return record
    except ThreadConflict:
        return None

def thread_list(user_id: str, skill_id: str | None = None) -> list[dict]:
    """某用户的会话摘要（新→旧）。skill_id 可选过滤（前端按技能分组展示）。"""
    sql = "SELECT id,skill_id,msgs,updated FROM threads WHERE user_id=?"
    args: list = [user_id]
    if skill_id:
        sql += " AND skill_id=?"
        args.append(skill_id)
    sql += " ORDER BY updated DESC"
    out = []
    with database.session() as connection:                     # ★ 连**遍历游标**也要在锁里：行是惰性取的
        for row in connection.execute(sql, args):
            msgs = json.loads(row["msgs"])
            out.append({"id": row["id"], "skill_id": row["skill_id"],
                        "updated": row["updated"], "n": len(msgs),
                        "last": (msgs[-1].get("content", "") if msgs else "")[:60]})
    return out
