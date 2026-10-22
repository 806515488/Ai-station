"""settings 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
from station.persistence import database


def model_config_save(user_id: str, cfg: dict) -> None:
    """保存（覆盖）某用户的模型配置整包。"""
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO model_configs(user_id,cfg,updated) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET cfg=excluded.cfg, "
            "updated=excluded.updated",
            (user_id, json.dumps(cfg, ensure_ascii=False), database.now()))


def model_config_load(user_id: str) -> dict | None:
    """读某用户的模型配置；从没存过返回 None（**不等于**"用默认"）。

    注意这个返回值里可能含明文 api_key——只许喂给 modelcfg 的解析/掩码函数，
    绝不能直接扔进 HTTP 响应或日志。
    """
    if not user_id:
        return None
    with database.session() as connection:
        row = connection.execute("SELECT cfg FROM model_configs WHERE user_id=?",
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
    with database.session(write=True) as connection:
        connection.execute("DELETE FROM model_configs WHERE user_id=?", (user_id,))


def mcp_config_save(user_id: str, cfg: dict) -> None:
    """保存（覆盖）某用户的 MCP 配置整包。"""
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO mcp_servers(user_id,cfg,updated) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET cfg=excluded.cfg, "
            "updated=excluded.updated",
            (user_id, json.dumps(cfg, ensure_ascii=False), database.now()))


def mcp_config_load(user_id: str) -> dict | None:
    """读某用户的 MCP 配置；从没存过返回 None（**不等于**"没有 server"）。"""
    if not user_id:
        return None
    with database.session() as connection:
        row = connection.execute("SELECT cfg FROM mcp_servers WHERE user_id=?",
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
    with database.session(write=True) as connection:
        connection.execute("DELETE FROM mcp_servers WHERE user_id=?", (user_id,))
