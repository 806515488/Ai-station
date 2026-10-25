"""任务仓库：双状态、消息版本和业务阶段；不创建线程或连接 MQ。"""
import json
import time
import uuid
from station.persistence import database
from station.persistence.backends.postgres import PostgresConnection
from station.persistence.errors import ExecutionLost

FIELDS = (
    "id", "skill_id", "args", "user_id", "work_status", "message_status", "message_id",
    "phase", "queue", "meta", "execution_id", "publish_attempts", "next_publish_at",
    "publish_error", "step_attempts", "cancel_requested", "progress", "message",
    "created", "updated", "log", "artifacts", "result",
)
JSON_FIELDS = {"args", "meta", "log", "artifacts", "result"}
ACTIVE = ("pending", "running")


def _encode(name, value):
    return json.dumps(value, ensure_ascii=False) if name in JSON_FIELDS and value is not None else value


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for name in JSON_FIELDS:
        if result[name] is not None:
            result[name] = json.loads(result[name])
    return result


def job_load(job_id):
    with database.session() as c:
        return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def task_job_register(record, metadata=None):
    """只插入，不覆盖已存在任务；输入和待投递状态一次提交。"""
    value = {**record, "meta": {**record.get("meta", {}), **(metadata or {})}}
    with database.session(write=True) as c:
        result = c.execute(
            "INSERT INTO jobs(" + ",".join(FIELDS) + ") VALUES(" + ",".join("?" for _ in FIELDS)
            + ") ON CONFLICT(id) DO NOTHING",
            tuple(_encode(k, value[k]) for k in FIELDS))
        return bool(result.rowcount)


def job_save(record):
    """仅用于尚未登记的记录；运行中的更新必须有消息和执行版本。"""
    if not task_job_register(record):
        raise ValueError("任务已存在，请使用受控状态转换，不能覆盖整个旧快照")


def task_job_list(user_id, before=None, limit=51):
    with database.session() as c:
        where, args = "user_id=?", [user_id]
        if before:
            where += " AND (created<? OR (created=? AND id<?))"
            args.extend((before[0], before[0], before[1]))
        return [_row(r) for r in c.execute(
            "SELECT * FROM jobs WHERE " + where + " ORDER BY created DESC,id DESC LIMIT ?",
            (*args, min(200, max(1, limit)))).fetchall()]


def pending_messages(limit=10, max_attempts=6, queues=None):
    with database.session() as c:
        scope = " AND queue IN (" + ",".join("?" for _ in queues) + ")" if queues else ""
        return [_row(r) for r in c.execute(
            "SELECT * FROM jobs WHERE message_status IN ('pending','unknown') "
            "AND work_status IN ('pending','running') AND cancel_requested=0 "
            "AND phase<>'legacy' AND queue<>'' AND next_publish_at<=? AND publish_attempts<? "
            + scope + " ORDER BY next_publish_at,created,id LIMIT ?",
            (time.time(), max_attempts, *(queues or ()), limit)).fetchall()]


def begin_publish(job_id, message_id, max_attempts):
    with database.session(write=True) as c:
        return bool(c.execute(
            "UPDATE jobs SET publish_attempts=publish_attempts+1 WHERE id=? AND message_id=? "
            "AND message_status IN ('pending','unknown') AND work_status IN ('pending','running') "
            "AND cancel_requested=0 AND publish_attempts<?",
            (job_id, message_id, max_attempts)).rowcount)


def publish_result(job_id, message_id, state, error="", next_at=0, exhausted=False):
    """迟到的确认只修改匹配消息；不会把新步骤或 Worker 状态改回旧值。"""
    with database.session(write=True) as c:
        count = c.execute(
            "UPDATE jobs SET message_status=?,publish_error=?,next_publish_at=?,updated=? "
            "WHERE id=? AND message_id=? AND message_status<>'confirmed'",
            (state, error, next_at, time.time(), job_id, message_id)).rowcount
        if count and exhausted:
            c.execute("UPDATE jobs SET work_status='needs_review',message=? WHERE id=? AND message_id=? AND work_status IN ('pending','running') AND execution_id='' AND step_attempts=0",
                      ("消息投递未能确认，请核对 MQ 配置后重新投递", job_id, message_id))
        return bool(count)


def review_exhausted_publications(max_attempts, queues=None):
    """覆盖计数已增加、发送前进程退出的窗口，不让待投递记录永久卡住。"""
    with database.session(write=True) as c:
        scope = " AND queue IN (" + ",".join("?" for _ in queues) + ")" if queues else ""
        c.execute("UPDATE jobs SET work_status='needs_review',message_status='unknown',message=?,updated=? "
                  "WHERE publish_attempts>=? AND message_status IN ('pending','unknown') AND work_status IN ('pending','running') AND execution_id='' AND step_attempts=0" + scope,
                  ("消息投递未能确认，请检查配置后重新投递", time.time(), max_attempts, *(queues or ())))


def review_unhandled_failure(job_id, message_id):
    with database.session(write=True) as c:
        c.execute("UPDATE jobs SET work_status='needs_review',message=?,updated=? WHERE id=? AND message_id=? AND work_status IN ('pending','running')",
                  ("Worker 执行中断或重试次数耗尽，请核对执行记录", time.time(), job_id, message_id))


def begin_execution(job_id, message_id, execution_id):
    with database.session(write=True) as c:
        row = _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if not row or row["message_id"] != message_id or row["phase"] == "legacy" or row["work_status"] not in ACTIVE:
            return None
        if row["cancel_requested"]:
            c.execute("UPDATE jobs SET work_status='cancelled',message=?,updated=? WHERE id=?",
                      ("已取消本地处理", time.time(), job_id))
            return None
        c.execute("UPDATE jobs SET execution_id=?,work_status='running',step_attempts=step_attempts+1,updated=? WHERE id=? AND message_id=?",
                  (execution_id, time.time(), job_id, message_id))
        return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def execution_update(record, expected_message, expected_execution, changes=None):
    """工作结果与消息投递字段分开写，防止 Worker 覆盖发布端刚收到的确认。"""
    value = {k: record[k] for k in ("work_status", "progress", "message", "log", "artifacts", "result", "meta", "updated")}
    value.update(changes or {})
    allowed = set(FIELDS) - {"id", "user_id", "skill_id", "args", "created", "cancel_requested"}
    if not set(value) <= allowed:
        raise ValueError("无效任务写入字段")
    with database.session(write=True) as c:
        count = c.execute(
            "UPDATE jobs SET " + ",".join(k + "=?" for k in value)
            + " WHERE id=? AND user_id=? AND message_id=? AND execution_id=? AND work_status IN ('pending','running')",
            (*(_encode(k, v) for k, v in value.items()), record["id"], record["user_id"], expected_message, expected_execution)).rowcount
        if count != 1:
            raise ExecutionLost("消息已过期或执行现场已失效")


def task_cancel(job_id, owner):
    with database.session(write=True) as c:
        return bool(c.execute(
            "UPDATE jobs SET cancel_requested=1,work_status=CASE WHEN work_status='pending' THEN 'cancelled' ELSE work_status END,updated=? "
            "WHERE id=? AND user_id=? AND work_status IN ('pending','running')",
            (time.time(), job_id, owner)).rowcount)


def reschedule(job_id, owner, *, phase=None, queue=None, metadata=None):
    """人工恢复同一业务任务；只生成新的步骤消息，不替换原输入。"""
    with database.session(write=True) as c:
        row = _row(c.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, owner)).fetchone())
        if not row or row["work_status"] not in ("failed", "needs_review", "cancelled"):
            raise ValueError("当前任务不能恢复")
        message_id = uuid.uuid4().hex
        c.execute("UPDATE jobs SET message_id=?,message_status='pending',work_status='pending',phase=?,queue=?,meta=?,"
                  "publish_attempts=0,next_publish_at=0,publish_error='',step_attempts=0,cancel_requested=0,execution_id='',updated=? "
                  "WHERE id=? AND user_id=?",
                  (message_id, phase or row["phase"], queue or row["queue"], json.dumps(metadata if metadata is not None else row["meta"], ensure_ascii=False), time.time(), job_id, owner))
        return job_load(job_id)


def retry_publish(job_id, owner):
    with database.session(write=True) as c:
        return bool(c.execute("UPDATE jobs SET message_status='pending',work_status='pending',publish_attempts=0,next_publish_at=0,publish_error='',updated=? "
                              "WHERE id=? AND user_id=? AND execution_id='' AND step_attempts=0 AND work_status IN ('pending','needs_review')",
                              (time.time(), job_id, owner)).rowcount)


def action_exists(owner, job_id, action, client_key):
    with database.session() as c:
        row = c.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?", (owner, client_key)).fetchone()
        if row and (row["job_id"] != job_id or row["action"] != action):
            raise ValueError("操作编号已用于其他任务")
        return dict(row) if row else None


def reserve_self_action(owner, job_id, action, client_key):
    with database.session(write=True) as c:
        count = c.execute("INSERT INTO station_task_actions(id,user_id,job_id,action,client_key,next_job_id,created) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,client_key) DO NOTHING",
                          (uuid.uuid4().hex, owner, job_id, action, client_key, job_id, time.time())).rowcount
        action_exists(owner, job_id, action, client_key)
        return bool(count)


def task_existing_jobs(user_id, ids):
    values = list(dict.fromkeys(ids))[:100]
    if not values:
        return set()
    with database.session() as c:
        return {r["id"] for r in c.execute("SELECT id FROM jobs WHERE user_id=? AND id IN (" + ",".join("?" for _ in values) + ")",
                                         (user_id, *values)).fetchall()}


def task_action_reserve(user_id, job_id, action, client_key, next_job_id):
    """原子预留手动重做 ID；所有参数仍来自原任务。"""
    with database.session(write=True) as c:
        suffix = " FOR UPDATE" if isinstance(c, PostgresConnection) else ""
        row = c.execute("SELECT meta FROM jobs WHERE id=? AND user_id=?" + suffix, (job_id, user_id)).fetchone()
        existing = c.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?", (user_id, client_key)).fetchone()
        if existing:
            if existing["job_id"] != job_id or existing["action"] != action:
                raise ValueError("操作编号已用于其他任务")
            return dict(existing)
        if not row:
            raise ValueError("原任务不存在")
        meta = json.loads(row["meta"])
        if meta.get("successor_id") and meta.get("successor_action") != action:
            raise ValueError("已有其他后续任务")
        next_job_id = meta.get("successor_id") or next_job_id
        meta.update(successor_id=next_job_id, successor_action=action)
        c.execute("UPDATE jobs SET meta=?,updated=? WHERE id=? AND user_id=?",
                  (json.dumps(meta, ensure_ascii=False), time.time(), job_id, user_id))
        c.execute("INSERT INTO station_task_actions(id,user_id,job_id,action,client_key,next_job_id,created) VALUES(?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, user_id, job_id, action, client_key, next_job_id, time.time()))
        return dict(c.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?", (user_id, client_key)).fetchone())
