"""attachments 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
from station.persistence import database


def document_register(record: dict, upload_key: str) -> dict:
    """登记原件并分配当天次序；同一上传请求重放不会登记两次。"""
    with database.session(write=True) as connection:
        old = connection.execute("SELECT record FROM station_documents WHERE user_id=? AND upload_key=?",
                        (record["owner_id"], upload_key)).fetchone()
        if old:
            return json.loads(old["record"])
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM station_documents "
            "WHERE user_id=? AND local_date=?", (record["owner_id"], record["date"])).fetchone()["n"]
        value = dict(record, sequence=sequence)
        connection.execute("INSERT INTO station_documents(id,user_id,upload_key,local_date,sequence,record,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?)", (value["id"], value["owner_id"], upload_key, value["date"],
                  sequence, json.dumps(value, ensure_ascii=False), value["created"], database.now()))
        return value


def document_get(document_id: str, user_id: str) -> dict | None:
    """文档访问永远同时带 owner，内容哈希不充当权限。"""
    with database.session() as connection:
        row = connection.execute("SELECT record FROM station_documents WHERE id=? AND user_id=?",
                             (document_id, user_id)).fetchone()
        return json.loads(row["record"]) if row else None


def document_update(document_id: str, user_id: str, patch: dict) -> dict:
    """只更新当前用户的文档元数据；调用者不能修改归属/身份/上传日期。"""
    with database.session(write=True) as connection:
        value = document_get(document_id, user_id)
        if value is None:
            raise ValueError("文件不存在")
        if set(patch) & {"id", "owner_id", "date", "sequence", "created"}:
            raise ValueError("不能修改文档身份")
        value.update(patch)
        value["updated"] = database.now()
        connection.execute("UPDATE station_documents SET record=?,updated=? WHERE id=? AND user_id=?",
                  (json.dumps(value, ensure_ascii=False), value["updated"], document_id, user_id))
        return value


def document_list(user_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
    """按日期/当天数值次序列出文档；大正文不进入数据库。"""
    with database.session() as connection:
        rows = connection.execute("SELECT record FROM station_documents WHERE user_id=? "
                              "ORDER BY local_date DESC,sequence ASC LIMIT ? OFFSET ?",
                              (user_id, max(1, min(limit, 1000)), max(0, offset))).fetchall()
        return [json.loads(row["record"]) for row in rows]


def document_delete_draft(document_id: str, user_id: str) -> bool:
    """仅删除从未被轮次引用的上传草稿；已使用的原件不能随移除选择一起删除。"""
    with database.session(write=True) as connection:
        document = document_get(document_id, user_id)
        if not document or document.get("runs") or document.get("status") != "uploaded":
            return False
        # 不只查最近 1000 个轮次；保守的带引号 ID 匹配也会保护更老的引用。
        used = connection.execute("SELECT id FROM station_attachment_turns WHERE user_id=? AND record LIKE ? LIMIT 1",
                              (user_id, '%"' + document_id + '"%')).fetchone()
        if used:
            return False
        connection.execute("DELETE FROM station_documents WHERE id=? AND user_id=?", (document_id, user_id))
        return True


def document_expired_drafts(cutoff: float) -> list[dict]:
    with database.session() as connection:
        rows = connection.execute("SELECT record FROM station_documents WHERE created<?", (cutoff,)).fetchall()
        return [value for row in rows if (value := json.loads(row["record"])).get("status") == "uploaded"]


def attachment_turn_create(record: dict) -> tuple[dict, bool]:
    """冻结一轮文字/附件；唯一键防止断网重发和双击发送。"""
    with database.session(write=True) as connection:
        row = connection.execute("SELECT record FROM station_attachment_turns WHERE user_id=? AND client_key=?",
                        (record["owner_id"], record["client_key"])).fetchone()
        if row:
            return json.loads(row["record"]), False
        # 与草稿删除共用数据库锁，不能在校验后被清理再提交一个悬空引用。
        if any(document_get(document_id, record["owner_id"]) is None for document_id in record["attachment_ids"]):
            raise ValueError("附件不存在或已经过期")
        connection.execute("INSERT INTO station_attachment_turns(id,user_id,thread_id,client_key,state,record,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?)", (record["id"], record["owner_id"], record["thread_id"],
                  record["client_key"], record["state"], json.dumps(record, ensure_ascii=False),
                  record["created"], database.now()))
        return record, True


def attachment_turn_get(turn_id: str, user_id: str) -> dict | None:
    with database.session() as connection:
        row = connection.execute("SELECT record FROM station_attachment_turns WHERE id=? AND user_id=?",
                             (turn_id, user_id)).fetchone()
        return json.loads(row["record"]) if row else None


def attachment_turn_find(client_key: str, user_id: str) -> dict | None:
    """首次发送响应丢失时，客户端尚无 thread_id，仍可按请求编号找到原轮次。"""
    with database.session() as connection:
        row = connection.execute("SELECT record FROM station_attachment_turns WHERE client_key=? AND user_id=? "
                             "ORDER BY created DESC LIMIT 1", (client_key, user_id)).fetchone()
        return json.loads(row["record"]) if row else None


def attachment_turn_update(turn_id: str, user_id: str, patch: dict,
                           expected: tuple[str, ...] | None = None,
                           *, expected_claim_id: str | None = None) -> dict | None:
    """状态条件用于领取执行权；收尾另校验 claim，避免旧请求结束新请求的轮次。"""
    with database.session(write=True) as connection:
        row = connection.execute("SELECT record FROM station_attachment_turns WHERE id=? AND user_id=?",
                        (turn_id, user_id)).fetchone()
        value = json.loads(row["record"]) if row else None
        if value is None or (expected is not None and value["state"] not in expected):
            return None
        if expected_claim_id is not None and value.get("agent_claim_id") != expected_claim_id:
            return None
        if set(patch) & {"id", "owner_id", "thread_id", "client_key", "message", "attachment_ids", "config"}:
            raise ValueError("不能修改已冻结的附件输入")
        previous = value["state"]
        value.update(patch)
        value["updated"] = database.now()
        # 同一个进程的锁不能挡住另一实例。收尾比较读取时的原始 record，保证检查 claim
        # 与写回之间即便发生了远端接管，本次 UPDATE 也不会覆盖新执行者（两种数据库通用）。
        guard = " AND record=?" if expected_claim_id is not None else ""
        args = (value["state"], json.dumps(value, ensure_ascii=False), value["updated"],
                turn_id, user_id, previous)
        if expected_claim_id is not None:
            args += (row["record"],)
        cur = connection.execute("UPDATE station_attachment_turns SET state=?,record=?,updated=? "
                        "WHERE id=? AND user_id=? AND state=?" + guard, args)
        return value if cur.rowcount else None


def attachment_turn_list(user_id: str = "", thread_id: str = "") -> list[dict]:
    """空 owner 只供宿主启动恢复使用，HTTP 查询必须带登录用户。"""
    with database.session() as connection:
        where, args = [], []
        if user_id:
            where.append("user_id=?")
            args.append(user_id)
        if thread_id:
            where.append("thread_id=?")
            args.append(thread_id)
        rows = connection.execute("SELECT record FROM station_attachment_turns" +
                              (" WHERE " + " AND ".join(where) if where else "") +
                              " ORDER BY created DESC LIMIT 1000", tuple(args)).fetchall()
        return [json.loads(row["record"]) for row in rows]
