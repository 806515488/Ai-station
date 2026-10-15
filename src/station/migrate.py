"""把旧 SQLite 元数据一次性、安全地导入 PostgreSQL。

新手视角：这不是应用启动时自动做的"顺手搬家"，而是一个必须由运维显式执行的
一次性工具。它只搬 station.db 内的用户、会话、任务和现场状态；照片、音频和导出文件
仍在 data/ 卷中，路径不变。目标库必须为空，导入后逐表核对行数，SQLite 原文件不会修改。

用法（在有应用代码与 psycopg 的环境中）：
    STATION_DATABASE_URL='postgresql://...' station-migrate-postgres
    station-migrate-postgres --source /app/data/station/station.db
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from station import config, db


# 固定表名和列名，不从命令行接标识符：SQL 参数只能保护“值”，不能保护表名。
_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("users", ("id", "name", "pw_hash", "salt", "created", "seq")),
    ("threads", ("id", "user_id", "skill_id", "msgs", "meta", "created", "updated")),
    ("jobs", ("id", "user_id", "skill_id", "args", "status", "progress", "message",
              "log", "artifacts", "result", "created", "updated")),
    ("archive_states", ("project", "channel", "job_id", "user_id", "person", "state", "updated")),
    ("corrections", ("id", "project", "user_id", "kind", "payload", "ts")),
    ("checkpoints", ("id", "project", "channel", "user_id", "label", "state", "created")),
    ("model_configs", ("user_id", "cfg", "updated")),
    ("mcp_servers", ("user_id", "cfg", "updated")),
    ("ref_links", ("token", "fid", "user_id", "expires", "created")),
    ("station_documents", ("id", "user_id", "upload_key", "local_date", "sequence", "record", "created", "updated")),
    ("station_attachment_turns", ("id", "user_id", "thread_id", "client_key", "state", "record", "created", "updated")),
)


def _source_counts(source: sqlite3.Connection) -> dict[str, int]:
    """读取源库各表行数；老库缺某张后加的表时按 0 对待。"""
    present = {r[0] for r in source.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {name: (int(source.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                   if name in present else 0)
            for name, _ in _TABLES}


def _select_exprs(source: sqlite3.Connection, name: str,
                  columns: tuple[str, ...]) -> str:
    """拼 SELECT 的表达式列表；老库缺的列在这里补出来。

    ★ ``users.seq``（插入序，owner_id 的平局判据）是 2026-10 才加的列：更早的
      station.db 里没有它，照直 SELECT 会 "no such column"。老库的插入序正好就是
      SQLite 的 rowid，拿它顶上即可（这正是 seq 这一列的语义）。
    """
    have = {r[1] for r in source.execute(f"PRAGMA table_info({name})")}
    exprs = []
    for column in columns:
        if column in have:
            exprs.append(column)
        elif name == "users" and column == "seq":
            exprs.append("rowid AS seq")
        else:
            raise RuntimeError(f"源库 {name} 缺列 {column}，无法迁移")
    return ",".join(exprs)


def migrate(source_path: Path, database_url: str, *, dry_run: bool = False) -> dict[str, int]:
    """执行导入并返回逐表行数；目标非空或校验失败时抛错且不碰源 SQLite。"""
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到 SQLite 源库：{source_path}")
    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row
    target = None
    try:
        counts = _source_counts(source)
        if dry_run:
            return counts

        target = db.connect_postgres(database_url)
        db.init_postgres(target)
        existing = {
            name: int(target.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
            for name, _ in _TABLES
        }
        nonempty = {name: n for name, n in existing.items() if n}
        if nonempty:
            raise RuntimeError(
                "目标 PostgreSQL 不是空库，拒绝覆盖："
                + "，".join(f"{name}={n}" for name, n in nonempty.items()))

        # ★★ 先把上面那几条 SELECT COUNT(*) 开出来的**隐式事务**提交掉。
        #   psycopg 的连接默认不是 autocommit：任何 execute() 都会开一个事务，而
        #   在一个**已开着**的事务里再进 transaction() 只会建 SAVEPOINT —— 退出时
        #   释放 savepoint 是**不会提交外层事务**的，随后 close() 把未提交的事务
        #   一滚，数据就没了。症状极具欺骗性：**"导入完成、行数校验也对"**（校验读的
        #   正是同一事务内那批还没落库的数据），而库里其实是空的。离线测试看不见
        #   这一层（它连不上 PG），靠 tests/test_postgres_integration.py 才逮到。
        target.raw.commit()

        # 一个事务搬完：中间任意一张表失败，整批 INSERT 回滚，不会留下半库。
        with target.raw.transaction():
            present = {r[0] for r in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for name, columns in _TABLES:
                if name not in present:
                    continue
                names = ",".join(columns)
                marks = ",".join("?" for _ in columns)
                select = _select_exprs(source, name, columns)
                for row in source.execute(f"SELECT {select} FROM {name}"):
                    target.execute(
                        f"INSERT INTO {name}({names}) VALUES({marks})",
                        tuple(row[column] for column in columns))

            # 显式导入 corrections/checkpoints 的旧 id 后，把 identity 序列拨到末尾；
            # 否则下一次应用新建检查点可能撞上已经迁进来的 id。
            for name in ("corrections", "checkpoints"):
                target.execute(
                    f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {name}), 1), true)")

        verified = {
            name: int(target.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
            for name, _ in _TABLES
        }
        if verified != counts:
            raise RuntimeError(f"导入后行数不一致：源={counts}，目标={verified}")
        return verified
    finally:
        source.close()
        if target is not None:
            target.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="把 station.db 元数据导入一个空 PostgreSQL 库")
    parser.add_argument("--source", type=Path,
                        default=config.DATA_DIR / "station.db",
                        help="SQLite 源库路径（默认 data/station/station.db）")
    parser.add_argument("--database-url", default="",
                        help="PostgreSQL URL；更推荐通过 STATION_DATABASE_URL 环境变量传入")
    parser.add_argument("--dry-run", action="store_true", help="只显示源库逐表行数，不连接 PostgreSQL")
    args = parser.parse_args()
    url = args.database_url or config.database_url()
    if not args.dry_run and not url:
        parser.error("缺少 PostgreSQL URL：设置 STATION_DATABASE_URL 或传 --database-url")
    counts = migrate(args.source, url, dry_run=args.dry_run)
    label = "源库检查" if args.dry_run else "导入完成并已校验"
    print(label + "：" + "，".join(f"{name}={n}" for name, n in counts.items()))


if __name__ == "__main__":
    main()
