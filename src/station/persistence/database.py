"""统一连接、锁和事务；所有仓库共享同一个 Database 实例。

仓库通过 session() 读、session(write=True) 写，锁覆盖游标消费和完整事务。
业务服务可用 transaction() 包住多个仓库调用；内部复用连接，不提前提交。
嵌套调用失败后即使异常被捕获，外层也会回滚，避免提交一半业务数据。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import time

from station import config
from station.persistence.backends import postgres
from station.persistence.backends.sqlite import connect_sqlite
from station.persistence.errors import TransactionAborted


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else config.DATA_DIR / "station.db"
        self._connection = None
        self.lock = threading.RLock()
        self._local = threading.local()

    @staticmethod
    def now() -> float:
        return time.time()

    @property
    def in_transaction(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def backend(self) -> str:
        return "postgres" if isinstance(self._connection, postgres.PostgresConnection) else "sqlite"

    def reset(self, path: Path | None = None) -> None:
        """关闭连接并可重设 SQLite 路径；用于受控退出和测试隔离，不在事务中换库。"""
        with self.lock:
            if self.in_transaction:
                raise RuntimeError("事务中不能重置数据库连接")
            previous, self._connection = self._connection, None
            if previous is not None:
                previous.close()
            if path is not None:
                self.path = Path(path)

    def connection(self):
        """惰性建立连接；业务必须在 session/transaction 中使用返回的连接。"""
        with self.lock:
            if (isinstance(self._connection, postgres.PostgresConnection)
                    and postgres.raw_is_closed(self._connection.raw)):
                if self.in_transaction:
                    raise RuntimeError("事务连接已关闭，必须回滚后重新执行业务")
                previous, self._connection = self._connection, None
                try:
                    previous.close()
                except Exception:
                    pass
            if self._connection is None:
                url = config.database_url()
                candidate = None
                try:
                    if url:
                        from station.persistence.schema.postgres import init_postgres
                        candidate = postgres.connect_postgres(url, owner=self)
                        init_postgres(candidate)
                    else:
                        from station.persistence.schema.sqlite import init_db
                        candidate = connect_sqlite(self.path)
                        init_db(candidate)
                except ValueError as exc:
                    if candidate is not None:
                        candidate.close()
                    raise RuntimeError("STATION_DATABASE_URL 只支持 PostgreSQL 连接串") from exc
                except BaseException:
                    if candidate is not None:
                        candidate.close()
                    raise
                self._connection = candidate
            return self._connection

    def replace_broken_connection(self, failed):
        """只在事务外替换本实例持有的坏连接；写事务不换连接或重放语句。"""
        with self.lock:
            if self._connection is not failed or self.in_transaction:
                return None
            self._connection = None
            try:
                failed.close()
            except Exception:
                pass
            return self.connection()

    @contextmanager
    def session(self, *, write: bool = False):
        if write:
            with self.transaction() as connection:
                yield connection
        else:
            with self.lock:
                try:
                    yield self.connection()
                except BaseException:
                    if self.in_transaction:
                        self._local.rollback_only = True
                    raise

    @contextmanager
    def transaction(self):
        with self.lock:
            connection = self.connection()
            if self.in_transaction:
                self._local.depth += 1
                try:
                    yield connection
                except BaseException:
                    self._local.rollback_only = True
                    raise
                finally:
                    self._local.depth -= 1
                return
            self._local.depth = 1
            self._local.rollback_only = False
            try:
                if isinstance(connection, postgres.PostgresConnection):
                    with connection.raw.transaction():
                        yield connection
                        self._check_commit()
                else:
                    connection.execute("BEGIN")
                    try:
                        yield connection
                        self._check_commit()
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
            finally:
                self._local.depth = 0
                self._local.rollback_only = False

    def _check_commit(self):
        if self._local.rollback_only:
            raise TransactionAborted("内部操作失败，整个事务已回滚")


database = Database()
