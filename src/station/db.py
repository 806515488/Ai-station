"""station.db —— 宿主持久层（PostgreSQL 生产库 + SQLite 离线测试/迁移源）。

为什么上 SQLite（用户决策 09-06）：页面一刷新"啥都没了"的体验不能忍。
此前 Thread/Job 其实都落了盘（JSON 文件），但没有"用户"概念——谁开的会话、
谁的档案卷无从谈起；前端刷新后也不恢复。引入 users 表 + 各数据表挂 user_id，
并把散落的 JSON 存取收拢到一个连接助手这里。

表（见 init_db 的建表语句）：
  users    用户：name 唯一 + pw_hash（盐化 sha256；本地单机站，不追求对抗顶级攻击，
           但绝不存明文）。首名自动建为管理员身份（本站无角色区分，仅做记录）。
  threads  会话：msgs/meta 存 JSON 文本；skill_id + user_id 归属。
  jobs     任务：status/progress/log/artifacts/result 存 JSON 文本；user_id 归属。

新手视角（Java 朋友版）：
  - 生产环境设置 STATION_DATABASE_URL 后使用 PostgreSQL；SQLite 仅保留给离线测试、
    本机轻量使用，以及把旧 station.db 导入 PostgreSQL 的迁移源。
  - 目前仍用一把 RLock 串行化本进程的连接访问：这是为了保持既有线程安全契约，
    不是 PostgreSQL 的并发上限。将来需要多个 Web worker 时再改成连接池。
  - 两种后端都返回可按列名取值的行（row["name"]），上层业务不需要知道数据库品牌。
"""
from __future__ import annotations

import hashlib     # 密码摘要（sha256 + 每用户随机盐）
import json
import secrets     # 生成随机盐
import sqlite3
import threading
import time
import uuid
from typing import Any

from station import config

DB_PATH = config.DATA_DIR / "station.db"     # 库文件：data/station/station.db
_conn: Any = None
# 全站**数据库互斥锁**。★ 它不只管写，读也必须拿 —— 全站共用一个 sqlite3 连接，
# 而"一个连接被多线程同时用"是**未定义行为**：实测 1.5 秒里能撞出 783 次异常
# （sqlite3.InterfaceError: bad parameter / no more rows available），**而且读到的行
# 会是错乱的**（json.loads 收到 None、行元组长度不对）——不只是报错，是数据看起来
# "莫名其妙没了"。用 RLock：万一某个已持锁的函数又调了另一个拿锁的函数，同线程重入
# 不会死锁。
_lock = threading.RLock()


def _now() -> float:
    return time.time()


class _PostgresConnection:
    """给 psycopg 连接补一层很薄的 SQLite 兼容接口。

    历史业务 SQL 使用 DB-API 的 ``?`` 占位符；psycopg 使用 ``%s``。转换只发生在
    本模块固定的参数化 SQL 上，绝不拼接业务输入。这样迁移后上层函数仍只需要调用
    ``conn().execute(...).fetchone()``。
    """

    def __init__(self, raw):
        self.raw = raw

    def execute(self, statement: str, params=None, *, _retried_after_disconnect: bool = False):
        statement = statement.replace("?", "%s")
        try:
            return self.raw.execute(statement, params or ())
        except Exception as e:
            # psycopg 的一个语句失败会让当前事务进入 aborted；站内连接是复用的，
            # 不回滚的话一次死锁会把后续所有请求都变成 InFailedSqlTransaction。
            try:
                self.raw.rollback()
            except Exception:
                # 若连接已被服务端断开，保留原始 SQL 异常供调用方判断，别让清理异常盖掉它。
                pass
            # PG 是进程外服务：容器滚动重启、网络短抖或 NAT 清掉空闲 TCP 都会让
            # 这条长连接失效。过去这里只抛错，全局 _conn 仍指向坏对象，之后每个
            # 登录/鉴权请求都会 500，直到人工重启 Web。现在丢弃坏连接：安全的只读
            # SELECT 自动在新连接重放一次；写入**绝不重放**，以免连接断开时把“其实
            # 已提交但客户端没收到响应”的写操作做第二遍。
            replacement = _replace_broken_global_connection(self) \
                if _is_connection_lost(e, self.raw) else None
            if (replacement is not None and not _retried_after_disconnect
                    and _is_safe_read(statement)):
                return replacement.execute(statement, params,
                                          _retried_after_disconnect=True)
            raise

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def _raw_is_closed(raw) -> bool:
    """psycopg 用 bool，测试替身可能没有该属性；两边都安全识别。"""
    return bool(getattr(raw, "closed", False))


def _is_connection_lost(err: Exception, raw) -> bool:
    """这是“换连接才会好”的错误，不是死锁/语法错误等业务 SQL 错。"""
    if _raw_is_closed(raw):
        return True
    state = str(getattr(err, "sqlstate", "") or "")
    if state.startswith("08"):            # SQLSTATE 08xxx = connection_exception
        return True
    text = str(err).lower()
    return any(token in text for token in (
        "connection is closed", "server closed the connection", "connection reset by peer",
        "terminating connection", "ssl connection has been closed", "consuming input failed",
    ))


def _is_safe_read(statement: str) -> bool:
    """仅重放无副作用的普通 SELECT；写入的提交结果在断线时不可判定。"""
    sql = (statement or "").lstrip().upper()
    return sql.startswith("SELECT") and "FOR UPDATE" not in sql and "FOR SHARE" not in sql


def _replace_broken_global_connection(failed: _PostgresConnection):
    """仅替换本站全局连接；迁移工具的私有连接仍把原始异常交给调用方。"""
    global _conn
    with _lock:
        if _conn is not failed:
            return None                     # 已由别的请求替换，不抢它的新连接
        _conn = None                         # 先摘引用：conn() 才会真正新建连接
        try:
            failed.close()
        except Exception:                    # 已断的 close 不该盖掉原始 SQL 异常
            pass
        return conn()                        # RLock 可重入；会做只读 schema 校验再发布


def backend() -> str:
    """返回当前进程实际使用的后端名（``postgres`` 或 ``sqlite``）。"""
    return "postgres" if isinstance(_conn, _PostgresConnection) else "sqlite"


def connect_postgres(url: str) -> _PostgresConnection:
    """建立一个独立的 PostgreSQL 连接，供一次性迁移工具使用。"""
    if not (url or "").startswith(("postgresql://", "postgres://")):
        raise ValueError("PostgreSQL 连接串必须以 postgresql:// 或 postgres:// 开头")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as e:  # pragma: no cover - 依赖声明已保证 Docker 会安装
        raise RuntimeError("未安装 PostgreSQL 驱动，请安装 psycopg[binary]") from e
    # 业务层每一条读写本就独立提交。保留 psycopg 默认事务模式会让连 SELECT 也
    # 留下 idle in transaction；共享连接因此把读锁带进后续请求，极易与 DDL 相撞。
    # 迁移需要原子性时会显式进入 raw.transaction()。
    c = _PostgresConnection(psycopg.connect(
        url, row_factory=dict_row, autocommit=True,
        # 本机连远程 PG 时，路由器/NAT 可能静默回收空闲 TCP；主动保活让坏连接尽早
        # 被发现。connect_timeout 则防止数据库不可达时 HTTP worker 无限挂住。
        connect_timeout=10, keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3))
    try:
        # 每个连接单独设锁等待上限：遗留事务占住表时，登录应尽快得到明确错误，
        # 而不是让 HTTP worker 无限挂住。set_config 的参数化形式不拼接环境变量。
        c.execute("SELECT set_config('lock_timeout', ?, false)",
                  (f"{config.database_lock_timeout_ms()}ms",))
        c.commit()
        return c
    except Exception:
        c.close()  # 初始化失败不能留下 idle in transaction 的半成品连接
        raise


def conn():
    """取全站唯一连接；有 PostgreSQL URL 时优先连 PG，否则使用 SQLite。"""
    global _conn
    with _lock:
        # 服务端已经明确关闭的连接无需等下一条 SQL 再报一次错；直接在请求入口换新。
        if isinstance(_conn, _PostgresConnection) and _raw_is_closed(_conn.raw):
            stale, _conn = _conn, None
            try:
                stale.close()
            except Exception:
                pass
        if _conn is None:
            url = config.database_url()
            if url:
                candidate = None
                try:
                    candidate = connect_postgres(url)
                    init_postgres(candidate)
                except ValueError as e:
                    raise RuntimeError("STATION_DATABASE_URL 只支持 PostgreSQL 连接串") from e
                except Exception:
                    if candidate is not None:
                        candidate.close()
                    raise
                _conn = candidate  # 只有建表/补列成功后才能发布给全站使用
            else:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                # WAL 模式：读写不互斥（浏览器轮询时 worker 在写，不卡读）
                _conn.execute("PRAGMA journal_mode=WAL")
                init_db(_conn)
        return _conn


def _interview_schema() -> tuple[str, ...]:
    """面试档案表：PG 与 SQLite 同字段，长音频/全文转写只存文件 id，不进数据库。"""
    return (
        "CREATE TABLE IF NOT EXISTS interviews("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT, "
        "title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', "
        "round TEXT NOT NULL DEFAULT '', occurred_at DOUBLE PRECISION, "
        "status TEXT NOT NULL DEFAULT 'draft', speaker_map TEXT NOT NULL DEFAULT '{}', "
        "summary TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}', "
        "created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS interview_recordings("
        "id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE, "
        "file_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', sequence INTEGER NOT NULL, "
        "duration_ms BIGINT, asr_job_id TEXT NOT NULL DEFAULT '', transcript_file_id TEXT NOT NULL DEFAULT '', "
        "asr_model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'uploaded', "
        "error_message TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL, "
        "updated DOUBLE PRECISION NOT NULL, UNIQUE(interview_id, sequence))",
        "CREATE TABLE IF NOT EXISTS interview_questions("
        "id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE, "
        "sequence INTEGER NOT NULL, category TEXT NOT NULL DEFAULT '', question_text TEXT NOT NULL DEFAULT '', "
        "answer_text TEXT NOT NULL DEFAULT '', followup_text TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'draft', tags TEXT NOT NULL DEFAULT '[]', "
        "created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL, "
        "UNIQUE(interview_id, sequence))",
        "CREATE TABLE IF NOT EXISTS interview_question_spans("
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
        "question_id TEXT NOT NULL REFERENCES interview_questions(id) ON DELETE CASCADE, "
        "recording_id TEXT NOT NULL REFERENCES interview_recordings(id) ON DELETE CASCADE, "
        "start_ms BIGINT NOT NULL, end_ms BIGINT NOT NULL, speaker_role TEXT NOT NULL DEFAULT '', "
        "created DOUBLE PRECISION NOT NULL, CHECK(end_ms >= start_ms))",
        "CREATE TABLE IF NOT EXISTS interview_reviews("
        "id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES interview_questions(id) ON DELETE CASCADE, "
        "version INTEGER NOT NULL, self_reflection TEXT NOT NULL DEFAULT '', "
        "coach_feedback TEXT NOT NULL DEFAULT '', improved_answer TEXT NOT NULL DEFAULT '', "
        "drill_prompt TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'ai', "
        "created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL, "
        "UNIQUE(question_id, version))",
        "CREATE TABLE IF NOT EXISTS interview_actions("
        "id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE, "
        "question_id TEXT REFERENCES interview_questions(id) ON DELETE SET NULL, "
        "title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'todo', "
        "due_at DOUBLE PRECISION, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_interviews_user_updated ON interviews(user_id, updated DESC)",
        "CREATE INDEX IF NOT EXISTS idx_recordings_interview_sequence ON interview_recordings(interview_id, sequence)",
        "CREATE INDEX IF NOT EXISTS idx_questions_interview_sequence ON interview_questions(interview_id, sequence)",
        "CREATE INDEX IF NOT EXISTS idx_question_spans_question ON interview_question_spans(question_id, recording_id, start_ms)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_question_version ON interview_reviews(question_id, version DESC)",
        "CREATE INDEX IF NOT EXISTS idx_actions_interview_status_updated ON interview_actions(interview_id, status, updated DESC)",
    )


def _document_schema() -> tuple[str, ...]:
    """附件元数据（两种数据库共用）；大正文、JSONL 和原件留在数据卷。"""
    return (
        "CREATE TABLE IF NOT EXISTS station_documents("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, upload_key TEXT NOT NULL, "
        "local_date TEXT NOT NULL, sequence INTEGER NOT NULL, record TEXT NOT NULL, "
        "created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL, "
        "UNIQUE(user_id,upload_key), UNIQUE(user_id,local_date,sequence))",
        "CREATE TABLE IF NOT EXISTS station_attachment_turns("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
        "client_key TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL, "
        "created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL, "
        "UNIQUE(user_id,client_key))",
        "CREATE INDEX IF NOT EXISTS idx_documents_user_date "
        "ON station_documents(user_id,local_date,sequence)",
        "CREATE INDEX IF NOT EXISTS idx_attachment_turns_thread "
        "ON station_attachment_turns(user_id,thread_id,created)",
    )


def _trace_schema() -> tuple[str, ...]:
    """执行追踪只存有界元数据；正文仍属于会话/产物，不复制进追踪表。"""
    return (
        "CREATE TABLE IF NOT EXISTS station_traces("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
        "correlation_key TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, "
        "updated DOUBLE PRECISION NOT NULL, record TEXT NOT NULL, UNIQUE(user_id,correlation_key))",
        "CREATE TABLE IF NOT EXISTS station_trace_spans("
        "id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES station_traces(id) ON DELETE CASCADE, "
        "created DOUBLE PRECISION NOT NULL, record TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_traces_thread ON station_traces(user_id,thread_id,created)",
        "CREATE INDEX IF NOT EXISTS idx_trace_spans_trace ON station_trace_spans(trace_id,created)",
    )


def _task_schema() -> tuple[str, ...]:
    return (
        "CREATE TABLE IF NOT EXISTS station_task_control("
        "job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, user_id TEXT NOT NULL, "
        "instance_id TEXT NOT NULL DEFAULT '', boot_id TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '', "
        "lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0, "
        "record TEXT NOT NULL DEFAULT '{}', updated DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS station_task_actions("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT NOT NULL, action TEXT NOT NULL, "
        "client_key TEXT NOT NULL, next_job_id TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, "
        "UNIQUE(user_id,client_key))",
        "CREATE INDEX IF NOT EXISTS idx_task_control_user ON station_task_control(user_id,updated)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_created ON jobs(user_id,created DESC,id DESC)",
    )


def _postgres_schema() -> tuple[str, ...]:
    """PostgreSQL 的初始 DDL（新库首次启动自动建表，幂等）。

    JSON 目前仍存 TEXT：上层统一 ``json.loads``，能无损导入旧 SQLite；未来真要按 JSON
    字段检索时，再用独立迁移把特定列改成 JSONB，而不是在首次切库时混入数据形状变更。
    """
    core = (
        # seq = 插入序，owner_id() 的平局判据（与 init_db 里 SQLite 那侧同义）
        "CREATE TABLE IF NOT EXISTS users("
        "id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, "
        "salt TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, "
        "seq BIGINT NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS threads("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, skill_id TEXT NOT NULL, "
        "msgs TEXT NOT NULL, meta TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, "
        "updated DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS jobs("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, skill_id TEXT NOT NULL, "
        "args TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, "
        "message TEXT NOT NULL DEFAULT '', log TEXT NOT NULL, artifacts TEXT NOT NULL, "
        "result TEXT, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS archive_states("
        "project TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, person TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, "
        "updated DOUBLE PRECISION NOT NULL, PRIMARY KEY (project, channel))",
        "CREATE TABLE IF NOT EXISTS corrections("
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, project TEXT NOT NULL, "
        "user_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, "
        "ts DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS checkpoints("
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, project TEXT NOT NULL, "
        "channel TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL, label TEXT NOT NULL, "
        "state TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS model_configs("
        "user_id TEXT PRIMARY KEY, cfg TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS mcp_servers("
        "user_id TEXT PRIMARY KEY, cfg TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS ref_links("
        "token TEXT PRIMARY KEY, fid TEXT NOT NULL, user_id TEXT NOT NULL, "
        "expires DOUBLE PRECISION NOT NULL, created DOUBLE PRECISION NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id, updated)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, updated)",
        "CREATE INDEX IF NOT EXISTS idx_states_user ON archive_states(user_id, updated)",
        "CREATE INDEX IF NOT EXISTS idx_ck_project ON checkpoints(project, id)",
        "CREATE INDEX IF NOT EXISTS idx_ref_expires ON ref_links(expires)",
    )
    return core + _interview_schema() + _document_schema() + _trace_schema() + _task_schema()


_PG_SCHEMA_LOCK = 741_267_945
_PG_CORE_TABLES = {"users", "threads", "jobs", "archive_states", "corrections", "checkpoints",
                   "model_configs", "mcp_servers", "ref_links"}
_PG_INTERVIEW_TABLES = {"interviews", "interview_recordings", "interview_questions",
                        "interview_question_spans", "interview_reviews", "interview_actions"}
_PG_DOCUMENT_TABLES = {"station_documents", "station_attachment_turns"}
_PG_TRACE_TABLES = {"station_traces", "station_trace_spans"}
_PG_TASK_TABLES = {"station_task_control", "station_task_actions"}
_PG_TABLES = _PG_CORE_TABLES | _PG_INTERVIEW_TABLES | _PG_DOCUMENT_TABLES | _PG_TRACE_TABLES | _PG_TASK_TABLES


def _postgres_schema_state(c: _PostgresConnection) -> str:
    """探测现有 schema：只读，不让普通请求执行任何 DDL。"""
    rows = c.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name IN "
        "('users','threads','jobs','archive_states','corrections','checkpoints',"
        "'model_configs','mcp_servers','ref_links','interviews','interview_recordings',"
        "'interview_questions','interview_question_spans','interview_reviews',"
        "'interview_actions','station_documents','station_attachment_turns',"
        "'station_traces','station_trace_spans','station_task_control','station_task_actions')").fetchall()
    tables = {row["table_name"] for row in rows}
    if not tables:
        return "empty"
    if not _PG_CORE_TABLES.issubset(tables):
        missing = ", ".join(sorted(_PG_CORE_TABLES - tables))
        raise RuntimeError(f"PostgreSQL schema 不完整（缺少：{missing}）；请先执行受控迁移")
    has_seq = c.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='users' AND column_name='seq') "
        "AS present").fetchone()["present"]
    required = _PG_INTERVIEW_TABLES | _PG_DOCUMENT_TABLES | _PG_TRACE_TABLES | _PG_TASK_TABLES
    return "current" if has_seq and required.issubset(tables) else "upgrade-needed"


def upgrade_postgres_schema(c: _PostgresConnection) -> str:
    """显式执行 PostgreSQL schema bootstrap/升级，调用前应停掉所有 station 实例。

    迁移命令会先用 advisory lock 串行化；升级老库时再用 NOWAIT 的排他锁，若仍有
    在线读写就立即失败而非等待、死锁或半天后才让用户发现登录卡住。
    """
    with c.raw.transaction():
        c.execute("SELECT pg_advisory_xact_lock(?)", (_PG_SCHEMA_LOCK,))
        state = _postgres_schema_state(c)  # 获锁后重查，避免并发启动的竞态
        if state == "current":
            return state
        if state == "empty":
            for statement in _postgres_schema():
                c.execute(statement)
            return "bootstrapped"

        # 旧库可能同时缺 users.seq 与面试模块。显式迁移才允许 DDL；普通 Web 启动只读验。
        has_seq = c.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' AND column_name='seq') "
            "AS present").fetchone()["present"]
        if not has_seq:
            c.execute("LOCK TABLE users IN ACCESS EXCLUSIVE MODE NOWAIT")
            c.execute("ALTER TABLE users ADD COLUMN seq BIGINT NOT NULL DEFAULT 0")
            c.execute(
                "UPDATE users SET seq=ordered.seq FROM ("
                "SELECT id, ROW_NUMBER() OVER (ORDER BY created ASC, id ASC) AS seq FROM users"
                ") AS ordered WHERE users.id=ordered.id AND users.seq=0")
        for statement in _interview_schema() + _document_schema() + _trace_schema() + _task_schema():
            c.execute(statement)
        return "upgraded"


def init_postgres(c: _PostgresConnection) -> None:
    """给应用进程做无副作用的 schema 检查；只有全新空库允许自动 bootstrap。"""
    state = _postgres_schema_state(c)
    if state == "current":
        return
    if state == "empty":
        upgrade_postgres_schema(c)
        return
    raise RuntimeError(
        "PostgreSQL schema 需要升级：停掉所有 station 服务后执行 station-upgrade-postgres")


def init_db(c: sqlite3.Connection) -> None:
    """建表（IF NOT EXISTS：已有库重复启动也不炸、不丢数据）。"""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id       TEXT PRIMARY KEY,
        name     TEXT UNIQUE NOT NULL,
        pw_hash  TEXT NOT NULL,
        salt     TEXT NOT NULL,
        created  REAL NOT NULL,
        -- 插入序：owner_id() 要在"同一时刻注册的两个账号"之间分出先后。
        -- ★ 不能用 id 顶替 —— uuid4 生成的片段**稳定但不保序**（原来靠 SQLite 的
        --   rowid，而 PG 没有 rowid）。显式存一列，两端行为一致。
        seq      INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS threads(
        id       TEXT PRIMARY KEY,
        user_id  TEXT NOT NULL,
        skill_id TEXT NOT NULL,
        msgs     TEXT NOT NULL,   -- JSON：[{role,content,tool_calls,...},...]
        meta     TEXT NOT NULL,   -- JSON：{pending,summary,...}
        created  REAL NOT NULL,
        updated  REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS jobs(
        id       TEXT PRIMARY KEY,
        user_id  TEXT NOT NULL,
        skill_id TEXT NOT NULL,
        args     TEXT NOT NULL,   -- JSON
        status   TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        message  TEXT NOT NULL DEFAULT '',
        log      TEXT NOT NULL,   -- JSON：事件流水
        artifacts TEXT NOT NULL,  -- JSON：[文件id,...]
        result   TEXT,            -- JSON：技能附加结果（可空）
        created  REAL NOT NULL,
        updated  REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS archive_states(
        project  TEXT NOT NULL,      -- project.json 绝对路径
        channel  TEXT NOT NULL DEFAULT '',   -- 见下方注释：'' = 卷级元数据行，其余 = 某家识图模型的现场
        job_id   TEXT NOT NULL,
        user_id  TEXT NOT NULL,
        person   TEXT NOT NULL DEFAULT '',
        state    TEXT NOT NULL,      -- JSON：{materials, issues, updated[, stage, active]}
        updated  REAL NOT NULL,
        PRIMARY KEY (project, channel)
    );
    CREATE TABLE IF NOT EXISTS corrections(
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        project  TEXT NOT NULL,
        user_id  TEXT NOT NULL,
        kind     TEXT NOT NULL,      -- set_category | merge | split | reocr
        payload  TEXT NOT NULL,      -- JSON：{before, after, ts}
        ts       REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS checkpoints(
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        project  TEXT NOT NULL,
        channel  TEXT NOT NULL DEFAULT '',   -- 这份快照属于哪家模型的现场（换模型后互不串）
        user_id  TEXT NOT NULL,
        label    TEXT NOT NULL,      -- 人话说明（"改类 调资表→九-1 后"）
        state    TEXT NOT NULL,      -- JSON：完整现场快照
        created  REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS model_configs(
        user_id  TEXT PRIMARY KEY,   -- 归属用户（每人一份，覆盖写）
        cfg      TEXT NOT NULL,      -- JSON：{version, providers[], slots{}}
        updated  REAL NOT NULL
    );
    -- 用户自己配的 MCP server（每人一份，跟模型配置同一种存法：整包 JSON 一列）。
    -- ★ 只存**用户配的**那些：代码里写死的内置 server 不落库（见 station/mcp/config.py
    --   的 BUILTIN_SERVERS）—— 落库就有"库里那条和代码里那条不一致"的问题。
    -- ★ 这一列里**不含任何 stdio 命令**，那是刻意的安全边界（用户配不了 stdio），
    --   别在别处偷偷加回来。
    CREATE TABLE IF NOT EXISTS mcp_servers(
        user_id  TEXT PRIMARY KEY,   -- 归属用户（每人一份，覆盖写）
        cfg      TEXT NOT NULL,      -- JSON：{version, servers[{id,label,transport,url,headers}]}
        updated  REAL NOT NULL
    );
    -- 能力令牌：把某个文件**临时**开给外部服务抓取（视频生成的人像参考图就是这么用的）。
    -- 为什么不直接把文件 id 当公开路径：fid 是内容 sha1，那等于给了一个可枚举的面，
    -- 而且没有过期概念 —— 一旦公开就是永久公开。这里存的是不可猜、会过期、可撤销的取件码。
    CREATE TABLE IF NOT EXISTS ref_links(
        token    TEXT PRIMARY KEY,   -- 取件码（secrets.token_urlsafe，不可猜）
        fid      TEXT NOT NULL,      -- 指向文件区哪个文件
        user_id  TEXT NOT NULL,      -- 谁签发的（撤销/审计用）
        expires  REAL NOT NULL,      -- 过期时间戳（秒）：判断写进 SQL，别在 Python 里比
        created  REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS interviews(
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        title       TEXT NOT NULL,
        company     TEXT NOT NULL DEFAULT '',
        role        TEXT NOT NULL DEFAULT '',
        round       TEXT NOT NULL DEFAULT '',
        occurred_at REAL,
        status      TEXT NOT NULL DEFAULT 'draft',
        speaker_map TEXT NOT NULL DEFAULT '{}',
        summary     TEXT NOT NULL DEFAULT '',
        meta        TEXT NOT NULL DEFAULT '{}',
        created     REAL NOT NULL,
        updated     REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS interview_recordings(
        id                 TEXT PRIMARY KEY,
        interview_id       TEXT NOT NULL,
        file_id            TEXT NOT NULL,
        display_name       TEXT NOT NULL DEFAULT '',
        sequence           INTEGER NOT NULL,
        duration_ms        INTEGER,
        asr_job_id         TEXT NOT NULL DEFAULT '',
        transcript_file_id TEXT NOT NULL DEFAULT '',
        asr_model          TEXT NOT NULL DEFAULT '',
        status             TEXT NOT NULL DEFAULT 'uploaded',
        error_message      TEXT NOT NULL DEFAULT '',
        created            REAL NOT NULL,
        updated            REAL NOT NULL,
        UNIQUE(interview_id, sequence)
    );
    CREATE TABLE IF NOT EXISTS interview_questions(
        id            TEXT PRIMARY KEY,
        interview_id  TEXT NOT NULL,
        sequence      INTEGER NOT NULL,
        category      TEXT NOT NULL DEFAULT '',
        question_text TEXT NOT NULL DEFAULT '',
        answer_text   TEXT NOT NULL DEFAULT '',
        followup_text TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'draft',
        tags          TEXT NOT NULL DEFAULT '[]',
        created       REAL NOT NULL,
        updated       REAL NOT NULL,
        UNIQUE(interview_id, sequence)
    );
    CREATE TABLE IF NOT EXISTS interview_question_spans(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id  TEXT NOT NULL,
        recording_id TEXT NOT NULL,
        start_ms     INTEGER NOT NULL,
        end_ms       INTEGER NOT NULL,
        speaker_role TEXT NOT NULL DEFAULT '',
        created      REAL NOT NULL,
        CHECK(end_ms >= start_ms)
    );
    CREATE TABLE IF NOT EXISTS interview_reviews(
        id              TEXT PRIMARY KEY,
        question_id     TEXT NOT NULL,
        version         INTEGER NOT NULL,
        self_reflection TEXT NOT NULL DEFAULT '',
        coach_feedback  TEXT NOT NULL DEFAULT '',
        improved_answer TEXT NOT NULL DEFAULT '',
        drill_prompt    TEXT NOT NULL DEFAULT '',
        source          TEXT NOT NULL DEFAULT 'ai',
        created         REAL NOT NULL,
        updated         REAL NOT NULL,
        UNIQUE(question_id, version)
    );
    CREATE TABLE IF NOT EXISTS interview_actions(
        id           TEXT PRIMARY KEY,
        interview_id TEXT NOT NULL,
        question_id  TEXT,
        title        TEXT NOT NULL,
        detail       TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'todo',
        due_at       REAL,
        created      REAL NOT NULL,
        updated      REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id, updated);
    CREATE INDEX IF NOT EXISTS idx_jobs_user    ON jobs(user_id, updated);
    CREATE INDEX IF NOT EXISTS idx_states_user  ON archive_states(user_id, updated);
    CREATE INDEX IF NOT EXISTS idx_ck_project   ON checkpoints(project, id);
    CREATE INDEX IF NOT EXISTS idx_ref_expires  ON ref_links(expires);
    CREATE INDEX IF NOT EXISTS idx_interviews_user_updated ON interviews(user_id, updated DESC);
    CREATE INDEX IF NOT EXISTS idx_recordings_interview_sequence ON interview_recordings(interview_id, sequence);
    CREATE INDEX IF NOT EXISTS idx_questions_interview_sequence ON interview_questions(interview_id, sequence);
    CREATE INDEX IF NOT EXISTS idx_question_spans_question ON interview_question_spans(question_id, recording_id, start_ms);
    CREATE INDEX IF NOT EXISTS idx_reviews_question_version ON interview_reviews(question_id, version DESC);
    CREATE INDEX IF NOT EXISTS idx_actions_interview_status_updated ON interview_actions(interview_id, status, updated DESC);
    """)
    for statement in _document_schema() + _trace_schema() + _task_schema():
        c.execute(statement)
    _migrate(c)
    c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """老库升级（**幂等**）：给"多模型现场共存"补上缺的列/主键。

    新手视角（Java 朋友版）：上面的 CREATE TABLE IF NOT EXISTS 对**已存在**的表
    什么都不做 —— 表结构变了它管不了。所以升级要自己写一段"改表"逻辑，每次启动跑一遍，
    用 PRAGMA 探一下"新列在不在"来决定要不要动，跑第二次就是空转（幂等）。

    两个表的情况不同：
      · archive_states：主键要从 (project) 变成 (project, channel)，而 **SQLite 改不了
        主键** → 只能"建新表 → 把老数据当 channel='' 搬过去 → 删旧表 → 改名"。
        旧表那一行正好就是这个卷的"卷级元数据行"（归属/姓名/当前展示哪家），语义对得上。
        注意：DROP TABLE 会连索引一起删掉，所以搬完要**重建索引**。
      · checkpoints：只是多一列 → `ALTER TABLE ... ADD COLUMN` 一条搞定。
    """
    cols = {r[1] for r in c.execute("PRAGMA table_info(archive_states)")}
    if "channel" not in cols:
        c.executescript("""
        CREATE TABLE archive_states_new(
            project  TEXT NOT NULL,
            channel  TEXT NOT NULL DEFAULT '',
            job_id   TEXT NOT NULL,
            user_id  TEXT NOT NULL,
            person   TEXT NOT NULL DEFAULT '',
            state    TEXT NOT NULL,
            updated  REAL NOT NULL,
            PRIMARY KEY (project, channel)
        );
        INSERT INTO archive_states_new
            (project, channel, job_id, user_id, person, state, updated)
            SELECT project, '', job_id, user_id, person, state, updated
              FROM archive_states;
        DROP TABLE archive_states;
        ALTER TABLE archive_states_new RENAME TO archive_states;
        CREATE INDEX IF NOT EXISTS idx_states_user ON archive_states(user_id, updated);
        """)
    cols = {r[1] for r in c.execute("PRAGMA table_info(checkpoints)")}
    if "channel" not in cols:
        c.execute("ALTER TABLE checkpoints ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    if "seq" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
        # 老行按**插入序**回填（SQLite 的 rowid 正好就是插入序），
        # 否则老库升上来 owner_id() 会因为 seq 全是 0 而退化成随机挑。
        c.execute("UPDATE users SET seq = rowid WHERE seq = 0")


# ── 用户（注册/登录校验）────────────────────────────────────────────

def _hash(pw: str, salt: str) -> str:
    """盐化摘要：sha256(salt + 密码)。盐每个用户随机——同密码也不同摘要。"""
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()


def create_user(name: str, pw: str) -> dict:
    """注册一个用户；重名抛 ValueError（接口层转 400）。"""
    if not (name or "").strip() or not pw:
        raise ValueError("用户名和密码不能为空")
    uid = uuid.uuid4().hex[:12]
    salt = secrets.token_hex(8)
    with _lock:
        try:
            conn().execute(
                "INSERT INTO users(id,name,pw_hash,salt,created,seq) "
                "VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(seq),0)+1 FROM users))",
                (uid, name.strip(), _hash(pw, salt), salt, _now()))
            conn().commit()
        except Exception as e:
            # 重名怎么认：SQLite 抛 IntegrityError，psycopg 抛 UniqueViolation。
            # ★ **不按类名认** —— 类名会随驱动/包装层变（仓库按类名判错栽过一次），
            #   改用 SQL 标准状态码 23505 = unique_violation，两个驱动都带 sqlstate。
            if isinstance(e, sqlite3.IntegrityError) or getattr(e, "sqlstate", None) == "23505":
                raise ValueError(f"用户名已存在：{name}") from e
            raise
    return {"id": uid, "name": name.strip()}


def check_login(name: str, pw: str) -> dict | None:
    """校验登录：对了返回 {id,name}，错了 None。不区分'没此人'和'密码错'（防探测）。"""
    with _lock:
        row = conn().execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        if _hash(pw or "", row["salt"]) != row["pw_hash"]:
            return None
        return {"id": row["id"], "name": row["name"]}


def get_user(uid: str) -> dict | None:
    """按 id 查用户（cookie 里只存 uid，每次请求回查——改密码/删号立即生效）。"""
    with _lock:
        row = conn().execute("SELECT id,name FROM users WHERE id=?", (uid,)).fetchone()
        return {"id": row["id"], "name": row["name"]} if row else None


def user_count() -> int:
    """用户总数（前端据此显示"首个用户=站长"提示）。"""
    with _lock:
        return conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def owner_id() -> str | None:
    """站长的 user id = **最早注册的那个账号**；一个用户都没有时返回 None。

    排序以 created 为主、**seq（插入序）**兜底：seq 是显式存的一列，SQLite 与
    PostgreSQL 都可用。★ 不能用 id 顶替 —— uuid4 生成的片段**稳定但不保序**，
    那样同刻注册的两个账号会退化成"随机挑一个当站长"；也不能再用 SQLite 专有的 rowid。
    """
    with _lock:
        row = conn().execute(
            "SELECT id FROM users ORDER BY created ASC, seq ASC LIMIT 1").fetchone()
        return row["id"] if row else None


def is_owner(uid: str) -> bool:
    """uid 是不是站长。

    站长独享 src/.env 里那几把密钥的**兜底**（见 modelcfg._api_key）—— 站是部署的人
    自己掏钱开的，密钥自然只给他用；别的账号必须自带密钥，否则等于访客在花站长的钱。

    ★ 空 uid 视为站长：那是**没有登录态**的路径（archive CLI、后台内部调用），
      本来就是运维自己在跑，必须继续认 .env —— 见 conventions「无用户 → 走 .env 默认」。
    """
    if not uid:
        return True
    return uid == owner_id()


def document_register(record: dict, upload_key: str) -> dict:
    """登记原件并分配当天次序；同一上传请求重放不会登记两次。"""
    with _lock:
        c = conn()
        old = c.execute("SELECT record FROM station_documents WHERE user_id=? AND upload_key=?",
                        (record["owner_id"], upload_key)).fetchone()
        if old:
            return json.loads(old["record"])
        sequence = c.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM station_documents "
            "WHERE user_id=? AND local_date=?", (record["owner_id"], record["date"])).fetchone()["n"]
        value = dict(record, sequence=sequence)
        c.execute("INSERT INTO station_documents(id,user_id,upload_key,local_date,sequence,record,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?)", (value["id"], value["owner_id"], upload_key, value["date"],
                  sequence, json.dumps(value, ensure_ascii=False), value["created"], _now()))
        c.commit()
        return value


def document_get(document_id: str, user_id: str) -> dict | None:
    """文档访问永远同时带 owner，内容哈希不充当权限。"""
    with _lock:
        row = conn().execute("SELECT record FROM station_documents WHERE id=? AND user_id=?",
                             (document_id, user_id)).fetchone()
        return json.loads(row["record"]) if row else None


def document_update(document_id: str, user_id: str, patch: dict) -> dict:
    """只更新当前用户的文档元数据；调用者不能修改归属/身份/上传日期。"""
    with _lock:
        value = document_get(document_id, user_id)
        if value is None:
            raise ValueError("文件不存在")
        if set(patch) & {"id", "owner_id", "date", "sequence", "created"}:
            raise ValueError("不能修改文档身份")
        value.update(patch)
        value["updated"] = _now()
        c = conn()
        c.execute("UPDATE station_documents SET record=?,updated=? WHERE id=? AND user_id=?",
                  (json.dumps(value, ensure_ascii=False), value["updated"], document_id, user_id))
        c.commit()
        return value


def document_list(user_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
    """按日期/当天数值次序列出文档；大正文不进入数据库。"""
    with _lock:
        rows = conn().execute("SELECT record FROM station_documents WHERE user_id=? "
                              "ORDER BY local_date DESC,sequence ASC LIMIT ? OFFSET ?",
                              (user_id, max(1, min(limit, 1000)), max(0, offset))).fetchall()
        return [json.loads(row["record"]) for row in rows]


def document_delete_draft(document_id: str, user_id: str) -> bool:
    """仅删除从未被轮次引用的上传草稿；已使用的原件不能随移除选择一起删除。"""
    with _lock:
        document = document_get(document_id, user_id)
        if not document or document.get("runs") or document.get("status") != "uploaded":
            return False
        # 不只查最近 1000 个轮次；保守的带引号 ID 匹配也会保护更老的引用。
        used = conn().execute("SELECT id FROM station_attachment_turns WHERE user_id=? AND record LIKE ? LIMIT 1",
                              (user_id, '%"' + document_id + '"%')).fetchone()
        if used:
            return False
        c = conn()
        c.execute("DELETE FROM station_documents WHERE id=? AND user_id=?", (document_id, user_id))
        c.commit()
        return True


def document_expired_drafts(cutoff: float) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT record FROM station_documents WHERE created<?", (cutoff,)).fetchall()
        return [value for row in rows if (value := json.loads(row["record"])).get("status") == "uploaded"]


def attachment_turn_create(record: dict) -> tuple[dict, bool]:
    """冻结一轮文字/附件；唯一键防止断网重发和双击发送。"""
    with _lock:
        c = conn()
        row = c.execute("SELECT record FROM station_attachment_turns WHERE user_id=? AND client_key=?",
                        (record["owner_id"], record["client_key"])).fetchone()
        if row:
            return json.loads(row["record"]), False
        # 与草稿删除共用数据库锁，不能在校验后被清理再提交一个悬空引用。
        if any(document_get(document_id, record["owner_id"]) is None for document_id in record["attachment_ids"]):
            raise ValueError("附件不存在或已经过期")
        c.execute("INSERT INTO station_attachment_turns(id,user_id,thread_id,client_key,state,record,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?)", (record["id"], record["owner_id"], record["thread_id"],
                  record["client_key"], record["state"], json.dumps(record, ensure_ascii=False),
                  record["created"], _now()))
        c.commit()
        return record, True


def attachment_turn_get(turn_id: str, user_id: str) -> dict | None:
    with _lock:
        row = conn().execute("SELECT record FROM station_attachment_turns WHERE id=? AND user_id=?",
                             (turn_id, user_id)).fetchone()
        return json.loads(row["record"]) if row else None


def attachment_turn_find(client_key: str, user_id: str) -> dict | None:
    """首次发送响应丢失时，客户端尚无 thread_id，仍可按请求编号找到原轮次。"""
    with _lock:
        row = conn().execute("SELECT record FROM station_attachment_turns WHERE client_key=? AND user_id=? "
                             "ORDER BY created DESC LIMIT 1", (client_key, user_id)).fetchone()
        return json.loads(row["record"]) if row else None


def attachment_turn_update(turn_id: str, user_id: str, patch: dict,
                           expected: tuple[str, ...] | None = None) -> dict | None:
    """状态条件更新用于领取 agent 执行权；同一轮只有一个恢复请求胜出。"""
    with _lock:
        value = attachment_turn_get(turn_id, user_id)
        if value is None or (expected is not None and value["state"] not in expected):
            return None
        if set(patch) & {"id", "owner_id", "thread_id", "client_key", "message", "attachment_ids", "config"}:
            raise ValueError("不能修改已冻结的附件输入")
        previous = value["state"]
        value.update(patch)
        value["updated"] = _now()
        c = conn()
        cur = c.execute("UPDATE station_attachment_turns SET state=?,record=?,updated=? "
                        "WHERE id=? AND user_id=? AND state=?", (value["state"],
                        json.dumps(value, ensure_ascii=False), value["updated"], turn_id, user_id, previous))
        c.commit()
        return value if cur.rowcount else None


def attachment_turn_list(user_id: str = "", thread_id: str = "") -> list[dict]:
    """空 owner 只供宿主启动恢复使用，HTTP 查询必须带登录用户。"""
    with _lock:
        where, args = [], []
        if user_id:
            where.append("user_id=?")
            args.append(user_id)
        if thread_id:
            where.append("thread_id=?")
            args.append(thread_id)
        rows = conn().execute("SELECT record FROM station_attachment_turns" +
                              (" WHERE " + " AND ".join(where) if where else "") +
                              " ORDER BY created DESC LIMIT 1000", tuple(args)).fetchall()
        return [json.loads(row["record"]) for row in rows]


def trace_create(record: dict, key: str = "") -> dict:
    """关联键由服务端构造；恢复复用同一档案，重复提交不创建第二份追踪。"""
    with _lock:
        c = conn()
        # 默认保存 30 天、每用户最多 1000 条。仅清理追踪，不动会话和产物。
        expired = c.execute("SELECT id FROM station_traces WHERE user_id=? AND updated<?",
                            (record['user_id'],_now()-30*86400)).fetchall()
        overflow = c.execute("SELECT id FROM station_traces WHERE user_id=? ORDER BY created DESC "
                             "LIMIT 10000 OFFSET 999",(record['user_id'],)).fetchall()
        for row in [*expired,*overflow]:
            c.execute("DELETE FROM station_trace_spans WHERE trace_id=?",(row['id'],))
            c.execute("DELETE FROM station_traces WHERE id=? AND user_id=?",(row['id'],record['user_id']))
        key = key or record['id']
        c.execute("INSERT INTO station_traces(id,user_id,thread_id,correlation_key,created,updated,record) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,correlation_key) DO NOTHING",
                  (record['id'],record['user_id'],record['thread_id'],key,record['created'],record['updated'],
                   json.dumps(record,ensure_ascii=False)))
        c.commit()
        row = c.execute("SELECT record FROM station_traces WHERE user_id=? AND correlation_key=?",
                        (record['user_id'],key)).fetchone()
        return json.loads(row['record'])


def trace_save(record: dict) -> dict:
    with _lock:
        previous = trace_get(record['id'], record['user_id'])
        if previous:
            record = {**record, 'state': previous['state'], 'active_ms': previous.get('active_ms', 0),
                      'attributes': {**previous.get('attributes', {}), **record.get('attributes', {})}}
        conn().execute("UPDATE station_traces SET updated=?,record=? WHERE id=? AND user_id=?",
                       (record['updated'],json.dumps(record,ensure_ascii=False),record['id'],record['user_id']))
        conn().commit()
        return record


def trace_finish(trace_id: str, user_id: str, state: str, duration: float, incomplete: bool) -> dict | None:
    """同一附件的并发恢复可能只执行一次；累计时间原子合并，跳过请求不能覆盖获胜者状态。"""
    with _lock:
        record = trace_get(trace_id, user_id)
        if not record:
            return None
        record.update(updated=_now(), active_ms=round(record.get('active_ms',0)+duration,2),
                      incomplete=bool(record.get('incomplete') or incomplete))
        if state != 'skipped':
            record['state'] = state
        conn().execute("UPDATE station_traces SET updated=?,record=? WHERE id=? AND user_id=?",
                       (record['updated'],json.dumps(record,ensure_ascii=False),trace_id,user_id))
        conn().commit()
        return record


def trace_span_save(record: dict) -> None:
    with _lock:
        result = conn().execute("INSERT INTO station_trace_spans(id,trace_id,created,record) SELECT ?,?,?,? "
                                "WHERE EXISTS(SELECT 1 FROM station_trace_spans WHERE id=?) OR "
                                "(SELECT COUNT(*) FROM station_trace_spans WHERE trace_id=?) < 400 "
                                "ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                                (record['id'],record['trace_id'],record['created'],json.dumps(record,ensure_ascii=False),
                                 record['id'],record['trace_id']))
        if not result.rowcount:
            raise RuntimeError("trace span limit")
        conn().commit()


def trace_get(trace_id: str, user_id: str) -> dict | None:
    with _lock:
        row = conn().execute("SELECT record FROM station_traces WHERE id=? AND user_id=?",
                             (trace_id,user_id)).fetchone()
        return json.loads(row['record']) if row else None


def trace_spans(trace_id: str, user_id: str) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT s.record FROM station_trace_spans s JOIN station_traces t ON s.trace_id=t.id "
                              "WHERE t.id=? AND t.user_id=? ORDER BY s.created,s.id LIMIT 1000",
                              (trace_id,user_id)).fetchall()
        return [json.loads(r['record']) for r in rows]


def trace_list(thread_id: str, user_id: str) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT record FROM station_traces WHERE thread_id=? AND user_id=? "
                              "ORDER BY created DESC LIMIT 100",(thread_id,user_id)).fetchall()
        return [json.loads(r['record']) for r in rows]


def trace_children(trace_id: str, user_id: str) -> list[dict]:
    with _lock:
        row = conn().execute("SELECT thread_id FROM station_traces WHERE id=? AND user_id=?",(trace_id,user_id)).fetchone()
        if not row:
            return []
        rows = conn().execute("SELECT record FROM station_traces WHERE thread_id=? AND user_id=? "
                              "ORDER BY created DESC LIMIT 1000", (row['thread_id'],user_id)).fetchall()
        return [record for row in rows if (record := json.loads(row['record'])).get('origin_trace_id') == trace_id]


# ── Thread（会话）──────────────────────────────────────────────────

def thread_save(t) -> None:
    """把 core.session.Thread 的内容写进 threads 表（UPSERT：有则更新无则插入）。"""
    with _lock:
        conn().execute(
            "INSERT INTO threads(id,user_id,skill_id,msgs,meta,created,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "user_id=excluded.user_id, skill_id=excluded.skill_id, msgs=excluded.msgs, "
            "meta=excluded.meta, updated=excluded.updated",
            (t.id, getattr(t, "user_id", "") or "", t.skill_id,
             json.dumps(t.msgs, ensure_ascii=False),
             json.dumps(t.meta, ensure_ascii=False),
             getattr(t, "created", _now()), t.updated))
        conn().commit()


def thread_load(thread_id: str):
    """按 id 读回 Thread（core.session.Thread 对象）；没有返回 None。"""
    from station.core.session import Thread
    with _lock:
        row = conn().execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        t = Thread(skill_id=row["skill_id"], thread_id=row["id"])
        t.msgs = json.loads(row["msgs"])
        t.meta = json.loads(row["meta"])
        t.updated = row["updated"]
        t.user_id = row["user_id"]
        return t


def thread_list(user_id: str, skill_id: str | None = None) -> list[dict]:
    """某用户的会话摘要（新→旧）。skill_id 可选过滤（前端按技能分组展示）。"""
    sql = "SELECT id,skill_id,msgs,updated FROM threads WHERE user_id=?"
    args: list = [user_id]
    if skill_id:
        sql += " AND skill_id=?"
        args.append(skill_id)
    sql += " ORDER BY updated DESC"
    out = []
    with _lock:                     # ★ 连**遍历游标**也要在锁里：行是惰性取的
        for row in conn().execute(sql, args):
            msgs = json.loads(row["msgs"])
            out.append({"id": row["id"], "skill_id": row["skill_id"],
                        "updated": row["updated"], "n": len(msgs),
                        "last": (msgs[-1].get("content", "") if msgs else "")[:60]})
    return out


# thread_delete() 已删（09-11）：没有"删除会话"这个功能，零调用方。


# ── Job（任务）─────────────────────────────────────────────────────

def job_save(j) -> None:
    """把 jobs/manager.Job 写进 jobs 表（每次 update/append 都会调，UPSERT）。"""
    with _lock:
        token=getattr(j,'_lease_token','')
        if token:
            # 围栏必须与写入是同一条 SQL：心跳检查后网络暂停时，旧 token 仍不能覆盖新 worker。
            result=conn().execute(
                "UPDATE jobs SET status=?,progress=?,message=?,log=?,artifacts=?,result=?,updated=? "
                "WHERE id=? AND user_id=? AND EXISTS(SELECT 1 FROM station_task_control c "
                "WHERE c.job_id=jobs.id AND c.user_id=jobs.user_id AND c.token=? AND c.lease_until>?)",
                (j.status,j.progress,j.message,json.dumps(j.log,ensure_ascii=False),json.dumps(j.artifacts),
                 json.dumps(j.result,ensure_ascii=False) if j.result is not None else None,j.updated,
                 j.id,j.user_id,token,_now()))
            conn().commit()
            if not result.rowcount:
                from station.tasks.runtime import LeaseLost
                raise LeaseLost()
            return
        conn().execute(
            "INSERT INTO jobs(id,user_id,skill_id,args,status,progress,message,"
            "log,artifacts,result,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "progress=excluded.progress, message=excluded.message, log=excluded.log, "
            "artifacts=excluded.artifacts, result=excluded.result, "
            "updated=excluded.updated",
            (j.id, getattr(j, "user_id", "") or "", j.skill_id,
             json.dumps(j.args, ensure_ascii=False), j.status, j.progress,
             j.message, json.dumps(j.log, ensure_ascii=False),
             json.dumps(j.artifacts, ensure_ascii=False),
             json.dumps(j.result, ensure_ascii=False) if j.result is not None else None,
             j.created, j.updated))
        conn().commit()


def _job_from_row(row):
    """纯行适配，不访问连接；job_load 和任务分页共用。"""
    from station.jobs.manager import Job
    j = Job(row["skill_id"], json.loads(row["args"]))
    j.id = row["id"]
    j.status, j.progress = row["status"], row["progress"]
    j.message, j.created, j.updated = row["message"], row["created"], row["updated"]
    j.log = json.loads(row["log"])
    j.artifacts = json.loads(row["artifacts"])
    j.result = json.loads(row["result"]) if row["result"] else None
    j.user_id = row["user_id"]
    return j


def job_load(job_id: str):
    with _lock:
        row=conn().execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        return _job_from_row(row) if row else None


def task_job_register(job, metadata: dict) -> bool:
    """任务与恢复元数据一起登记；同 ID 的并发提交不能把已运行任务覆盖回 queued。"""
    with _lock:
        c=conn()
        transaction=c.raw.transaction() if isinstance(c,_PostgresConnection) else c
        with transaction:
            result=c.execute("INSERT INTO jobs(id,user_id,skill_id,args,status,progress,message,log,artifacts,result,created,updated) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                             (job.id,job.user_id,job.skill_id,json.dumps(job.args,ensure_ascii=False),job.status,job.progress,
                              job.message,json.dumps(job.log),json.dumps(job.artifacts),
                              json.dumps(job.result,ensure_ascii=False) if job.result is not None else None,job.created,job.updated))
            if result.rowcount:
                c.execute("INSERT INTO station_task_control(job_id,user_id,record,updated) VALUES(?,?,?,?)",
                          (job.id,job.user_id,json.dumps(metadata,ensure_ascii=False),_now()))
            return bool(result.rowcount)


def task_control_create(job_id: str, user_id: str, record: dict) -> None:
    with _lock:
        conn().execute("INSERT INTO station_task_control(job_id,user_id,record,updated) VALUES(?,?,?,?) "
                       "ON CONFLICT(job_id) DO NOTHING",(job_id,user_id,json.dumps(record,ensure_ascii=False),_now()))
        conn().commit()


def task_control_get(job_id: str, user_id: str) -> dict | None:
    with _lock:
        row=conn().execute("SELECT * FROM station_task_control WHERE job_id=? AND user_id=?",(job_id,user_id)).fetchone()
        return {**dict(row),'record':json.loads(row['record'])} if row else None


def task_claim(job_id: str, user_id: str, instance: str, boot: str, token: str, seconds: float) -> bool:
    with _lock:
        now=_now()
        result=conn().execute("UPDATE station_task_control SET instance_id=?,boot_id=?,token=?,lease_until=?,"
                              "cancel_requested=0,updated=? WHERE job_id=? AND user_id=? AND "
                              "(lease_until<=? OR (instance_id=? AND boot_id<>?))",
                              (instance,boot,token,now+seconds,now,job_id,user_id,now,instance,boot))
        conn().commit()
        return bool(result.rowcount)


def task_heartbeat(job_id: str, user_id: str, token: str, seconds: float) -> bool:
    with _lock:
        now=_now()
        result=conn().execute("UPDATE station_task_control SET lease_until=?,updated=? "
                              "WHERE job_id=? AND user_id=? AND token=? AND lease_until>?",
                              (now+seconds,now,job_id,user_id,token,now))
        conn().commit()
        return bool(result.rowcount)


def task_checkpoint(job_id: str, user_id: str, token: str, values: dict) -> bool:
    with _lock:
        row=task_control_get(job_id,user_id)
        if not row or row['token']!=token or row['lease_until']<=_now():return False
        record={**row['record'],**values}
        result=conn().execute("UPDATE station_task_control SET record=?,updated=? WHERE job_id=? AND user_id=? AND token=?",
                              (json.dumps(record,ensure_ascii=False),_now(),job_id,user_id,token))
        conn().commit()
        return bool(result.rowcount)


def task_release(job_id: str, user_id: str, token: str) -> None:
    with _lock:
        conn().execute("UPDATE station_task_control SET lease_until=0,updated=? WHERE job_id=? AND user_id=? AND token=?",
                       (_now(),job_id,user_id,token))
        conn().commit()


def task_cancel(job_id: str, user_id: str) -> bool:
    with _lock:
        result=conn().execute("UPDATE station_task_control SET cancel_requested=1,updated=? WHERE job_id=? AND user_id=?",
                              (_now(),job_id,user_id))
        conn().commit()
        return bool(result.rowcount)


def task_job_list(user_id: str, before: tuple | None = None, limit: int = 51) -> list:
    with _lock:
        where="user_id=?";args=[user_id]
        if before:
            where+=" AND (created<? OR (created=? AND id<?))";args.extend((before[0],before[0],before[1]))
        rows=conn().execute("SELECT * FROM jobs WHERE "+where+" ORDER BY created DESC,id DESC LIMIT ?",(*args,min(200,max(1,limit)))).fetchall()
        return [_job_from_row(row) for row in rows]


def task_records_many(kind: str, user_id: str, ids: list) -> dict:
    """固定表白名单的批量读取，列表页不为每个任务单独往返远程数据库。"""
    with _lock:
        table,column={"control":("station_task_control","job_id"),"turn":("station_attachment_turns","id"),
                      "document":("station_documents","id")}[kind]
        values=list(dict.fromkeys(i for i in ids if i))[:2000]
        result={}
        for offset in range(0,len(values),200):
            batch=values[offset:offset+200]
            rows=conn().execute("SELECT * FROM "+table+" WHERE user_id=? AND "+column+" IN ("+",".join("?" for _ in batch)+")",
                                (user_id,*batch)).fetchall()
            for row in rows:
                value=json.loads(row['record'])
                result[row[column]]={**dict(row),'record':value} if kind=='control' else value
        return result


def task_existing_jobs(user_id: str, ids: list) -> set:
    with _lock:
        values=list(dict.fromkeys(i for i in ids if i))[:100]
        if not values:return set()
        rows=conn().execute("SELECT id FROM jobs WHERE user_id=? AND id IN ("+",".join("?" for _ in values)+")",(user_id,*values)).fetchall()
        return {r['id'] for r in rows}


def task_action_reserve(user_id: str, job_id: str, action: str, client_key: str, next_job_id: str) -> dict:
    """同一操作编号幂等，同一旧任务只产生一个接续任务；事务锁覆盖多进程竞争。"""
    with _lock:
        c=conn()
        transaction=c.raw.transaction() if isinstance(c,_PostgresConnection) else c
        with transaction:
            suffix=" FOR UPDATE" if isinstance(c,_PostgresConnection) else ""
            control=c.execute("SELECT record FROM station_task_control WHERE job_id=? AND user_id=?"+suffix,(job_id,user_id)).fetchone()
            existing=c.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?",(user_id,client_key)).fetchone()
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
            c.execute("UPDATE station_task_control SET record=?,updated=? WHERE job_id=? AND user_id=?",
                      (json.dumps(record,ensure_ascii=False),_now(),job_id,user_id))
            c.execute("INSERT INTO station_task_actions(id,user_id,job_id,action,client_key,next_job_id,created) "
                      "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,client_key) DO NOTHING",
                      (uuid.uuid4().hex,user_id,job_id,action,client_key,next_job_id,_now()))
            saved=c.execute("SELECT * FROM station_task_actions WHERE user_id=? AND client_key=?",(user_id,client_key)).fetchone()
            if saved['job_id']!=job_id or saved['action']!=action:raise ValueError('该操作编号已用于其他任务')
            return dict(saved)


# job_list() 已删（09-11）：唯一的调用方是 GET /api/jobs（任务历史列表），
# 那个端点随"统一主对话"一起删了；查单个任务用 job_load。


# ── 档案现场 / 修正账本 / 检查点（09-06 第二批：全持久化到 DB）──────────
# 此前现场(review/materials.json)和账本(corrections.jsonl)散在项目目录的 JSON
# 文件里——能用，但没法按人查、没法跨卷聚合、也没法在 UI 里列出"哪一步能回退"。
# 现在统一进 SQLite：账本/检查点流水追加，现场**一份卷 × 一家识图模型一行**。
#
# ★ 09-12 起现场多了 channel 维度（原来一卷一行，换模型重跑会覆盖前一家）：
#     channel = ''   → **卷级元数据行**：归属 user_id、person、stage="created"、
#                      以及"当前展示哪一家"（state.active）
#     channel = 'qwen'/'glm'/… → **某家模型读出来的现场**（materials/issues）
#   于是"换个模型重识别"是**多一行**，不是覆盖；展示哪家由元数据行的 active 决定。

def archive_state_save(project: str, job_id: str, user_id: str,
                       person: str, state: dict, channel: str = "") -> None:
    """保存（或覆盖）某个 channel 的现场：materials/issues 整包 JSON。

    channel="" 写的是卷级元数据行（归属/姓名/stage/active），其余是某家模型的现场。
    ★ ON CONFLICT 只更新 job_id/state/updated：**user_id/person 以第一次写入的为准**
      （建卷时定的归属与姓名，后续任何一家模型的写入都不该改写它）。
    """
    with _lock:
        conn().execute(
            "INSERT INTO archive_states(project,channel,job_id,user_id,person,state,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,channel) DO UPDATE SET "
            "job_id=excluded.job_id, state=excluded.state, updated=excluded.updated",
            (project, channel or "", job_id, user_id, person,
             json.dumps(state, ensure_ascii=False), _now()))
        conn().commit()


def archive_state_owner(project: str) -> str:
    """某卷的归属 user_id；没有这一卷返回空串。

    ★ 给 server 的 `_owned_project` 用（**页图端点唯一的鉴权**）。
    ★ 多 channel 之后不能随便 fetchone：优先读**卷级元数据行**（channel=''，建卷时写的，
      归属最权威），没有才回落到任意一行 —— 保证同一卷无论有多少家的现场，
      鉴权答案唯一、稳定。
    **别在 db.py 外面直接 `conn().execute()`**：绕开 `_lock` 就把"多线程共用一条连接"
    的坑又埋回去了 —— 全景卡一次要拉上百张缩略图，每个都过这道归属校验，正好和聊天
    流里的 `thread.save()` 撞车，表现为**部分缩略图破图**（`<img>` 收到 401/500 的 JSON
    就是一张破图）。
    """
    with _lock:
        row = conn().execute(
            "SELECT user_id FROM archive_states WHERE project=? "
            "ORDER BY (channel <> '') ASC, updated DESC LIMIT 1",
            (project,)).fetchone()
        return row["user_id"] if row else ""


def archive_state_get(project: str, channel: str = "") -> dict | None:
    """读**指定 channel** 的现场；没有返回 None（channel="" 即卷级元数据行）。"""
    with _lock:
        row = conn().execute(
            "SELECT state FROM archive_states WHERE project=? AND channel=?",
            (project, channel or "")).fetchone()
        return json.loads(row["state"]) if row else None


def archive_state_channels(project: str) -> list[dict]:
    """这卷有哪些 channel 的现场（不含卷级元数据行），新→旧。

    给"报菜单"（这卷被哪几家读过）与展示切换用。返回 [{"channel","person","updated"}]。
    """
    with _lock:
        rows = conn().execute(
            "SELECT channel, person, updated FROM archive_states "
            "WHERE project=? AND channel<>'' ORDER BY updated DESC",
            (project,)).fetchall()
        return [{"channel": r["channel"], "person": r["person"],
                 "updated": r["updated"]} for r in rows]


def archive_state_delete(project: str, channel: str = "") -> None:
    """删掉某卷**某个 channel** 的现场（重识别前清旧的空现场用）。

    ★ 默认只有 channel=""（卷级元数据行）会走到这里；**换模型重跑绝不能删别人** ——
      要清某一家时显式传 channel。
    """
    with _lock:
        conn().execute("DELETE FROM archive_states WHERE project=? AND channel=?",
                       (project, channel or ""))
        conn().commit()


# archive_state_list() 已删（09-11）：唯一的调用方是 GET /api/archives（"我的档案库"），
# 那个端点没 UI 用；按卷取现场用 archive_state_get。


def correction_add(project: str, user_id: str, kind: str, payload: dict) -> None:
    """追加一条修正记录（原 corrections.jsonl 的 DB 版；聚合口径提案的数据源）。"""
    with _lock:
        conn().execute(
            "INSERT INTO corrections(project,user_id,kind,payload,ts) VALUES(?,?,?,?,?)",
            (project, user_id, kind, json.dumps(payload, ensure_ascii=False), _now()))
        conn().commit()


def corrections_list(project: str) -> list[dict]:
    """某卷的全部修正记录（旧→新；read_corrections 的 DB 版）。"""
    with _lock:
        rows = conn().execute(
            "SELECT kind,payload,ts FROM corrections WHERE project=? ORDER BY id",
            (project,)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["payload"])
            d["kind"] = r["kind"]
            d["ts"] = r["ts"]
            out.append(d)
        return out


def checkpoint_add(project: str, user_id: str, label: str, state: dict,
                   channel: str = "") -> int:
    """存一个检查点（操作前的现场快照），返回检查点 id。

    channel：这份快照属于**哪家模型**的现场（换模型之后各家的回退链互不干扰）。
    """
    with _lock:
        c = conn()  # 先建连接，backend() 才能知道这次实际选中了哪一种数据库
        if backend() == "postgres":
            cur = c.execute(
                "INSERT INTO checkpoints(project,channel,user_id,label,state,created) "
                "VALUES(?,?,?,?,?,?) RETURNING id",
                (project, channel or "", user_id, label,
                 json.dumps(state, ensure_ascii=False), _now()))
            ck_id = int(cur.fetchone()["id"])
        else:
            cur = c.execute(
                "INSERT INTO checkpoints(project,channel,user_id,label,state,created) "
                "VALUES(?,?,?,?,?,?)",
                (project, channel or "", user_id, label,
                 json.dumps(state, ensure_ascii=False), _now()))
            ck_id = int(cur.lastrowid)
        c.commit()
    return ck_id


def checkpoints_list(project: str, limit: int = 20, channel: str = "") -> list[dict]:
    """某卷**某一家模型**的检查点列表（新→旧；不含 state 本体，省流量）。

    ★ 它是回退的**前置**：回退要 ck_id，而 id 是全局自增号，猜不出来 ——
      没这个函数，检查点就是"每步都在写、却列不出 id"的死链。
    """
    with _lock:
        rows = conn().execute(
            "SELECT id,label,created FROM checkpoints WHERE project=? AND channel=? "
            "ORDER BY id DESC LIMIT ?", (project, channel or "", limit)).fetchall()
        return [{"id": r["id"], "label": r["label"], "created": r["created"]}
                for r in rows]


def checkpoint_get(ck_id: int) -> dict | None:
    """读一个检查点的完整现场（含它属于哪卷、哪家模型 —— 回退前要校验归属）。

    返回 {"project","channel","state"} 或 None。
    ★ 不能只回 state：ck_id 是**全局自增号**，模型可能编一个编号，而那个编号也许属于
      **另一卷** —— 不校验就把它写进当前卷（09-12 审计发现的真隐患）。
    """
    with _lock:
        row = conn().execute(
            "SELECT project,channel,state FROM checkpoints WHERE id=?",
            (ck_id,)).fetchone()
        if row is None:
            return None
        return {"project": row["project"], "channel": row["channel"],
                "state": json.loads(row["state"])}


# ── 模型配置（每人一份：provider 连接信息 + 各槽位降级链）──────────────
# 09-10 新增：此前"用哪家模型"写死在 model.py 的 _PROVIDERS 常量里，只有 .env
# 能改；现在改成用户可在界面配置、落库。整包 JSON 存一列（不是拆成多列多表），
# 因为 init_db 只有 CREATE TABLE IF NOT EXISTS、没有迁移机制——拆列将来加字段会卡住。
# 读写/校验/规范化都在 station/modelcfg.py，这里只管"存取"。

def model_config_save(user_id: str, cfg: dict) -> None:
    """保存（覆盖）某用户的模型配置整包。"""
    with _lock:
        conn().execute(
            "INSERT INTO model_configs(user_id,cfg,updated) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET cfg=excluded.cfg, "
            "updated=excluded.updated",
            (user_id, json.dumps(cfg, ensure_ascii=False), _now()))
        conn().commit()


def model_config_load(user_id: str) -> dict | None:
    """读某用户的模型配置；从没存过返回 None（**不等于**"用默认"）。

    注意这个返回值里可能含明文 api_key——只许喂给 modelcfg 的解析/掩码函数，
    绝不能直接扔进 HTTP 响应或日志。
    """
    if not user_id:
        return None
    with _lock:
        row = conn().execute("SELECT cfg FROM model_configs WHERE user_id=?",
                             (user_id,)).fetchone()
        if row is None:
            return None
        try:
            d = json.loads(row["cfg"])
        except Exception:                # noqa：坏 JSON 当没存过，别让整站起不来
            return None
        return d if isinstance(d, dict) else None


def model_config_delete(user_id: str) -> None:
    """删掉某用户的模型配置（"恢复默认"用；下次读就又回到内置默认了）。"""
    with _lock:
        conn().execute("DELETE FROM model_configs WHERE user_id=?", (user_id,))
        conn().commit()


# ── 用户自配的 MCP server（每人一份）──────────────────────────────────
# 结构与理由同上一节（整包 JSON 一列）。读写/校验/合成都在 station/mcp/config.py，
# 这里只管"存取"。

def mcp_config_save(user_id: str, cfg: dict) -> None:
    """保存（覆盖）某用户的 MCP 配置整包。"""
    with _lock:
        conn().execute(
            "INSERT INTO mcp_servers(user_id,cfg,updated) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET cfg=excluded.cfg, "
            "updated=excluded.updated",
            (user_id, json.dumps(cfg, ensure_ascii=False), _now()))
        conn().commit()


def mcp_config_load(user_id: str) -> dict | None:
    """读某用户的 MCP 配置；从没存过返回 None（**不等于**"没有 server"）。"""
    if not user_id:
        return None
    with _lock:
        row = conn().execute("SELECT cfg FROM mcp_servers WHERE user_id=?",
                             (user_id,)).fetchone()
        if row is None:
            return None
        try:
            d = json.loads(row["cfg"])
        except Exception:                # noqa：坏 JSON 当没存过，别让整站起不来
            return None
        return d if isinstance(d, dict) else None


def mcp_config_delete(user_id: str) -> None:
    """删掉某用户的 MCP 配置（"全部清空"用）。"""
    with _lock:
        conn().execute("DELETE FROM mcp_servers WHERE user_id=?", (user_id,))
        conn().commit()


# ── 面试档案（多场面试 / 多段音频）───────────────────────────────────────
# 音频与完整转写 JSON 都在 files/store；这里仅保留归属、顺序、任务和产物引用。
# 任何读取都经 user_id 过滤，避免“猜到 id 就能看见别人的面试”。

def interview_create(user_id: str, title: str, *, company: str = "", role: str = "",
                     round_name: str = "", occurred_at: float | None = None) -> dict:
    """新建一份面试档案，返回可直接给工具层使用的行字典。"""
    now, iid = _now(), uuid.uuid4().hex
    clean_title = (title or "").strip() or "未命名面试"
    with _lock:
        conn().execute(
            "INSERT INTO interviews(id,user_id,title,company,role,round,occurred_at,"
            "status,speaker_map,summary,meta,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, user_id or "", clean_title, (company or "").strip(), (role or "").strip(),
             (round_name or "").strip(), occurred_at, "draft", "{}", "", "{}", now, now))
        conn().commit()
    return interview_get(iid, user_id) or {}


def interview_get(interview_id: str, user_id: str) -> dict | None:
    """按 id 读取自己的面试档案；不属于该用户与不存在统一返回 None。"""
    if not interview_id or not user_id:
        return None
    with _lock:
        row = conn().execute(
            "SELECT * FROM interviews WHERE id=? AND user_id=?", (interview_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_list(user_id: str, limit: int = 30) -> list[dict]:
    """列自己的面试档案，附带音频总数与已转写数，供选择/概览卡使用。"""
    if not user_id:
        return []
    with _lock:
        rows = conn().execute(
            "SELECT i.*, "
            "(SELECT COUNT(*) FROM interview_recordings r WHERE r.interview_id=i.id) AS recording_count, "
            "(SELECT COUNT(*) FROM interview_recordings r WHERE r.interview_id=i.id AND r.status='done') AS done_count "
            "FROM interviews i WHERE i.user_id=? ORDER BY i.updated DESC LIMIT ?",
            (user_id, max(1, min(int(limit or 30), 100)))).fetchall()
        return [dict(row) for row in rows]


def interview_recording_add(interview_id: str, user_id: str, file_id: str,
                            display_name: str = "") -> dict:
    """把一段已上传音频追加到某次面试，sequence 由数据库内现有最大值推导。"""
    interview = interview_get(interview_id, user_id)
    if interview is None:
        raise ValueError("面试档案不存在或不属于当前账号")
    now, rid = _now(), uuid.uuid4().hex
    with _lock:
        old = conn().execute(
            "SELECT * FROM interview_recordings WHERE interview_id=? AND file_id=?",
            (interview_id, file_id)).fetchone()
        if old is not None:              # 上传卡重试不能造出重复录音
            return dict(old)
        seq = conn().execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next_seq FROM interview_recordings WHERE interview_id=?",
            (interview_id,)).fetchone()["next_seq"]
        conn().execute(
            "INSERT INTO interview_recordings(id,interview_id,file_id,display_name,sequence,"
            "asr_job_id,transcript_file_id,asr_model,status,error_message,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, interview_id, file_id, display_name or "", int(seq), "", "", "", "uploaded", "", now, now))
        conn().execute("UPDATE interviews SET updated=? WHERE id=?", (now, interview_id))
        conn().commit()
        return dict(conn().execute("SELECT * FROM interview_recordings WHERE id=?", (rid,)).fetchone())


def interview_recording_get(recording_id: str, user_id: str) -> dict | None:
    """读取一段录音，同时经所属面试的 user_id 做鉴权。"""
    if not recording_id or not user_id:
        return None
    with _lock:
        row = conn().execute(
            "SELECT r.* FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE r.id=? AND i.user_id=?", (recording_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_recording_by_file(interview_id: str, user_id: str, file_id: str) -> dict | None:
    """从上传回传的 file_id 找到当前面试下对应录音。"""
    if not interview_id or not user_id or not file_id:
        return None
    with _lock:
        row = conn().execute(
            "SELECT r.* FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE r.interview_id=? AND r.file_id=? AND i.user_id=?",
            (interview_id, file_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_recording_by_job(job_id: str, user_id: str) -> dict | None:
    """按 ASR job 找录音；任务状态页/完成回写都用这条，不依赖会话最近任务。"""
    if not job_id or not user_id:
        return None
    with _lock:
        row = conn().execute(
            "SELECT r.* FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE r.asr_job_id=? AND i.user_id=?", (job_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_recordings(interview_id: str, user_id: str) -> list[dict]:
    """按用户确认过的 sequence 返回当前面试的全部录音。"""
    if interview_get(interview_id, user_id) is None:
        return []
    with _lock:
        rows = conn().execute(
            "SELECT * FROM interview_recordings WHERE interview_id=? ORDER BY sequence ASC",
            (interview_id,)).fetchall()
        return [dict(row) for row in rows]


def _refresh_interview_status(interview_id: str) -> None:
    """从录音状态推导档案状态；仅在调用者已持 _lock 时使用。"""
    rows = conn().execute(
        "SELECT status FROM interview_recordings WHERE interview_id=?", (interview_id,)).fetchall()
    states = {str(row["status"] or "") for row in rows}
    status = "transcribing" if "transcribing" in states else ("ready" if "done" in states else "draft")
    conn().execute("UPDATE interviews SET status=?,updated=? WHERE id=?", (status, _now(), interview_id))


def interview_recording_start(recording_id: str, user_id: str, job_id: str) -> dict | None:
    """把录音绑定到刚提交的 ASR job；同一录音不会被悄悄覆盖到另一条 job。"""
    rec = interview_recording_get(recording_id, user_id)
    if rec is None:
        return None
    with _lock:
        conn().execute(
            "UPDATE interview_recordings SET asr_job_id=?,status='transcribing',error_message='',updated=? WHERE id=?",
            (job_id, _now(), recording_id))
        _refresh_interview_status(rec["interview_id"])
        conn().commit()
    return interview_recording_get(recording_id, user_id)


def interview_recording_finish(recording_id: str, user_id: str, *, transcript_file_id: str = "",
                               asr_model: str = "", error_message: str = "",
                               expected_job_id: str = "") -> dict | None:
    """转写终态回写。失败只保存简短错误，完整异常仍在 job.log，避免表里塞长堆栈。"""
    rec = interview_recording_get(recording_id, user_id)
    if rec is None:
        return None
    ok = bool(transcript_file_id)
    with _lock:
        sql = (
            "UPDATE interview_recordings SET transcript_file_id=?,asr_model=?,status=?,error_message=?,updated=? "
            "WHERE id=?")
        args = [transcript_file_id or "", asr_model or rec.get("asr_model") or "",
                "done" if ok else "failed", (error_message or "")[:500], _now(), recording_id]
        if expected_job_id:
            sql += " AND asr_job_id=?"
            args.append(expected_job_id)
        cur = conn().execute(sql, args)
        if int(cur.rowcount or 0) == 0:
            return interview_recording_get(recording_id, user_id)  # 已被显式重试的新 job 接管
        _refresh_interview_status(rec["interview_id"])
        conn().commit()
    return interview_recording_get(recording_id, user_id)


def interview_set_speaker_map(interview_id: str, user_id: str, speaker_map: dict) -> dict | None:
    """保存用户确认的 speaker_0/1 身份映射；只收小 JSON，避免把转写全文塞入 PG。"""
    if interview_get(interview_id, user_id) is None:
        return None
    clean = {str(k)[:80]: str(v)[:40] for k, v in (speaker_map or {}).items()
             if str(k).strip() and str(v).strip()}
    with _lock:
        conn().execute("UPDATE interviews SET speaker_map=?,updated=? WHERE id=?",
                       (json.dumps(clean, ensure_ascii=False), _now(), interview_id))
        conn().commit()
    return interview_get(interview_id, user_id)


def interview_question_spans(question_id: str, user_id: str) -> list[dict]:
    """列某题的可追溯音频证据范围。"""
    question = interview_question_get(question_id, user_id)
    if question is None:
        return []
    with _lock:
        rows = conn().execute(
            "SELECT * FROM interview_question_spans WHERE question_id=? ORDER BY recording_id,start_ms",
            (question_id,)).fetchall()
        return [dict(row) for row in rows]


def interview_question_list(interview_id: str, user_id: str) -> list[dict]:
    """列当前用户某场面试的题目草稿/已确认题；不把其它面试混进来。"""
    if interview_get(interview_id, user_id) is None:
        return []
    with _lock:
        rows = conn().execute(
            "SELECT * FROM interview_questions WHERE interview_id=? ORDER BY sequence ASC",
            (interview_id,)).fetchall()
        return [dict(row) for row in rows]


def interview_question_get(question_id: str, user_id: str) -> dict | None:
    """按题目 id 读取，并经 interview.user_id 鉴权。"""
    if not question_id or not user_id:
        return None
    with _lock:
        row = conn().execute(
            "SELECT q.* FROM interview_questions q JOIN interviews i ON i.id=q.interview_id "
            "WHERE q.id=? AND i.user_id=?", (question_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_question_add_many(interview_id: str, user_id: str, items: list[dict]) -> list[dict]:
    """追加模型提取出的 Q&A 草稿；同题同答已存在时跳过，绝不覆盖人工确认内容。"""
    if interview_get(interview_id, user_id) is None:
        raise ValueError("面试档案不存在或不属于当前账号")
    clean = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question_text") or item.get("question") or "").strip()
        answer = str(item.get("answer_text") or item.get("answer") or "").strip()
        if not question or not answer:
            continue
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        clean.append({
            "question_text": question[:8000], "answer_text": answer[:16000],
            "followup_text": str(item.get("followup_text") or item.get("followup") or "").strip()[:8000],
            "category": str(item.get("category") or "").strip()[:100],
            "tags": json.dumps([str(x)[:80] for x in tags[:20]], ensure_ascii=False),
            "spans": item.get("spans") if isinstance(item.get("spans"), list) else [],
        })
    if not clean:
        return []
    now, added = _now(), []
    with _lock:
        rows = conn().execute(
            "SELECT question_text,answer_text FROM interview_questions WHERE interview_id=?",
            (interview_id,)).fetchall()
        exists = {(str(row["question_text"]).strip(), str(row["answer_text"]).strip()) for row in rows}
        seq = conn().execute(
            "SELECT COALESCE(MAX(sequence),0) AS max_seq FROM interview_questions WHERE interview_id=?",
            (interview_id,)).fetchone()["max_seq"]
        for item in clean:
            sig = (item["question_text"], item["answer_text"])
            if sig in exists:
                continue
            seq, qid = int(seq) + 1, uuid.uuid4().hex
            conn().execute(
                "INSERT INTO interview_questions(id,interview_id,sequence,category,question_text,"
                "answer_text,followup_text,status,tags,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (qid, interview_id, seq, item["category"], item["question_text"], item["answer_text"],
                 item["followup_text"], "draft", item["tags"], now, now))
            added.append(dict(conn().execute("SELECT * FROM interview_questions WHERE id=?", (qid,)).fetchone()))
            # spans 是可选证据，不是模型必须猜出的字段。若给了，必须确属当前面试的录音。
            for span in item.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                rid = str(span.get("recording_id") or "")
                rec = interview_recording_get(rid, user_id)
                if rec is None or rec["interview_id"] != interview_id:
                    continue
                try:
                    start, end = int(span.get("start_ms")), int(span.get("end_ms"))
                except (TypeError, ValueError):
                    continue
                if start < 0 or end < start:
                    continue
                conn().execute(
                    "INSERT INTO interview_question_spans(question_id,recording_id,start_ms,end_ms,speaker_role,created) "
                    "VALUES(?,?,?,?,?,?)",
                    (qid, rid, start, end, str(span.get("speaker_role") or "")[:40], now))
            exists.add(sig)
        if added:
            conn().execute("UPDATE interviews SET updated=? WHERE id=?", (now, interview_id))
        conn().commit()
    return added


def interview_question_set_status(question_id: str, user_id: str, status: str) -> dict | None:
    """题目状态仅允许 draft/confirmed/reviewed/skipped，防模型写入任意状态字。"""
    if status not in {"draft", "confirmed", "reviewed", "skipped"}:
        raise ValueError("题目状态不合法")
    question = interview_question_get(question_id, user_id)
    if question is None:
        return None
    with _lock:
        conn().execute("UPDATE interview_questions SET status=?,updated=? WHERE id=?",
                       (status, _now(), question_id))
        conn().commit()
    return interview_question_get(question_id, user_id)


def interview_question_update(question_id: str, user_id: str, *, question_text: str, answer_text: str,
                              category: str = "", followup_text: str = "") -> dict | None:
    q = interview_question_get(question_id, user_id)
    if q is None:
        return None
    if not (question_text or "").strip() or not (answer_text or "").strip():
        raise ValueError("问题和原回答都不能为空")
    with _lock:
        conn().execute("UPDATE interview_questions SET question_text=?,answer_text=?,category=?,followup_text=?,updated=? WHERE id=?",
                       ((question_text or "").strip()[:8000], (answer_text or "").strip()[:16000],
                        (category or "").strip()[:100], (followup_text or "").strip()[:8000], _now(), question_id))
        conn().commit()
    return interview_question_get(question_id, user_id)


def interview_question_merge(keep_id: str, merge_id: str, user_id: str) -> dict | None:
    keep, other = interview_question_get(keep_id, user_id), interview_question_get(merge_id, user_id)
    if not keep or not other or keep["interview_id"] != other["interview_id"] or keep_id == merge_id:
        raise ValueError("只能合并同一面试中的两道不同题目")
    if interview_reviews(other["id"], user_id):
        raise ValueError("被合并题已有复盘版本，请先人工编辑，避免丢失练习历史")
    with _lock:
        answer = keep["answer_text"] + "\n\n" + other["answer_text"]
        follow = "\n".join(x for x in (keep.get("followup_text"), other.get("followup_text")) if x)
        conn().execute("UPDATE interview_questions SET answer_text=?,followup_text=?,updated=? WHERE id=?",
                       (answer[:16000], follow[:8000], _now(), keep_id))
        conn().execute("UPDATE interview_question_spans SET question_id=? WHERE question_id=?", (keep_id, merge_id))
        conn().execute("UPDATE interview_actions SET question_id=? WHERE question_id=?", (keep_id, merge_id))
        conn().execute("DELETE FROM interview_questions WHERE id=?", (merge_id,))
        conn().commit()
    return interview_question_get(keep_id, user_id)


def interview_question_split(question_id: str, user_id: str, *, first_question: str, first_answer: str,
                             second_question: str, second_answer: str) -> tuple[dict, dict] | None:
    q = interview_question_get(question_id, user_id)
    if q is None:
        return None
    first = interview_question_update(q["id"], user_id, question_text=first_question, answer_text=first_answer,
                                      category=q.get("category") or "", followup_text=q.get("followup_text") or "")
    added = interview_question_add_many(q["interview_id"], user_id, [{
        "question_text": second_question, "answer_text": second_answer,
        "category": q.get("category") or "", "tags": json.loads(q.get("tags") or "[]"),
    }])
    return (first, added[0]) if added else None


def interview_review_add(question_id: str, user_id: str, *, self_reflection: str = "",
                         coach_feedback: str = "", improved_answer: str = "",
                         drill_prompt: str = "", source: str = "ai") -> dict | None:
    """为一题追加不可覆盖的复盘版本，方便比较“第一次改法”和之后的练习进步。"""
    question = interview_question_get(question_id, user_id)
    if question is None:
        return None
    now, rid = _now(), uuid.uuid4().hex
    with _lock:
        version = conn().execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM interview_reviews WHERE question_id=?",
            (question_id,)).fetchone()["next_version"]
        conn().execute(
            "INSERT INTO interview_reviews(id,question_id,version,self_reflection,coach_feedback,"
            "improved_answer,drill_prompt,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rid, question_id, int(version), (self_reflection or "")[:8000],
             (coach_feedback or "")[:16000], (improved_answer or "")[:16000],
             (drill_prompt or "")[:8000], (source or "ai")[:30], now, now))
        conn().execute("UPDATE interview_questions SET status='reviewed',updated=? WHERE id=?", (now, question_id))
        conn().commit()
        return dict(conn().execute("SELECT * FROM interview_reviews WHERE id=?", (rid,)).fetchone())


def interview_reviews(question_id: str, user_id: str) -> list[dict]:
    """读取一题的复盘版本历史；题目归属校验失败时返回空，避免越权查看。"""
    if interview_question_get(question_id, user_id) is None:
        return []
    with _lock:
        rows = conn().execute(
            "SELECT * FROM interview_reviews WHERE question_id=? ORDER BY version DESC", (question_id,)).fetchall()
        return [dict(row) for row in rows]


def interview_action_add(interview_id: str, user_id: str, title: str, *, detail: str = "",
                         question_id: str = "", due_at: float | None = None) -> dict | None:
    """新增下一次面试前的行动项；若绑定题目，必须同属这次面试。"""
    if interview_get(interview_id, user_id) is None:
        return None
    if question_id:
        question = interview_question_get(question_id, user_id)
        if question is None or question["interview_id"] != interview_id:
            raise ValueError("行动项绑定的题目不属于当前面试")
    now, aid = _now(), uuid.uuid4().hex
    with _lock:
        conn().execute(
            "INSERT INTO interview_actions(id,interview_id,question_id,title,detail,status,due_at,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, interview_id, question_id or None, (title or "").strip()[:300],
             (detail or "")[:8000], "todo", due_at, now, now))
        conn().commit()
        return dict(conn().execute("SELECT * FROM interview_actions WHERE id=?", (aid,)).fetchone())


def interview_actions(interview_id: str, user_id: str, status: str = "") -> list[dict]:
    """列某场面试的行动项；可按 todo/doing/done/dismissed 过滤。"""
    if interview_get(interview_id, user_id) is None:
        return []
    sql, args = "SELECT * FROM interview_actions WHERE interview_id=?", [interview_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY CASE status WHEN 'todo' THEN 0 WHEN 'doing' THEN 1 ELSE 2 END, updated DESC"
    with _lock:
        return [dict(row) for row in conn().execute(sql, args).fetchall()]


def interview_action_set_status(action_id: str, user_id: str, status: str) -> dict | None:
    """完成/搁置行动项；状态白名单防止模型写入任意值。"""
    if status not in {"todo", "doing", "done", "dismissed"}:
        raise ValueError("行动项状态不合法")
    with _lock:
        row = conn().execute(
            "SELECT a.* FROM interview_actions a JOIN interviews i ON i.id=a.interview_id "
            "WHERE a.id=? AND i.user_id=?", (action_id, user_id)).fetchone()
        if row is None:
            return None
        conn().execute("UPDATE interview_actions SET status=?,updated=? WHERE id=?",
                       (status, _now(), action_id))
        conn().commit()
        return dict(conn().execute("SELECT * FROM interview_actions WHERE id=?", (action_id,)).fetchone())


def interview_file_references(file_id: str, user_id: str) -> int:
    """统计某面试文件仍被多少录音行引用，物理删除前的最后一道保护。"""
    with _lock:
        row = conn().execute(
            "SELECT COUNT(*) AS n FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE i.user_id=? AND (r.file_id=? OR r.transcript_file_id=?)",
            (user_id, file_id, file_id)).fetchone()
        return int(row["n"] if row else 0)


def interview_recording_delete(recording_id: str, user_id: str) -> list[str]:
    """删除一段录音的业务记录，返回候选文件 id；物理文件由技能层在引用归零后删。"""
    rec = interview_recording_get(recording_id, user_id)
    if rec is None:
        return []
    files = [x for x in (rec.get("file_id"), rec.get("transcript_file_id")) if x]
    with _lock:
        conn().execute("DELETE FROM interview_question_spans WHERE recording_id=?", (recording_id,))
        conn().execute("DELETE FROM interview_recordings WHERE id=?", (recording_id,))
        _refresh_interview_status(rec["interview_id"])
        conn().commit()
    return files


def interview_delete(interview_id: str, user_id: str) -> list[str]:
    """删除整场面试及其关系记录，返回可安全检查后清理的文件 id。"""
    if interview_get(interview_id, user_id) is None:
        return []
    recs = interview_recordings(interview_id, user_id)
    files = [x for r in recs for x in (r.get("file_id"), r.get("transcript_file_id")) if x]
    with _lock:
        qrows = conn().execute("SELECT id FROM interview_questions WHERE interview_id=?", (interview_id,)).fetchall()
        qids = [r["id"] for r in qrows]
        for qid in qids:
            conn().execute("DELETE FROM interview_reviews WHERE question_id=?", (qid,))
            conn().execute("DELETE FROM interview_question_spans WHERE question_id=?", (qid,))
        conn().execute("DELETE FROM interview_actions WHERE interview_id=?", (interview_id,))
        conn().execute("DELETE FROM interview_questions WHERE interview_id=?", (interview_id,))
        conn().execute("DELETE FROM interview_recordings WHERE interview_id=?", (interview_id,))
        conn().execute("DELETE FROM interviews WHERE id=? AND user_id=?", (interview_id, user_id))
        conn().commit()
    return files


def interview_questions_across(user_id: str, interview_ids: list[str] | None = None) -> list[dict]:
    """跨面试洞察的数据源；只读用户显式选择的档案，默认最多最近 20 场。"""
    ids = [x for x in (interview_ids or []) if x]
    sql = ("SELECT q.*,i.title AS interview_title,i.company,i.role,i.round,i.occurred_at "
           "FROM interview_questions q JOIN interviews i ON i.id=q.interview_id WHERE i.user_id=?")
    args = [user_id]
    if ids:
        sql += " AND i.id IN (%s)" % ",".join("?" * len(ids)); args.extend(ids)
    sql += " ORDER BY i.occurred_at DESC,q.sequence ASC"
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


# ── 能力令牌（ref_links）：把文件临时开给外部服务抓取 ──────────────────
# ★ 这一组是**全站唯一能让"不登录的人"读到数据**的凭据，所以：
#   · 令牌不可猜（签发在 files/share.py，用的是 secrets 而不是 uuid）；
#   · **过期判断写进 SQL**，不在 Python 里比时间 —— 两边时钟/口径不一致时会
#     出现"库里说没过期、代码说过期"的扯皮，而这是安全问题，不能有解释空间；
#   · **未命中一律当作不存在**（调用方返 404，不要 403/410 区分"没有/过期了"，
#     那等于给枚举者一个"猜对了但过期了"的信号）。

def ref_link_add(token: str, fid: str, user_id: str, expires: float) -> None:
    """登记一条令牌。token 必须是**调用方生成的随机串**（本层不生成，方便单测）。"""
    with _lock:
        conn().execute(
            "INSERT INTO ref_links(token,fid,user_id,expires,created) VALUES(?,?,?,?,?) "
            "ON CONFLICT(token) DO UPDATE SET fid=excluded.fid, user_id=excluded.user_id, "
            "expires=excluded.expires, created=excluded.created",
            (token, fid, user_id or "", float(expires), _now()))
        conn().commit()


def ref_link_resolve(token: str) -> str:
    """令牌 → 文件 id；**不存在或已过期都返回空串**（调用方一律当 404）。"""
    if not token:
        return ""
    with _lock:
        row = conn().execute(
            "SELECT fid FROM ref_links WHERE token=? AND expires > ?",
            (token, _now())).fetchone()
        return (row["fid"] or "") if row else ""


def ref_link_revoke(tokens) -> int:
    """撤销若干令牌（任务跑完就该收回）。返回真删掉的行数。

    ★ 调用方**必须容错**：这个函数抛异常不能影响业务结果（见 video 的 build_runner）——
      一个已经生成成功的视频不该因为"撤销链接时数据库抽了一下"被判成失败。
    """
    toks = [t for t in (tokens or []) if t]
    if not toks:
        return 0
    with _lock:
        cur = conn().execute(
            "DELETE FROM ref_links WHERE token IN (%s)" % ",".join("?" * len(toks)),
            tuple(toks))
        conn().commit()
        return int(cur.rowcount or 0)


def ref_link_sweep() -> int:
    """清掉已过期的行（顺手做，不需要后台定时任务）。返回清掉几条。"""
    with _lock:
        cur = conn().execute("DELETE FROM ref_links WHERE expires <= ?", (_now(),))
        conn().commit()
        return int(cur.rowcount or 0)
