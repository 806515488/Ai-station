"""现有 jobs 表升级为消息/工作双状态；旧未完成任务只核验，不自动重放。"""

JOB_COLUMNS = {
    "message_status": "TEXT NOT NULL DEFAULT 'pending'",
    "message_id": "TEXT NOT NULL DEFAULT ''",
    "phase": "TEXT NOT NULL DEFAULT 'legacy'",
    "queue": "TEXT NOT NULL DEFAULT ''",
    "meta": "TEXT NOT NULL DEFAULT '{}'",
    "execution_id": "TEXT NOT NULL DEFAULT ''",
    "publish_attempts": "INTEGER NOT NULL DEFAULT 0",
    "next_publish_at": "DOUBLE PRECISION NOT NULL DEFAULT 0",
    "publish_error": "TEXT NOT NULL DEFAULT ''",
    "step_attempts": "INTEGER NOT NULL DEFAULT 0",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
}


def upgrade_jobs(c, *, postgres=False):
    """只由显式 PG 升级或 SQLite 初始化调用；不删除历史控制表。"""
    if postgres:
        columns = {r["column_name"] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='jobs'").fetchall()}
    else:
        columns = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    legacy = "work_status" not in columns
    if legacy:
        c.execute("ALTER TABLE jobs RENAME COLUMN status TO work_status")
    for name, declaration in JOB_COLUMNS.items():
        if name not in columns:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
    if legacy:
        c.execute("UPDATE jobs SET work_status=CASE work_status WHEN 'done' THEN 'success' WHEN 'failed' THEN 'failed' WHEN 'cancelled' THEN 'cancelled' ELSE 'needs_review' END")
        if postgres:
            control = c.execute("SELECT to_regclass('public.station_task_control') AS present").fetchone()["present"]
        else:
            control = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='station_task_control'").fetchone()
        if control:
            c.execute("UPDATE jobs SET meta=COALESCE((SELECT record FROM station_task_control WHERE job_id=jobs.id),'{}'), cancel_requested=COALESCE((SELECT cancel_requested FROM station_task_control WHERE job_id=jobs.id),0)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_publish ON jobs(message_status,next_publish_at)")
