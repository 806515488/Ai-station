"""会话历史：本人会话列表、消息/卡片回放和工具显示名。"""
from __future__ import annotations

from station.persistence.repositories import threads as thread_records


from fastapi import APIRouter, HTTPException, Request

from station.core.session import Thread
from station.app.auth import require_user
from station.chat.routing import agent_for_tool

router = APIRouter(tags=["会话历史"])


def _tool_label(tool_name: str, user_id: str = "") -> str:
    """工具全名 → **给人看的一句话**（工具在 Tool.label 里声明的）；查不到给空串。

    历史回放需要它：thread 里只存了工具名，而"该怎么跟用户说"是**工具自己**的事
    （宿主不认识业务），所以回注册表问一遍。空串时前端退回显示工具叶子名。
    """
    # 全局工具先查（它们不在技能注册表里）
    from station.tools import GLOBAL_TOOLS
    for t in GLOBAL_TOOLS:
        if t.name == tool_name:
            return getattr(t, "label", "") or ""
    # ★ 外部 MCP 工具（mcp.* 开头）也不属于任何技能 —— 它们是「每轮都在手上」的那类，
    #   每人一份现算（见 mcp/bridge.py）。查不到就算了，前端会退回显示叶子名。
    if user_id:
        try:
            from station.mcp import bridge
            for t in bridge.mcp_tools(user_id):
                if t.name == tool_name:
                    return getattr(t, "label", "") or ""
        except Exception:                  # noqa：查个显示名而已，坏了不该影响回放
            pass
    s = agent_for_tool(tool_name)
    if s is None:
        return ""
    # ★ 按**完整名**比，别按"第一个点后面的那串"（09-16 改）。
    #   老写法 `str(tool_name).split(".", 1)[-1]` 在单段命名空间下等于叶子名，
    #   但 `mcp.a.b.search` 会取成 `a.b.search`，跟 `t.leaf()` 的 `search` 对不上
    #   → 永远返回空串 → 历史回放里那一行显示成内部工具名（用户看不懂）。
    #   历史里可能存着裸叶子名，所以再兜一层按叶子名比。
    leaf = str(tool_name).rsplit(".", 1)[-1]
    for t in (getattr(s, "tools", None) or []):
        if t.name == tool_name or t.leaf() == leaf:
            return getattr(t, "label", "") or ""
    return ""


@router.get("/api/sessions")
def sessions(request: Request, skill: str = ""):
    """列出当前登录用户的历史会话摘要（可选按技能过滤 ?skill=xxx）。"""
    u = require_user(request)
    return {"sessions": thread_records.thread_list(u["id"], skill or None)}


@router.get("/api/sessions/{tid}")
def session_detail(tid: str, request: Request):
    """看某个会话的完整消息（只能看自己的）。"""
    u = require_user(request)
    t = Thread.load(tid)
    if t is None or getattr(t, "user_id", "") != u["id"]:
        raise HTTPException(404, "会话不存在")   # 抛 404 ≈ Java 里抛特定异常→框架转成 HTTP 状态码
    if t.pending and not t.pending.get("id"):
        # 一次性兼容旧数据：刷新后获得稳定 ID，旧按钮不会被自动绑定到新操作。
        t = Thread.from_record(thread_records.ensure_pending_id(t.id, u["id"]))
    meta = t.meta or {}
    view = _replay_items(t.msgs, meta.get("cards") or [], u["id"])
    # ★ pending 要带上 **label**：刷新页面时前端要重画那张批准卡，而卡片上显示的是
    #   人话标签不是内部工具名 —— 不带的话重画出来又是 `archive.xxx`（或者干脆画不出来，
    #   用户会卡在"agent 在等允许、屏幕上什么都没有"）。
    pend = t.pending
    if pend:
        pend = {**pend, "label": _tool_label(pend.get("tool") or "", u["id"])}
    return {"id": t.id, "skill_id": t.skill_id, "msgs": view,
            "pending": pend,
            "job_id": meta.get("job_id") or "",
            "cards": meta.get("cards") or []}


def _replay_items(msgs: list, cards: list, user_id: str = "") -> list:
    """按“真实发生顺序”把文本/工具事件/卡片交错成回放列表。

    原始 thread.msgs 是给模型看的（user/assistant/tool 消息），而页面还出现过
    工具调用行和渲染卡片。为了让刷新后的顺序跟当时对话一致，这里按消息位置
    合成 UI 顺序：assistant 工具调用 → “调用工具”行，卡片插在它所在工具结果之后。
    """
    items: list = []
    # 附件卡锚定用户消息，不受普通卡片只保留最近 30 张的限制，也不漂到回答后面。
    cards = sorted([c for c in cards if (c.get("card") or {}).get("type") != "attachments"],
                   key=lambda c: int(c.get("idx") or 0))
    ci = 0
    for pos, m in enumerate(msgs):
        role = m.get("role") or ""
        content = m.get("content") or ""
        if role == "assistant" and m.get("tool_calls"):
            # 模型“边说话边调工具”时，那句前言也要回放出来（实时显示过，刷新后不能丢）
            if content.strip():
                items.append({"role": "assistant", "content": content})
            calls = [c for c in m["tool_calls"] if c.get("name")]
            if calls:
                # 只给**结构化数据**（名字/label/参数），"怎么显示成人话"交给前端 ——
                # 实时那条路（core/agent 的 EV_TOOL）发的也是这个形状，两边共用同一个
                # 渲染函数。以前这里和前端各拼一份"→ 调用工具：…"，改一处忘一处。
                items.append({"role": "tool", "content": "", "tool_call": {
                    "names": [str(c.get("name")) for c in calls],
                    "labels": [_tool_label(str(c.get("name")), user_id) for c in calls],
                    "args": [c.get("arguments") or {} for c in calls]}})
        elif role in ("user", "assistant") and content:
            items.append({"role": role, "content": content, **({"trace_id": m["trace_id"]} if m.get("trace_id") else {})})
        # 卡片 idx 记的是“该工具结果出现的位置”，处理完这一条后再插入
        while ci < len(cards) and int(cards[ci].get("idx") or 0) <= pos:
            card = cards[ci].get("card")
            if card:
                items.append({"role": "render", "content": "", "card": card})
            ci += 1
    while ci < len(cards):              # 兜底：异常/旧数据 idx 超出范围也补在末尾
        card = cards[ci].get("card")
        if card:
            items.append({"role": "render", "content": "", "card": card})
        ci += 1
    return items
