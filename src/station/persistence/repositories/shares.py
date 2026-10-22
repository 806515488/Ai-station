"""shares 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

from station.persistence import database


def ref_link_add(token: str, fid: str, user_id: str, expires: float) -> None:
    """登记一条令牌。token 必须是**调用方生成的随机串**（本层不生成，方便单测）。"""
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO ref_links(token,fid,user_id,expires,created) VALUES(?,?,?,?,?) "
            "ON CONFLICT(token) DO UPDATE SET fid=excluded.fid, user_id=excluded.user_id, "
            "expires=excluded.expires, created=excluded.created",
            (token, fid, user_id or "", float(expires), database.now()))


def ref_link_resolve(token: str) -> str:
    """令牌 → 文件 id；**不存在或已过期都返回空串**（调用方一律当 404）。"""
    if not token:
        return ""
    with database.session() as connection:
        row = connection.execute(
            "SELECT fid FROM ref_links WHERE token=? AND expires > ?",
            (token, database.now())).fetchone()
        return (row["fid"] or "") if row else ""


def ref_link_revoke(tokens) -> int:
    """撤销若干令牌（任务跑完就该收回）。返回真删掉的行数。

    ★ 调用方**必须容错**：这个函数抛异常不能影响业务结果（见 video 的 build_runner）——
      一个已经生成成功的视频不该因为"撤销链接时数据库抽了一下"被判成失败。
    """
    toks = [t for t in (tokens or []) if t]
    if not toks:
        return 0
    with database.session(write=True) as connection:
        cur = connection.execute(
            "DELETE FROM ref_links WHERE token IN (%s)" % ",".join("?" * len(toks)),
            tuple(toks))
        return int(cur.rowcount or 0)


def ref_link_sweep() -> int:
    """清掉已过期的行（顺手做，不需要后台定时任务）。返回清掉几条。"""
    with database.session(write=True) as connection:
        cur = connection.execute("DELETE FROM ref_links WHERE expires <= ?", (database.now(),))
        return int(cur.rowcount or 0)
