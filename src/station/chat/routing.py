"""选择本轮助手：L1/L2 判定、活跃技能保护，以及批准工具的归属反查。

这里只选择“谁来执行”，不执行模型或工具。具体判词继续复用 core.router。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from station import observability as obs
from station.core.router import l1_route, l2_route, may_switch_to
from station.skills.registry import get_registry
from station.chat.catalog import build_generic_agent, get_skill


@dataclass
class Selection:
    agent: Any
    level: str = "fallback"
    skill_id: str = ""
    duration_ms: float = 0.0
    diagnostics: dict = field(default_factory=dict)


def select_agent(turn, catalog, generic) -> Selection:
    """仅回答模式用通用助手；其他情况按路由和会话状态选择技能。"""
    if turn.reply_only:
        return Selection(generic)
    available = {entry.id for entry in catalog}
    diagnostics: dict = {}
    started = time.perf_counter()
    with obs.span("route", "意图识别") as step:
        route = l1_route(turn.message, available)
        if route is None:
            route = l2_route(turn.message, catalog, diagnostics, turn.user_id)
        step.update(level=route.level if route else "fallback",
                    skill=route.skill_id if route else "", reason=diagnostics.get("reason", ""))
    duration = round((time.perf_counter() - started) * 1000, 1)

    # 附件恢复沿用提交时冻结的技能；普通续聊读取会话里的活跃技能。
    active = (turn.attachment.get("active_skill", "") if turn.attachment else
              turn.thread.meta.get("active_skill", ""))
    skill_id = route.skill_id if route else ""
    if not may_switch_to(turn.message, skill_id, active):
        skill_id = active if active in available else ""
    if skill_id and skill_id in available:
        agent = get_skill(skill_id) or generic
    elif route is not None and route.skill_id == "":
        agent = generic
    else:
        agent = get_skill(active) if active in available else generic
    return Selection(agent, route.level if route else "fallback",
                     route.skill_id if route else "", duration, diagnostics)


def agent_for_tool(tool_name: str):
    """按最长命名空间匹配工具所属技能；station.* 属于通用助手。"""
    name = str(tool_name or "")
    if not name:
        return None
    if name.split(".", 1)[0] == "station":
        return build_generic_agent()
    best = None
    for skill in get_registry().list():
        namespace = getattr(skill, "ns", "") or ""
        if namespace and (name == namespace or name.startswith(namespace + ".")):
            if best is None or len(namespace) > len(best.ns or ""):
                best = skill
    return best
