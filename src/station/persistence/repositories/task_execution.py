"""执行互斥：PG 使用会话 advisory lock；SQLite 仅支持单进程离线执行。

锁由数据库连接生命期管理，不使用实例身份文件、任务租约或应用心跳。
Worker 断连后禁止旧执行静默重连，避免失去锁后继续写入业务表。
"""
from contextlib import contextmanager, nullcontext
import hashlib
import threading
import weakref

from station.persistence import database
from station.persistence.backends.postgres import PostgresConnection

_locks = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


@contextmanager
def exclusive(job_id):
    with _registry_lock:
        lock = _locks.setdefault(job_id, threading.Lock())
    if not lock.acquire(blocking=False):
        yield False
        return
    connection = None
    acquired = False
    key = int.from_bytes(hashlib.sha256(("station-job:" + job_id).encode()).digest()[:8], "big", signed=True)
    try:
        with database.session() as c:
            if isinstance(c, PostgresConnection):
                acquired = bool(c.execute("SELECT pg_try_advisory_lock(?) AS acquired", (key,)).fetchone()["acquired"])
                connection = database.connection()
            else:
                acquired = True
        if not acquired:
            yield False
            return
        guard = database.worker_connection(connection) if connection is not None else nullcontext()
        with guard:
            yield True
    finally:
        if acquired and connection is not None and not connection.raw.closed:
            try:
                with database.session() as current:
                    if current is connection:
                        connection.execute("SELECT pg_advisory_unlock(?)", (key,))
            except Exception:
                connection.close()
        lock.release()
