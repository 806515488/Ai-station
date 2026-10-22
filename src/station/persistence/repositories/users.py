"""users 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

from station.persistence import database
from station.persistence.errors import DuplicateKey
import sqlite3


def insert_user(uid: str, name: str, password_hash: str, salt: str, created: float) -> dict:
    """保存已处理的凭据；不接收明文密码，也不承担注册规则。"""
    try:
        with database.session(write=True) as connection:
            connection.execute(
                "INSERT INTO users(id,name,pw_hash,salt,created,seq) "
                "VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(seq),0)+1 FROM users))",
                (uid, name, password_hash, salt, created))
    except Exception as error:
        if isinstance(error, sqlite3.IntegrityError) or getattr(error, "sqlstate", None) == "23505":
            raise DuplicateKey(name) from error
        raise
    return {"id": uid, "name": name}


def credentials_by_name(name: str) -> dict | None:
    """仅供认证服务使用；凭据记录不能直接返回给 HTTP 客户端。"""
    with database.session() as connection:
        row = connection.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def get_user(uid: str) -> dict | None:
    """按 id 查用户（cookie 里只存 uid，每次请求回查——改密码/删号立即生效）。"""
    with database.session() as connection:
        row = connection.execute("SELECT id,name FROM users WHERE id=?", (uid,)).fetchone()
        return {"id": row["id"], "name": row["name"]} if row else None


def user_count() -> int:
    """用户总数（前端据此显示"首个用户=站长"提示）。"""
    with database.session() as connection:
        return connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def owner_id() -> str | None:
    """站长的 user id = **最早注册的那个账号**；一个用户都没有时返回 None。

    排序以 created 为主、**seq（插入序）**兜底：seq 是显式存的一列，SQLite 与
    PostgreSQL 都可用。★ 不能用 id 顶替 —— uuid4 生成的片段**稳定但不保序**，
    那样同刻注册的两个账号会退化成"随机挑一个当站长"；也不能再用 SQLite 专有的 rowid。
    """
    with database.session() as connection:
        row = connection.execute(
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
