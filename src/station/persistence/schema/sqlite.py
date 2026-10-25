"""SQLite 初始化与旧结构补列；只接收显式连接，避免访问任何全局连接。"""
from __future__ import annotations
import sqlite3
from .tables import _trace_schema, _task_schema
from .task_upgrade import upgrade_jobs

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
        work_status TEXT NOT NULL,
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
    for statement in _trace_schema() + _task_schema():
        c.execute(statement)
    _migrate(c)
    upgrade_jobs(c)
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
