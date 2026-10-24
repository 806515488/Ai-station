"""选择本轮助手：L1/L2 判定、活跃技能保护，以及批准工具的归属反查。

这里只选择“谁来回答”，可能调用意图识别模型，但不调用主回答模型或业务工具。
具体判词继续复用 core.router；返回选择结果后，再由 assembly 装配并进入 Loop。
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
    """路由结果：agent 是实际选用对象，level/skill_id/diagnostics 用于解释与追踪。"""
    agent: Any
    level: str = "fallback"
    skill_id: str = ""
    duration_ms: float = 0.0
    diagnostics: dict = field(default_factory=dict)


def select_agent(turn, catalog, generic) -> Selection:
    """根据本次消息和会话当前技能选择助手。"""
    available = {entry.id for entry in catalog}
    diagnostics: dict = {}
    started = time.perf_counter()
    with obs.span("route", "意图识别") as step:
        # L1 先按规则匹配；没有明确命中才走 L2 意图模型，避免每句话都发一次识别请求。
        route = l1_route(turn.message, available)
        if route is None:
            route = l2_route(turn.message, catalog, diagnostics, turn.user_id)
        step.update(level=route.level if route else "fallback",
                    skill=route.skill_id if route else "", reason=diagnostics.get("reason", ""))
    duration = round((time.perf_counter() - started) * 1000, 1)

    # “当前技能”帮助追问保持连贯；保护规则不允许的跳转，继续沿用有效的原技能。
    active = turn.active_skill
    skill_id = route.skill_id if route else ""
    if not may_switch_to(turn.message, skill_id, active):
        skill_id = active if active in available else ""
    if skill_id and skill_id in available:
        agent = get_skill(skill_id) or generic
    elif route is not None and route.skill_id == "":
        agent = generic
    else:
        agent = get_skill(active) if active in available else generic
    # agent 是保护/回退后的实际选择；skill_id 保留原路由判断，二者可能不同。
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
