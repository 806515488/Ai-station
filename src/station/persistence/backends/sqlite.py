"""SQLite 连接配置：行按列名访问，跨线程访问由 Database 的同一把锁保护。"""
import sqlite3
from pathlib import Path


def connect_sqlite(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection
