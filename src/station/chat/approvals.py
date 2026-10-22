"""处理上一轮待批准操作：判定用户回答、补执行、保存协议配对和卡片。

只处理已持久化的 pending，不从本次请求接受工具名/参数。
工具结果与 pending 清除在发送卡片前保存，避免卡片刚发出就断连而留下旧批准状态。
"""
from dataclasses import dataclass, field
from typing import Any
import uuid

from station.core import guards
from station.core.agent import run_tool
from station.core.events import Event
from station.chat.history import remember_card
from station.chat.routing import agent_for_tool
from station.chat.runtime import create_context


@dataclass
class ApprovalResult:
    agent: Any = None
    context: Any = None
    stop: bool = False
    events: list[Event] = field(default_factory=list)


def resolve_pending(thread, message: str, generic) -> ApprovalResult:
    pending = thread.pending
    if not pending:
        return ApprovalResult()
    answer = guards.classify_answer(message)
    if not answer:
        thread.save()
        return ApprovalResult(stop=True, events=[
            Event("delta", {"text": "上一操作仍在等待确认，请回复：允许 / 拒绝。\n"}),
            Event("done"),
        ])

    agent = agent_for_tool(pending.get("tool") or "") or generic
    context = create_context(thread, agent)
    tool = next((tool for tool in agent.tools if tool.name == pending.get("tool")), None)
    if tool is None:
        result = f"工具 {pending['tool']} 已不存在"
    elif answer == "allow":
        result = run_tool(context, tool, pending.get("arguments") or {})
    else:
        result = "用户拒绝了该操作。"

    events = [event for event in context.events if event.type == "render"]
    for event in events:
        remember_card(thread, event.data.get("card"))
    context.events.clear()
    call_id = "a" + uuid.uuid4().hex[:10]
    # 模型协议要求 assistant 调用与 tool 结果配对；卡片保持工具结果前的原位置锚点。
    thread.add({"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "name": pending["tool"], "arguments": pending.get("arguments") or {}}]})
    thread.add({"role": "tool", "tool_call_id": call_id, "name": pending["tool"], "content": result})
    thread.clear_pending()
    thread.save()
    return ApprovalResult(agent=agent, context=context, events=events)
