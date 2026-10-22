"""jobs 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
import uuid
from station.persistence.backends.postgres import PostgresConnection
from station.persistence import database


def job_save(record: dict, lease_token: str = "") -> None:
    """保存任务记录；带 lease_token 时在同一 SQL 内校验执行权。"""
    with database.session(write=True) as connection:
        token=lease_token
        if token:
            # 围栏必须与写入是同一条 SQL：心跳检查后网络暂停时，旧 token 仍不能覆盖新 worker。
            result=connection.execute(
                "UPDATE jobs SET status=?,progress=?,message=?,log=?,artifacts=?,result=?,updated=? "
                "WHERE id=? AND user_id=? AND EXISTS(SELECT 1 FROM station_task_control c "
                "WHERE c.job_id=jobs.id AND c.user_id=jobs.user_id AND c.token=? AND c.lease_until>?)",
                (record["status"],record["progress"],record["message"],json.dumps(record["log"],ensure_ascii=False),json.dumps(record["artifacts"]),
                 json.dumps(record["result"],ensure_ascii=False) if record["result"] is not None else None,record["updated"],
                 record["id"],record["user_id"],token,database.now()))
            if not result.rowcount:
                from station.persistence.errors import LeaseLost
                raise LeaseLost()
            return
        connection.execute(
            "INSERT INTO jobs(id,user_id,skill_id,args,status,progress,message,"
            "log,artifacts,result,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "progress=excluded.progress, message=excluded.message, log=excluded.log, "
            "artifacts=excluded.artifacts, result=excluded.result, "
            "updated=excluded.updated",
            (record["id"], record.get("user_id", "") or "", record["skill_id"],
             json.dumps(record["args"], ensure_ascii=False), record["status"], record["progress"],
             record["message"], json.dumps(record["log"], ensure_ascii=False),
             json.dumps(record["artifacts"], ensure_ascii=False),
             json.dumps(record["result"], ensure_ascii=False) if record["result"] is not None else None,
             record["created"], record["updated"]))


def _job_from_row(row) -> dict:
    """数据库行转纯记录；Job 记录由 tasks.runtime 还原为纯数据对象。"""
    value = dict(row)
    for key in ('args', 'log', 'artifacts', 'result'):
        value[key] = json.loads(value[key]) if value[key] is not None else None
    return value

def job_load(job_id: str):
    with database.session() as connection:
        row=connection.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        return _job_from_row(row) if row else None


def task_job_register(record: dict, metadata: dict) -> bool:
    """任务与恢复元数据一起登记；同 ID 的并发提交不能把已运行任务覆盖回 queued。"""
    with database.session(write=True) as connection:
        result=connection.execute("INSERT INTO jobs(id,user_id,skill_id,args,status,progress,message,log,artifacts,result,created,updated) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                         (record["id"],record["user_id"],record["skill_id"],json.dumps(record["args"],ensure_ascii=False),record["status"],record["progress"],
                          record["message"],json.dumps(record["log"]),json.dumps(record["artifacts"]),
                          json.dumps(record["result"],ensure_ascii=False) if record["result"] is not None else None,record["created"],record["updated"]))
        if result.rowcount:
            connection.execute("INSERT INTO station_task_control(job_id,user_id,record,updated) VALUES(?,?,?,?)",
                      (record["id"],record["user_id"],json.dumps(metadata,ensure_ascii=False),database.now()))
        return bool(result.rowcount)


def task_control_create(job_id: str, user_id: str, record: dict) -> None:
    with database.session(write=True) as connection:
        connection.execute("INSERT INTO station_task_control(job_id,user_id,record,updated) VALUES(?,?,?,?) "
                       "ON CONFLICT(job_id) DO NOTHING",(job_id,user_id,json.dumps(record,ensure_ascii=False),database.now()))


def task_control_get(job_id: str, user_id: str) -> dict | None:
    with database.session() as connection:
        row=connection.execute("SELECT * FROM station_task_control WHERE job_id=? AND user_id=?",(job_id,user_id)).fetchone()
        return {**dict(row),'record':json.loads(row['record'])} if row else None


def task_claim(job_id: str, user_id: str, instance: str, boot: str, token: str, seconds: float) -> bool:
    with database.session(write=True) as connection:
        now=database.now()
        result=connection.execute("UPDATE station_task_control SET instance_id=?,boot_id=?,token=?,lease_until=?,"
                              "cancel_requested=0,updated=? WHERE job_id=? AND user_id=? AND "
                              "(lease_until<=? OR (instance_id=? AND boot_id<>?))",
                              (instance,boot,token,now+seconds,now,job_id,user_id,now,instance,boot))
        return bool(result.rowcount)


def task_heartbeat(job_id: str, user_id: str, token: str, seconds: float) -> bool:
    with database.session(write=True) as connection:
        now=database.now()
        result=connection.execute("UPDATE station_task_control SET lease_until=?,updated=? "
                              "WHERE job_id=? AND user_id=? AND token=? AND lease_until>?",
                              (now+seconds,now,job_id,user_id,token,now))
        return bool(result.rowcount)


def task_checkpoint(job_id: str, user_id: str, token: str, values: dict) -> bool:
    with database.session(write=True) as connection:
        row=task_control_get(job_id,user_id)
        if not row or row['token']!=token or row['lease_until']<=database.now():return False
        record={**row['record'],**values}
        result=connection.execute("UPDATE station_task_control SET record=?,updated=? WHERE job_id=? AND user_id=? AND token=?",
                              (json.dumps(record,ensure_ascii=False),database.now(),job_id,user_id,token))
        return bool(result.rowcount)


def task_release(job_id: str, user_id: str, token: str) -> None:
    with database.session(write=True) as connection:
        connection.execute("UPDATE station_task_control SET lease_until=0,updated=? WHERE job_id=? AND user_id=? AND token=?",
                       (database.now(),job_id,user_id,token))


def task_cancel(job_id: str, user_id: str) -> bool:
    with database.session(write=True) as connection:
        result=connection.execute("UPDATE station_task_control SET cancel_requested=1,updated=? WHERE job_id=? AND user_id=?",
                              (database.now(),job_id,user_id))
        return bool(result.rowcount)


def task_job_list(user_id: str, before: tuple | None = None, limit: int = 51) -> list:
    with database.session() as connection:
        where="user_id=?";args=[user_id]
        if before:
            where+=" AND (created<? OR (created=? AND id<?))";args.extend((before[0],before[0],before[1]))
        rows=connection.execute("SELECT * FROM jobs WHERE "+where+" ORDER BY created DESC,id DESC LIMIT ?",(*args,min(200,max(1,limit)))).fetchall()
        return [_job_from_row(row) for row in rows]


def task_records_many(kind: str, user_id: str, ids: list) -> dict:
    """固定表白名单的批量读取，列表页不为每个任务单独往返远程数据库。"""
    with database.session() as connection:
        table,column={"control":("station_task_control","job_id"),"turn":("station_attachment_turns","id"),
                      "document":("station_documents","id")}[kind]
        values=list(dict.fromkeys(i for i in ids if i))[:2000]
        result={}
        for offset in range(0,len(values),200):
            batch=values[offset:offset+200]
            rows=connection.execute("SELECT * FROM "+table+" WHERE user_id=? AND "+column+" IN ("+",".join("?" for _ in batch)+")",
                                (user_id,*batch)).fetchall()
            for row in rows:
                value=json.loads(row['record'])
                result[row[column]]={**dict(row),'record':value} if kind=='control' else value
        return result


def task_existing_jobs(user_id: str, ids: list) -> set:
    with database.session() as connection:
        values=list(dict.fromkeys(i for i in ids if i))[:100]
        if not values:return set()
        rows=connection.execute("SELECT id FROM jobs WHERE user_id=? AND id IN ("+",".join("?" for _ in values)+")",(user_id,*values)).fetchall()
        return {r['id'] for r in rows}


def task_action_reserve(user_id: str, job_id: str, action: str, client_key: str, next_job_id: str) -> dict:
    """同一操作编号幂等，同一旧任务只产生一个接续任务；事务锁覆盖多进程竞争。"""
    with database.session(write=True) as connection:
        suffix=" FOR UPDATE" if isinstance(connection,PostgresConnection) else ""
        control=connection.execute("SELECT record FROM station_task_control WHERE job_id=? AND user_id=?"+suffix,(job_id,user_id)).fetchone()
        existing=connection.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?",(user_id,client_key)).fetchone()
        if existing:
            if existing['job_id']!=job_id or existing['action']!=action:raise ValueError('该操作编号已用于其他任务')
            return dict(existing)
        if not control:raise ValueError('旧任务缺少执行信息，请回原会话核对')
        record=json.loads(control['record'])
        if record.get('successor_id') and record.get('successor_action') not in (None,action):
            raise ValueError('已有其他接续操作，请查看接续任务')
        next_job_id=record.get('successor_id') or next_job_id
        record['successor_id']=next_job_id
        record['successor_action']=action
        connection.execute("UPDATE station_task_control SET record=?,updated=? WHERE job_id=? AND user_id=?",
                  (json.dumps(record,ensure_ascii=False),database.now(),job_id,user_id))
        connection.execute("INSERT INTO station_task_actions(id,user_id,job_id,action,client_key,next_job_id,created) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,client_key) DO NOTHING",
                  (uuid.uuid4().hex,user_id,job_id,action,client_key,next_job_id,database.now()))
        saved=connection.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?",(user_id,client_key)).fetchone()
        if saved['job_id']!=job_id or saved['action']!=action:raise ValueError('该操作编号已用于其他任务')
        return dict(saved)
