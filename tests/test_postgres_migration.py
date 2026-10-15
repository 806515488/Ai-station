"""PostgreSQL 切换前的离线护栏：不需要真 PostgreSQL 服务。"""
from __future__ import annotations

from station import config, db
from station import migrate


def test_database_url_prefers_station_specific_name(monkeypatch):
    """站内变量优先，兼容通用 DATABASE_URL；读环境前不许遗留测试外的 .env 影响。"""
    monkeypatch.setattr(config, "load_env", lambda: {})
    monkeypatch.setenv("DATABASE_URL", "postgresql://generic")
    monkeypatch.setenv("STATION_DATABASE_URL", "postgresql://station")
    assert config.database_url() == "postgresql://station"
    monkeypatch.delenv("STATION_DATABASE_URL")
    assert config.database_url() == "postgresql://generic"


def test_database_lock_timeout_defaults_and_rejects_bad_values(monkeypatch):
    monkeypatch.setattr(config, "load_env", lambda: {})
    monkeypatch.delenv("STATION_DB_LOCK_TIMEOUT_MS", raising=False)
    assert config.database_lock_timeout_ms() == 5000
    monkeypatch.setenv("STATION_DB_LOCK_TIMEOUT_MS", "1200")
    assert config.database_lock_timeout_ms() == 1200
    monkeypatch.setenv("STATION_DB_LOCK_TIMEOUT_MS", "0")
    try:
        config.database_lock_timeout_ms()
    except ValueError as e:
        assert "大于 0" in str(e)
    else:
        raise AssertionError("0 毫秒不应被接受")


def test_postgres_wrapper_translates_only_dbapi_placeholders():
    """旧业务 SQL 的问号参数必须在送到 psycopg 前变成 %s。"""
    class Raw:
        def __init__(self):
            self.got = None

        def execute(self, sql, args):
            self.got = (sql, args)
            return self

    raw = Raw()
    db._PostgresConnection(raw).execute("SELECT * FROM x WHERE a=? AND b=?", (1, 2))
    assert raw.got == ("SELECT * FROM x WHERE a=%s AND b=%s", (1, 2))


def test_postgres_wrapper_rolls_back_after_a_failed_statement():
    """一次 SQL 失败不能永久毒死复用连接。"""
    class Raw:
        def __init__(self):
            self.rolled_back = False

        def execute(self, sql, args):
            raise RuntimeError("deadlock detected")

        def rollback(self):
            self.rolled_back = True

    raw = Raw()
    try:
        db._PostgresConnection(raw).execute("SELECT 1")
    except RuntimeError as e:
        assert str(e) == "deadlock detected"
    else:
        raise AssertionError("底层异常应继续抛给当前请求")
    assert raw.rolled_back is True


def test_postgres_read_reconnects_once_after_server_closes_connection(monkeypatch):
    """远程 PG 断线后，鉴权等 SELECT 应自动换连接，不许把全站永久打成 500。"""
    class GoneRaw:
        closed = False

        def __init__(self):
            self.closed = False
            self.closed_by_us = False

        def execute(self, _sql, _args):
            self.closed = True
            raise RuntimeError("server closed the connection unexpectedly")

        def rollback(self):
            pass

        def close(self):
            self.closed_by_us = True

    class FreshRaw:
        closed = False

        def __init__(self):
            self.calls = 0

        def execute(self, _sql, _args):
            self.calls += 1
            return "fresh-result"

        def rollback(self):
            pass

        def close(self):
            pass

    old = db._PostgresConnection(GoneRaw())
    fresh_raw, fresh = FreshRaw(), None
    fresh = db._PostgresConnection(fresh_raw)
    monkeypatch.setattr(db, "_conn", old)
    monkeypatch.setattr(config, "database_url", lambda: "postgresql://station")
    monkeypatch.setattr(db, "connect_postgres", lambda _url: fresh)
    monkeypatch.setattr(db, "init_postgres", lambda _conn: None)

    assert old.execute("SELECT id FROM users WHERE id=?", ("u1",)) == "fresh-result"
    assert db._conn is fresh and fresh_raw.calls == 1
    assert old.raw.closed_by_us is True


def test_postgres_write_reconnects_for_next_request_but_never_replays_write(monkeypatch):
    """断线时写操作的提交结果不确定：换连接可以，重放 INSERT 不可以。"""
    class GoneRaw:
        closed = False

        def execute(self, _sql, _args):
            self.closed = True
            raise RuntimeError("connection is closed")

        def rollback(self):
            pass

        def close(self):
            pass

    class FreshRaw:
        closed = False

        def __init__(self):
            self.calls = 0

        def execute(self, _sql, _args):
            self.calls += 1
            return self

        def rollback(self):
            pass

        def close(self):
            pass

    old = db._PostgresConnection(GoneRaw())
    fresh_raw = FreshRaw()
    fresh = db._PostgresConnection(fresh_raw)
    monkeypatch.setattr(db, "_conn", old)
    monkeypatch.setattr(config, "database_url", lambda: "postgresql://station")
    monkeypatch.setattr(db, "connect_postgres", lambda _url: fresh)
    monkeypatch.setattr(db, "init_postgres", lambda _conn: None)

    try:
        old.execute("INSERT INTO users(id) VALUES(?)", ("u1",))
    except RuntimeError as e:
        assert "connection is closed" in str(e)
    else:
        raise AssertionError("断线写操作不应自动重放")
    assert db._conn is fresh and fresh_raw.calls == 0


def test_migration_dry_run_reads_the_current_sqlite_schema():
    """迁移工具先只读源库，能认全当前表；dry-run 不需要 PG 地址、更不会写目标库。"""
    db.create_user("postgres-dry-run", "pw")
    counts = migrate.migrate(db.DB_PATH, "", dry_run=True)
    assert counts["users"] == 1
    assert set(counts) == {name for name, _ in migrate._TABLES}
