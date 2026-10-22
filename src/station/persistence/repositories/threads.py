"""threads 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
from station.persistence import database


def thread_save(record: dict) -> None:
    """保存会话纯记录；消息和元数据编码为 JSON，已有会话保留创建时间。"""
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO threads(id,user_id,skill_id,msgs,meta,created,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "user_id=excluded.user_id, skill_id=excluded.skill_id, msgs=excluded.msgs, "
            "meta=excluded.meta, updated=excluded.updated",
            (record["id"], record.get("user_id", "") or "", record["skill_id"],
             json.dumps(record["msgs"], ensure_ascii=False),
             json.dumps(record["meta"], ensure_ascii=False),
             record.get("created", database.now()), record["updated"]))


def thread_load(thread_id: str) -> dict | None:
    """读取纯会话记录，不依赖 core.session.Thread。"""
    with database.session() as connection:
        row = connection.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['msgs'] = json.loads(value['msgs'])
        value['meta'] = json.loads(value['meta'])
        return value

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
