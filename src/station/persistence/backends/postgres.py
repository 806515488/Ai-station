"""PostgreSQL 驱动适配：列名行、参数占位符、断线识别与安全只读重试。

普通连接使用 autocommit；写操作由 Database 显式开启事务。事务中不更换连接，
也不重放任何语句。独立迁移连接没有 owner，错误交还给运维命令处理。
"""
from station import config


class PostgresConnection:
    def __init__(self, raw, owner=None):
        self.raw = raw
        self.owner = owner

    def execute(self, statement: str, params=None, *, _retried_after_disconnect: bool = False):
        statement = statement.replace("?", "%s")
        try:
            return self.raw.execute(statement, params or ())
        except Exception as error:
            active_transaction = self.owner is not None and self.owner.in_transaction
            if not active_transaction:
                try:
                    self.raw.rollback()
                except Exception:
                    pass
            replacement = None
            if self.owner is not None and not active_transaction and is_connection_lost(error, self.raw):
                replacement = self.owner.replace_broken_connection(self)
            if replacement is not None and not _retried_after_disconnect and is_safe_read(statement):
                return replacement.execute(statement, params, _retried_after_disconnect=True)
            raise

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def raw_is_closed(raw) -> bool:
    return bool(getattr(raw, "closed", False))


def is_connection_lost(error: Exception, raw) -> bool:
    if raw_is_closed(raw):
        return True
    if str(getattr(error, "sqlstate", "") or "").startswith("08"):
        return True
    return any(token in str(error).lower() for token in (
        "connection is closed", "server closed the connection", "connection reset by peer",
        "terminating connection", "ssl connection has been closed", "consuming input failed",
    ))


def is_safe_read(statement: str) -> bool:
    sql = (statement or "").lstrip().upper()
    return sql.startswith("SELECT") and "FOR UPDATE" not in sql and "FOR SHARE" not in sql


def connect_postgres(url: str, *, owner=None) -> PostgresConnection:
    """建立独立连接；只有 Database 提供 owner 时才启用共享连接的安全重连。"""
    if not (url or "").startswith(("postgresql://", "postgres://")):
        raise ValueError("PostgreSQL 连接串必须以 postgresql:// 或 postgres:// 开头")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError("未安装 PostgreSQL 驱动，请安装 psycopg[binary]") from error
    connection = PostgresConnection(psycopg.connect(
        url, row_factory=dict_row, autocommit=True,
        connect_timeout=10, keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3,
    ), owner=owner)
    try:
        connection.execute("SELECT set_config('lock_timeout', ?, false)",
                           (f"{config.database_lock_timeout_ms()}ms",))
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise
