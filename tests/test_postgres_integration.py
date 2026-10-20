"""真 PostgreSQL 集成测试 —— **默认跳过**，只有给了 STATION_TEST_DATABASE_URL 才跑。

为什么这个文件必须存在
----------------------
`tests/conftest.py` 会把 `STATION_DATABASE_URL` / `DATABASE_URL` 删掉，让**离线**
测试永远只碰临时 SQLite（这是对的：CI 上不该有数据库服务）。代价是 PostgreSQL
那半边**一条都没被跑过** —— `_postgres_schema()` 的 DDL、`init_postgres`、
`checkpoint_add` 的 `RETURNING` 分支、`ref_link_add` 的 `ON CONFLICT` 分支、
`connect_postgres`，以及 `migrate()` 真正往库里写那条路。

而本仓库自己的规矩是：**"替身证明不了假设本身，必须真跑一次"**（见 docs/status.md
「验证」一节）。所以这些路径要有一个能真连上 PG 的测试来钉。

用法::

    STATION_TEST_DATABASE_URL='postgresql://station:<口令>@127.0.0.1:5432/station_test' \
        python -m pytest tests/test_postgres_integration.py -q

★ 目标库会被本文件**反复清空**，务必指向一次性的 station_test，绝不要指生产库。
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from station import db, migrate

_REQUIRED_URL = "STATION_TEST_DATABASE_URL"

# 与 _postgres_schema() 应当建出来的东西一一对应
_EXPECTED_TABLES = {
    "users", "threads", "jobs", "archive_states", "corrections",
    "checkpoints", "model_configs", "mcp_servers", "ref_links",
    "interviews", "interview_recordings", "interview_questions",
    "interview_question_spans", "interview_reviews", "interview_actions",
    "station_documents", "station_attachment_turns", "station_traces", "station_trace_spans",
}
_EXPECTED_INDEXES = {
    "idx_threads_user", "idx_jobs_user", "idx_states_user",
    "idx_ck_project", "idx_ref_expires",
    "idx_interviews_user_updated", "idx_recordings_interview_sequence",
    "idx_questions_interview_sequence", "idx_question_spans_question",
    "idx_reviews_question_version", "idx_actions_interview_status_updated",
    "idx_traces_thread", "idx_trace_spans_trace",
}


def _test_url() -> str:
    url = (os.environ.get(_REQUIRED_URL) or "").strip()
    if not url:
        pytest.skip(f"未设 {_REQUIRED_URL} —— 跳过真 PG 集成测试（离线套件不受影响）")
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        raise RuntimeError(
            f"{_REQUIRED_URL} 必须指向一次性的 *_test 库（本测试会清空它）：{url!r}")
    return url


def _wipe(url: str) -> None:
    """把目标库清空：本测试每次都从零开始，才谈得上"空库守卫"那些断言。"""
    c = db.connect_postgres(url)
    try:
        for table in _EXPECTED_TABLES:
            c.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        c.commit()
    finally:
        c.close()


@pytest.fixture
def pg(monkeypatch):
    """指向真 PG 的 station.db；用完清场并把全局连接复位。"""
    url = _test_url()
    monkeypatch.setenv("STATION_DATABASE_URL", url)
    db._conn = None                     # 丢掉可能存在的 SQLite 单例
    _wipe(url)
    yield url
    db._conn = None                     # 之后（含离线套件）重新走 SQLite 分支
    _wipe(url)


def _make_sqlite(tmp_path, *, legacy_users: bool = False):
    """造一个 SQLite 源库；legacy_users=True 时把 users 退回到"没有 seq"的老结构。

    老结构那条路必须测：线上/本机可能就躺着一个加 seq 之前建的 station.db，
    照直 `SELECT seq` 会 "no such column"。
    """
    path = tmp_path / "source.db"
    conn = sqlite3.connect(str(path))
    db.init_db(conn)                    # 先用现成 DDL 把 9 张表都建齐
    conn.commit()
    if legacy_users:
        # SQLite 不好删列，就直接重建这张表成老样子
        conn.executescript("""
        DROP TABLE users;
        CREATE TABLE users(
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL, created REAL NOT NULL
        );
        """)
        conn.commit()
    return path, conn


def _insert_user(conn, uid: str, name: str, *, legacy: bool, created: float = 1.0,
                 seq: int = 1) -> None:
    if legacy:
        conn.execute("INSERT INTO users(id,name,pw_hash,salt,created) VALUES(?,?,?,?,?)",
                     (uid, name, "h", "s", created))
    else:
        conn.execute(
            "INSERT INTO users(id,name,pw_hash,salt,created,seq) VALUES(?,?,?,?,?,?)",
            (uid, name, "h", "s", created, seq))


# ── 建表 ────────────────────────────────────────────────────────────────

def test_init_postgres_creates_schema_and_is_idempotent(pg):
    """建表要全、要幂等（第二次调用必须空转，不报错也不丢数据）。"""
    c = db.connect_postgres(pg)
    try:
        db.init_postgres(c)
        db.init_postgres(c)
        tables = {r["table_name"] for r in c.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'").fetchall()}
        indexes = {r["indexname"] for r in c.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'").fetchall()}
    finally:
        c.close()
    assert _EXPECTED_TABLES <= tables, f"缺表：{_EXPECTED_TABLES - tables}"
    assert _EXPECTED_INDEXES <= indexes, f"缺索引：{_EXPECTED_INDEXES - indexes}"


def test_existing_schema_startup_only_reads_metadata(pg):
    """★ 回归护栏：已有 PG 绝不能因一次登录再跑 CREATE/ALTER。"""
    c = db.connect_postgres(pg)
    try:
        db.init_postgres(c)                 # 空库的首次 bootstrap
        seen = []
        original = c.execute

        def spy(statement, params=None):
            seen.append(statement)
            return original(statement, params)

        c.execute = spy
        db.init_postgres(c)                 # 既有 schema：只能读 information_schema
    finally:
        c.close()
    assert all(not sql.lstrip().upper().startswith(("CREATE", "ALTER", "LOCK"))
               for sql in seen), seen


def test_existing_schema_upgrade_requires_explicit_command(pg):
    """旧 schema 不能被 Web 启动偷偷升级；显式升级才可以执行 ALTER。"""
    c = db.connect_postgres(pg)
    try:
        db.init_postgres(c)
        c.execute("ALTER TABLE users DROP COLUMN seq")
        with pytest.raises(RuntimeError, match="station-upgrade-postgres"):
            db.init_postgres(c)
        assert db.upgrade_postgres_schema(c) == "upgraded"
        assert c.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' AND column_name='seq') "
            "AS present").fetchone()["present"] is True
    finally:
        c.close()


def test_conn_really_switches_backend_to_postgres(pg):
    """给了 URL 之后 conn()/backend() 必须真走 PG（而不是还在用 SQLite）。"""
    db.conn()
    assert db.backend() == "postgres"
    assert db.conn().raw.autocommit is True
    assert db.conn().execute(
        "SELECT 1 AS one").fetchone()["one"] == 1


# ── 业务 SQL 里两处 PG 专有分支 ─────────────────────────────────────────

def test_create_user_assigns_insert_order_seq(pg):
    """seq 按注册顺序递增 —— owner_id() 就靠它分先后。"""
    a = db.create_user("first", "pw")
    b = db.create_user("second", "pw")
    rows = {r["id"]: r["seq"] for r in
            db.conn().execute("SELECT id,seq FROM users").fetchall()}
    assert rows[a["id"]] < rows[b["id"]]


def test_duplicate_user_raises_valueerror(pg):
    """重名转成人话 ValueError —— 靠 sqlstate 23505 认，不靠异常类名。"""
    db.create_user("dup", "pw")
    with pytest.raises(ValueError, match="用户名已存在"):
        db.create_user("dup", "pw")


def test_owner_is_earliest_by_seq_even_when_created_ties(pg):
    """★ 回归护栏：created 打平时，owner 必须是**先注册的那个**。

    原实现靠 SQLite 的 rowid 当平局判据；换 PG 后若拿随机 uuid 的 id 顶替，
    同刻注册的两个账号就分不出先后 —— 而 owner 就是管理员，挑错等于把管理权送人。
    """
    first = db.create_user("owner-candidate", "pw")
    second = db.create_user("later", "pw")
    tie = 1_700_000_000.0
    c = db.conn()
    c.execute("UPDATE users SET created=? WHERE id=?", (tie, first["id"]))
    c.execute("UPDATE users SET created=? WHERE id=?", (tie, second["id"]))
    c.commit()
    assert db.owner_id() == first["id"]


def test_checkpoint_add_returns_new_id(pg):
    """checkpoint_add 在 PG 上走 INSERT ... RETURNING id（不是 lastrowid）。"""
    db.conn()
    first = db.checkpoint_add("proj", "u1", "第一版", {"a": 1})
    second = db.checkpoint_add("proj", "u1", "第二版", {"a": 2})
    assert isinstance(first, int) and isinstance(second, int)
    assert second > first, "自增号必须递增（说明 identity 序列在走）"
    labels = [r["label"] for r in db.checkpoints_list("proj")]
    assert labels == ["第二版", "第一版"], "列表是新→旧"


def test_ref_link_add_upserts_same_token(pg):
    """ref_link_add 在 PG 上走 ON CONFLICT DO UPDATE（同 token 覆盖，不报错不重复）。"""
    db.conn()
    db.ref_link_add("tok", "fid-1", "u1", 1000.0)
    db.ref_link_add("tok", "fid-2", "u2", 2000.0)
    rows = db.conn().execute(
        "SELECT fid,user_id FROM ref_links WHERE token=?", ("tok",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fid"] == "fid-2" and rows[0]["user_id"] == "u2"


def test_interview_records_round_trip_on_postgres(pg):
    """面试档案第一期在真 PG 上能写入多段录音、复盘版本和行动项。"""
    user = db.create_user("interview-pg", "pw")
    interview = db.interview_create(user["id"], "A公司·一面")
    first = db.interview_recording_add(interview["id"], user["id"], "audio-a", "上半场.m4a")
    second = db.interview_recording_add(interview["id"], user["id"], "audio-b", "下半场.m4a")
    assert [r["sequence"] for r in db.interview_recordings(interview["id"], user["id"])] == [1, 2]

    db.interview_recording_start(first["id"], user["id"], "asr-job-a")
    done = db.interview_recording_finish(first["id"], user["id"], transcript_file_id="transcript-a")
    assert done["status"] == "done"

    questions = db.interview_question_add_many(interview["id"], user["id"], [
        {"question_text": "你解决过什么难题？", "answer_text": "我先定位影响范围。"},
    ])
    review = db.interview_review_add(questions[0]["id"], user["id"], coach_feedback="补充结果")
    action = db.interview_action_add(interview["id"], user["id"], "重答项目难题", question_id=questions[0]["id"])
    assert review["version"] == 1
    assert db.interview_action_set_status(action["id"], user["id"], "done")["status"] == "done"
    assert db.interview_recording_get(second["id"], user["id"])["status"] == "uploaded"


# ── 迁移工具全链路 ──────────────────────────────────────────────────────

def test_migrate_imports_and_verifies(pg, tmp_path):
    """SQLite → PG 全链路：导得进、行数对得上、内容没串。"""
    path, src = _make_sqlite(tmp_path)
    _insert_user(src, "u-1", "张三", legacy=False)
    src.execute("INSERT INTO threads(id,user_id,skill_id,msgs,meta,created,updated) "
                "VALUES(?,?,?,?,?,?,?)", ("t-1", "u-1", "archive", "[]", "{}", 1.0, 1.0))
    src.commit()
    src.close()

    counts = migrate.migrate(path, pg)
    assert counts["users"] == 1 and counts["threads"] == 1

    c = db.connect_postgres(pg)
    try:
        assert c.execute("SELECT name FROM users").fetchone()["name"] == "张三"
        assert c.execute("SELECT COUNT(*) AS n FROM threads").fetchone()["n"] == 1
    finally:
        c.close()


def test_migrate_refuses_nonempty_target(pg, tmp_path):
    """目标非空 = 拒绝覆盖：迁移是"只进空库"，第二次跑必须拦住。"""
    path, src = _make_sqlite(tmp_path)
    _insert_user(src, "u-1", "张三", legacy=False)
    src.commit()
    src.close()

    migrate.migrate(path, pg)
    with pytest.raises(RuntimeError, match="不是空库"):
        migrate.migrate(path, pg)


def test_migrate_legacy_sqlite_without_seq_uses_rowid(pg, tmp_path):
    """★ 兼容护栏：加 seq 之前的旧 station.db（users 没有 seq 列）也要迁得动。"""
    path, src = _make_sqlite(tmp_path, legacy_users=True)
    _insert_user(src, "old-1", "老账号", legacy=True)
    _insert_user(src, "old-2", "新账号", legacy=True)
    src.commit()
    src.close()

    migrate.migrate(path, pg)
    c = db.connect_postgres(pg)
    try:
        seqs = {r["name"]: r["seq"] for r in
                c.execute("SELECT name,seq FROM users").fetchall()}
    finally:
        c.close()
    assert seqs["老账号"] < seqs["新账号"], "老库的插入序必须由 rowid 顶上来"
