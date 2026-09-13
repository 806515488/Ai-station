"""station.db —— SQLite 持久层（单用户工作站升级为"多用户可辨"的第一步）。

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
  - sqlite3 是 Python 标准库自带的"嵌入式数据库"——不用装服务，一个 .db 文件
    就是一个库（≈ H2/SQLite for Java），零部署成本，单用户/小团队正合适。
  - 连接用 check_same_thread=False + 一把线程锁：FastAPI 的请求来自多个线程，
    SQLite 连接默认只许创建它的线程用；这里全站共用一个连接 + threading.Lock
    串行化写（单用户量级下足够，比每请求开连接简单可靠）。
  - row_factory=sqlite3.Row：让查询结果能按列名取值 row["name"]（≈ ResultSet）。
"""
from __future__ import annotations

import hashlib     # 密码摘要（sha256 + 每用户随机盐）
import json
import secrets     # 生成随机盐
import sqlite3
import threading
import time
import uuid

from station import config

DB_PATH = config.DATA_DIR / "station.db"     # 库文件：data/station/station.db
_conn: sqlite3.Connection | None = None
# 全站**数据库互斥锁**。★ 它不只管写，读也必须拿 —— 全站共用一个 sqlite3 连接，
# 而"一个连接被多线程同时用"是**未定义行为**：实测 1.5 秒里能撞出 783 次异常
# （sqlite3.InterfaceError: bad parameter / no more rows available），**而且读到的行
# 会是错乱的**（json.loads 收到 None、行元组长度不对）——不只是报错，是数据看起来
# "莫名其妙没了"。用 RLock：万一某个已持锁的函数又调了另一个拿锁的函数，同线程重入
# 不会死锁。
_lock = threading.RLock()


def _now() -> float:
    return time.time()


def conn() -> sqlite3.Connection:
    """取全站唯一的 SQLite 连接（懒建 + 首次自动建表）。"""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互斥（浏览器轮询时 worker 在写，不卡读）
        _conn.execute("PRAGMA journal_mode=WAL")
        init_db(_conn)
    return _conn


def init_db(c: sqlite3.Connection) -> None:
    """建表（IF NOT EXISTS：已有库重复启动也不炸、不丢数据）。"""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id       TEXT PRIMARY KEY,
        name     TEXT UNIQUE NOT NULL,
        pw_hash  TEXT NOT NULL,
        salt     TEXT NOT NULL,
        created  REAL NOT NULL
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
    CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id, updated);
    CREATE INDEX IF NOT EXISTS idx_jobs_user    ON jobs(user_id, updated);
    CREATE INDEX IF NOT EXISTS idx_states_user  ON archive_states(user_id, updated);
    CREATE INDEX IF NOT EXISTS idx_ck_project   ON checkpoints(project, id);
    """)
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
                "INSERT INTO users(id,name,pw_hash,salt,created) VALUES(?,?,?,?,?)",
                (uid, name.strip(), _hash(pw, salt), salt, _now()))
            conn().commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"用户名已存在：{name}")
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

    排序以 created 为主、rowid 兜底：created 是 REAL 时间戳，同一秒注册两个账号时
    光靠它分不出先后；rowid 是 SQLite 的自增插入序，正好当平局判据。
    """
    with _lock:
        row = conn().execute(
            "SELECT id FROM users ORDER BY created ASC, rowid ASC LIMIT 1").fetchone()
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


def job_load(job_id: str):
    """按 id 读回 manager.Job 对象；没有返回 None。"""
    from station.jobs.manager import Job
    with _lock:
        row = conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        j = Job(row["skill_id"], json.loads(row["args"]))
        j.id = row["id"]
        j.status, j.progress = row["status"], row["progress"]
        j.message, j.created, j.updated = row["message"], row["created"], row["updated"]
        j.log = json.loads(row["log"])
        j.artifacts = json.loads(row["artifacts"])
        j.result = json.loads(row["result"]) if row["result"] else None
        j.user_id = row["user_id"]
        return j


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
        cur = conn().execute(
            "INSERT INTO checkpoints(project,channel,user_id,label,state,created) "
            "VALUES(?,?,?,?,?,?)",
            (project, channel or "", user_id, label,
             json.dumps(state, ensure_ascii=False), _now()))
        conn().commit()
    return cur.lastrowid


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
