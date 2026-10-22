"""traces 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
from station.persistence import database


def trace_create(record: dict, key: str = "") -> dict:
    """关联键由服务端构造；恢复复用同一档案，重复提交不创建第二份追踪。"""
    with database.session(write=True) as connection:
        # 默认保存 30 天、每用户最多 1000 条。仅清理追踪，不动会话和产物。
        expired = connection.execute("SELECT id FROM station_traces WHERE user_id=? AND updated<?",
                            (record['user_id'],database.now()-30*86400)).fetchall()
        overflow = connection.execute("SELECT id FROM station_traces WHERE user_id=? ORDER BY created DESC "
                             "LIMIT 10000 OFFSET 999",(record['user_id'],)).fetchall()
        for row in [*expired,*overflow]:
            connection.execute("DELETE FROM station_trace_spans WHERE trace_id=?",(row['id'],))
            connection.execute("DELETE FROM station_traces WHERE id=? AND user_id=?",(row['id'],record['user_id']))
        key = key or record['id']
        connection.execute("INSERT INTO station_traces(id,user_id,thread_id,correlation_key,created,updated,record) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,correlation_key) DO NOTHING",
                  (record['id'],record['user_id'],record['thread_id'],key,record['created'],record['updated'],
                   json.dumps(record,ensure_ascii=False)))
        row = connection.execute("SELECT record FROM station_traces WHERE user_id=? AND correlation_key=?",
                        (record['user_id'],key)).fetchone()
        return json.loads(row['record'])


def trace_save(record: dict) -> dict:
    with database.session(write=True) as connection:
        previous = trace_get(record['id'], record['user_id'])
        if previous:
            record = {**record, 'state': previous['state'], 'active_ms': previous.get('active_ms', 0),
                      'attributes': {**previous.get('attributes', {}), **record.get('attributes', {})}}
        connection.execute("UPDATE station_traces SET updated=?,record=? WHERE id=? AND user_id=?",
                       (record['updated'],json.dumps(record,ensure_ascii=False),record['id'],record['user_id']))
        return record


def trace_finish(trace_id: str, user_id: str, state: str, duration: float, incomplete: bool) -> dict | None:
    """同一附件的并发恢复可能只执行一次；累计时间原子合并，跳过请求不能覆盖获胜者状态。"""
    with database.session(write=True) as connection:
        record = trace_get(trace_id, user_id)
        if not record:
            return None
        record.update(updated=database.now(), active_ms=round(record.get('active_ms',0)+duration,2),
                      incomplete=bool(record.get('incomplete') or incomplete))
        if state != 'skipped':
            record['state'] = state
        connection.execute("UPDATE station_traces SET updated=?,record=? WHERE id=? AND user_id=?",
                       (record['updated'],json.dumps(record,ensure_ascii=False),trace_id,user_id))
        return record


def trace_span_save(record: dict) -> None:
    with database.session(write=True) as connection:
        result = connection.execute("INSERT INTO station_trace_spans(id,trace_id,created,record) SELECT ?,?,?,? "
                                "WHERE EXISTS(SELECT 1 FROM station_trace_spans WHERE id=?) OR "
                                "(SELECT COUNT(*) FROM station_trace_spans WHERE trace_id=?) < 400 "
                                "ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                                (record['id'],record['trace_id'],record['created'],json.dumps(record,ensure_ascii=False),
                                 record['id'],record['trace_id']))
        if not result.rowcount:
            raise RuntimeError("trace span limit")


def trace_get(trace_id: str, user_id: str) -> dict | None:
    with database.session() as connection:
        row = connection.execute("SELECT record FROM station_traces WHERE id=? AND user_id=?",
                             (trace_id,user_id)).fetchone()
        return json.loads(row['record']) if row else None


def trace_spans(trace_id: str, user_id: str) -> list[dict]:
    with database.session() as connection:
        rows = connection.execute("SELECT s.record FROM station_trace_spans s JOIN station_traces t ON s.trace_id=t.id "
                              "WHERE t.id=? AND t.user_id=? ORDER BY s.created,s.id LIMIT 1000",
                              (trace_id,user_id)).fetchall()
        return [json.loads(r['record']) for r in rows]


def trace_list(thread_id: str, user_id: str) -> list[dict]:
    with database.session() as connection:
        rows = connection.execute("SELECT record FROM station_traces WHERE thread_id=? AND user_id=? "
                              "ORDER BY created DESC LIMIT 100",(thread_id,user_id)).fetchall()
        return [json.loads(r['record']) for r in rows]


def trace_children(trace_id: str, user_id: str) -> list[dict]:
    with database.session() as connection:
        row = connection.execute("SELECT thread_id FROM station_traces WHERE id=? AND user_id=?",(trace_id,user_id)).fetchone()
        if not row:
            return []
        rows = connection.execute("SELECT record FROM station_traces WHERE thread_id=? AND user_id=? "
                              "ORDER BY created DESC LIMIT 1000", (row['thread_id'],user_id)).fetchall()
        return [record for row in rows if (record := json.loads(row['record'])).get('origin_trace_id') == trace_id]
