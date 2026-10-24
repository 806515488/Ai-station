"""批准接续：只消费通用历史、工具与策略，不解释用户原话、不保存数据库。

由 AgentExecution 在模型初始化前驱动。工具副作用、结果入历史、检查点、卡片
按顺序执行；yield 检查点后必须等待会话层保存成功，才能继续。
"""
from station.core.contracts import (APPROVAL_REMINDER, CheckpointRequest, HistoryAnchor,
                                    Outcome, ToolInvocation, identity)
from station.core.events import Event


def publish_tool_result(history, call_id, name, text, presentations=(), *, item_id="", clear_pending=False):
    """普通调用和批准补调用共用结果提交顺序；真正保存仍由外层消费检查点完成。"""
    history.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": text}, item_id=item_id)
    if clear_pending:
        history.clear_pending()
    yield CheckpointRequest("tool_result", history.revision, presentations)
    for presentation in presentations:
        yield presentation.event


def resume_pending(history, tools, policy, decision):
    """完成原待批准操作，返回 None 表示可继续；返回 Outcome 表示本次应停止。"""
    pending = history.get_pending()
    if not pending:
        return None
    if decision is None:
        # 直接调用内核也必须守住批准；这条分支不访问工具/模型、不改 pending。
        yield Event("delta", {"text": APPROVAL_REMINDER})
        return Outcome("waiting_approval", "approval_reminder", {})

    # 参数来自已保存的 pending，不能由本次用户回复替换。
    name, arguments = pending["tool"], pending.get("arguments") or {}
    pair_id, call_id = identity(), "a" + identity()[:10]
    presentations = ()
    if tools.spec(name) is None:
        text = f"工具 {name} 已不存在"
    elif decision == "allow":
        if not policy.allow_tools:
            return Outcome("failed", "tool_policy_violation")
        result = tools.execute(ToolInvocation(call_id, name, arguments, HistoryAnchor(pair_id)))
        text, presentations = result.model_text, result.presentation_events
    else:
        text = "用户拒绝了该操作。"

    # 批准卡片沿用原 assistant 配对项锚点；普通工具卡片仍挂在 tool 结果项。
    history.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "name": name, "arguments": arguments}]}, item_id=pair_id)
    yield from publish_tool_result(history, call_id, name, text, presentations, clear_pending=True)
    # 不消耗新 Loop 的步骤/模型调用计数，也不给后续 approve 工具自动批准权。
    return None
