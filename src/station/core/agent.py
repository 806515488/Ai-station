"""Agent 执行控制：批准接续→模型初始化→模型/工具循环，不选择业务、不写数据库。

CheckpointRequest 要求调用方可靠保存后再继续。Outcome 说明结束原因，Event 呈现过程。

从 _loop 看核心机制：组装消息 → 请求模型 → 直接回答或执行工具 → 把结果放回历史 → 下一步。
一次用户回答可包含多次模型请求；这里的“步数”是循环次数，不是用户对话轮数。
"""
from __future__ import annotations
from contextlib import contextmanager
from station.core import compact
from station.core.contracts import (CheckpointRequest, ExecutionPolicy, HistoryAnchor, HistoryItem,
                                    Outcome, Replacement, ToolInvocation, identity, ExecutionCancelled,
                                    ApprovalDecision, ModelInitializationError)
from station.core.events import Event
from station.core.pending import resume_pending, publish_tool_result


@contextmanager
def model_stream(stream):
    """普通迭代器可用；支持 close 的流必须在提前退出时显式关闭。"""
    try:
        yield stream
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


class AgentExecution:
    """可暂停、可关闭的循环执行；创建对象时不会执行模型或工具。"""

    def __init__(self, model, history, tools, message_builder, policy, approval=None):
        # model：调用模型；history：读写内存历史；tools：查询/执行允许使用的工具。
        self.model, self.history, self.tools = model, history, tools
        # message_builder：把系统提示词和最新历史拼成模型输入；policy：次数、批准、压缩等规则。
        self.message_builder, self.policy = message_builder, policy
        self.approval = approval  # 仅表达批准决定；原工具/原参数从历史 pending 取。
        self.outcome: Outcome | None = None  # None 表示还没有确定结束原因。
        self._iterator = None
        self._closed = False
        self._closing = False  # 关闭期间的异常应向外传播，不能再 yield 一条错误事件。

    def events(self):
        """取得本次执行唯一的生成器；外层 next 一次，执行到下一次 yield 暂停。"""
        if self._closed and self._iterator is None:
            return iter(())
        if self._iterator is None:
            self._iterator = self._drive()
        return self._iterator

    def _drive(self):
        # 统一补齐终态；close() 引发的 GeneratorExit 也会运行 finally。
        try:
            # 校验也保持惰性；非法内部参数走统一终态/收尾，不执行任何工具或模型。
            if self.approval is not None and self.approval not in ("allow", "deny"):
                self.outcome = Outcome("failed", "invalid_approval")
                yield Event("error", {"text": "无效的批准决定"})
                return
            # ① 先接续原操作；结果保存和卡片放行完成后，才允许初始化主模型。
            pending_outcome = yield from resume_pending(self.history, self.tools, self.policy, self.approval)
            if pending_outcome is not None:
                self.outcome = pending_outcome
                if pending_outcome.reason == "tool_policy_violation":
                    # 先固定失败终态再展示错误；恰好在错误处断连也不能被改成 interrupted。
                    yield Event("error", {"text": self.policy.tools_disabled_text})
                return
            # ② 旧模型只需 stream/respond；生产适配器额外提供一次性的 initialize。
            try:
                initialize = getattr(self.model, "initialize", None)
                if initialize is not None:
                    for event in initialize():
                        yield event
            except ModelInitializationError as exc:
                self.outcome = Outcome("failed", "model_configuration")
                yield Event("error", {"text": str(exc)})
                return
            # ③ 初始化成功后，进入原有模型—工具循环（压缩仍在每步请求之前）。
            yield from self._loop()
        except ExecutionCancelled:
            self.outcome = Outcome("cancelled")
        except Exception:
            if self.outcome is None:
                self.outcome = Outcome("failed", "execution_error")
            raise
        finally:
            if self.outcome is None:
                self.outcome = Outcome("interrupted")
            self._closed = True

    def close(self):
        """断连时终止未完成循环；若已有 completed/waiting_approval 等终态则保留。"""
        if self._closed:
            return
        self._closing = True
        if self._iterator is not None:
            self._iterator.close()
        if self.outcome is None:
            self.outcome = Outcome("interrupted")
        self._closed = True

    def _compact(self):
        """超出压缩阈值时，用旧消息摘要替换前缀，保留最近消息。"""
        old = self.history.snapshot()
        messages = [item.message for item in old]
        if not compact.should_compact(messages, self.policy.compact_threshold):
            return False
        start = max(0, len(old) - self.policy.keep_messages)
        # 如果截断点位于工具结果组内，向前保留完整的 assistant/tool 协议组。
        while start and old[start].message.get("role") == "tool":
            start -= 1
        if not start:
            return False
        # 摘要调用 model.respond（非流式），正常回答调用 model.stream（流式）。
        summary = compact._summarize(messages[:start], self.model)
        retained = old[start:]
        # positions 把旧消息位置映射到新位置，供历史适配器移动界面卡片；摘要占第 0 位。
        report = Replacement(tuple(i.item_id for i in old[:start]),
                             {i: 0 if i < start else i - start + 1 for i in range(len(old))},
                             kept_item_ids=tuple(i.item_id for i in retained))
        self.history.replace([HistoryItem(identity(), {"role": "system", "content": f"（此前对话已压缩）{summary}"}), *retained], report)
        return True

    def _loop(self):
        # 给模型看的工具描述就是执行白名单；即使模型编造工具名，也不能绕过它。
        available = self.tools.specs() if self.policy.allow_tools else []
        by_name = {tool.name: tool for tool in available}
        used: dict[str, int] = {}  # 本次执行中各工具请求次数；不是跨会话累计次数。
        for _ in range(self.policy.max_steps):
            # ① 根据当前历史压缩，再组装本步消息；工具结果会在下一步自然进入输入。
            if self._compact():
                # 内存历史已经替换，先交给外层保存，再用新历史调用模型。
                yield CheckpointRequest("history_replaced", self.history.revision)
            try:
                # 每步重新构造输入，因此上一步工具结果会自然成为本步模型的上下文。
                messages = self.message_builder.build(self.history.snapshot())
                response, sent = None, ""
                with model_stream(self.model.stream(messages, available or None)) as stream:
                    for part in stream:
                        if "delta" in part:
                            # delta 是增量文字，先给用户看；最终完整结果随后用于写历史。
                            sent += part["delta"]
                            yield Event("delta", {"text": part["delta"]})
                        elif "retry" in part:
                            yield Event("retry", part["retry"])
                        else:
                            # final 同时含完整正文与工具调用；不等同于前端 done。
                            response = part["final"]
                if response is None:
                    raise RuntimeError("模型没有返回最终结果")
                text = response.get("content") or ""
                # 有些模型只返回完整正文；若已流出前缀，就只补发尚未显示的部分。
                rest = text[len(sent):] if text.startswith(sent) else text
                if rest:
                    yield Event("delta", {"text": rest})
            except Exception as exc:
                if self._closing:
                    raise
                self.outcome = Outcome("failed", "model_error")
                yield Event("error", {"text": f"模型调用失败: {type(exc).__name__}: {exc}"})
                return
            calls = response.get("tool_calls") or []
            # ② 模型直接回答就结束；禁止工具是通用执行规则，不识别恢复业务。
            if calls and not self.policy.allow_tools:
                self.outcome = Outcome("failed", "tool_policy_violation")
                yield Event("error", {"text": self.policy.tools_disabled_text})
                return
            if not calls:
                # 不再请求工具即形成最终回答。这里只记终态，外层保存后才发送 done。
                self.history.append({"role": "assistant", "content": text})
                self.outcome = Outcome("completed", done={})
                return
            calls = [c for c in calls if (c.get("name") or "").strip()]
            if not calls:
                note = "（模型返回了空的工具调用，已停止本轮。）"
                self.history.append({"role": "assistant", "content": (text + note).strip()})
                self.outcome = Outcome("invalid_tool_call", done={"text": "无效工具调用"})
                yield Event("delta", {"text": note})
                return
            # approve 表示这项操作需要用户确认；没有批准就不能产生真实工具副作用。
            need = next((c for c in calls if c["name"] in by_name and
                         by_name[c["name"]].risk == "approve" and not self.policy.auto_approve), None)
            if need:
                # ③ 先固定挂起原因，再请求保存 pending；批准卡片随后才可对外可见。
                if text.strip():
                    self.history.append({"role": "assistant", "content": text})
                args = need.get("arguments") or {}
                preview = self.tools.preview(need["name"], args)
                approval_id = self.history.set_pending(need["name"], args, preview)
                self.outcome = Outcome("waiting_approval", done={"text": "等待确认"})
                yield CheckpointRequest("approval_pending", self.history.revision)
                yield Event("approval_request", {"approval_id": approval_id,
                                                  "tool": need["name"], "label": by_name[need["name"]].label,
                                                  "preview": preview, "arguments": args})
                return
            # 先记模型要求调用哪些工具；后面逐个写 tool 结果，组成配对的模型消息。
            self.history.append({"role": "assistant", "content": text, "tool_calls": calls})
            yield Event("tool", {"names": [c["name"] for c in calls],
                                 "labels": [by_name[c["name"]].label if c["name"] in by_name else "" for c in calls],
                                 "args": [c.get("arguments") or {} for c in calls]})
            for call in calls:
                # ④ 为结果预留身份，卡片绑定这个身份；先保存配对结果再放行卡片。
                name, result_id = call["name"], identity()
                limit = self.policy.tool_limits.get(name)
                presentations = ()
                if name not in by_name:
                    output = f"工具 {name} 不在本技能白名单（可用: {', '.join(by_name) or '无'}）"
                elif limit and used.get(name, 0) >= limit.max_calls:
                    output = limit.exhausted_text
                else:
                    # 工具返回两部分：output 给模型，presentations 给界面；内核不解析卡片业务。
                    result = self.tools.execute(ToolInvocation(call.get("id", ""), name,
                        call.get("arguments") or {}, HistoryAnchor(result_id)))
                    output, presentations = result.model_text, result.presentation_events
                used[name] = used.get(name, 0) + 1
                yield from publish_tool_result(self.history, call.get("id"), name, output,
                                               presentations, item_id=result_id)
        # 防止模型持续调工具而无限循环；达到上限属于单独终态，不等于正常完成。
        self.outcome = Outcome("step_limit", done={"text": "（已达到最大工具轮次，请改小任务重试）"})


def run_agent(*, model, history, tools, message_builder, policy: ExecutionPolicy,
              approval: ApprovalDecision | None = None) -> AgentExecution:
    """创建惰性执行；开始迭代 events 才调用模型，close 幂等。"""
    return AgentExecution(model, history, tools, message_builder, policy, approval)
