"""显式升级 PostgreSQL schema；不由 Web 请求自动触发。"""
from __future__ import annotations

import argparse

from station import config, db


def main() -> None:
    parser = argparse.ArgumentParser(
        description="升级 station PostgreSQL schema（执行前先停掉所有 station 实例）")
    parser.add_argument("--database-url", default="",
                        help="PostgreSQL URL；默认读取 STATION_DATABASE_URL")
    args = parser.parse_args()
    url = args.database_url or config.database_url()
    if not url:
        parser.error("缺少 PostgreSQL URL：设置 STATION_DATABASE_URL 或传 --database-url")
    c = db.connect_postgres(url)
    try:
        result = db.upgrade_postgres_schema(c)
    finally:
        c.close()
    print(f"PostgreSQL schema：{result}")


if __name__ == "__main__":
    main()
