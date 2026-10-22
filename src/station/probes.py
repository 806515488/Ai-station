"""连接测试共用限流与元数据地址黑名单；模型、搜索和 MCP 探针共享同一用户预算。"""
from __future__ import annotations

import time

_TEST_HITS: dict[str, list] = {}


BLOCKED_HOSTS = {"169.254.169.254", "100.100.100.200", "metadata.google.internal"}


def test_allowed(user_id: str, per_minute: int = 10) -> bool:
    """同一用户每分钟最多测几次（防有人拿它当免费压测/烧钱工具）。"""
    now = time.time()
    hits = [t for t in _TEST_HITS.get(user_id, []) if now - t < 60]
    if len(hits) >= per_minute:
        _TEST_HITS[user_id] = hits
        return False
    hits.append(now)
    _TEST_HITS[user_id] = hits
    return True
