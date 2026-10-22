"""一轮对话的主流程：按这个文件的顺序阅读，再进入对应阶段模块。

resolve 会话 → 提交附件或领取回答 → 补批准/选技能 → 准备上下文 → 执行 agent。
输入是 ChatCommand，输出是 Event；本层不接触 FastAPI Request、Cookie 或 SSE 字符串。
"""
from __future__ import annotations

from contextlib import closing
import time

from station import observability as obs
from station.core.agent import run_agent
from station.core.events import Event
from station.core.router import log_route
from station.chat import runtime, routing
from station.chat.approvals import resolve_pending
from station.chat.catalog import build_generic_agent, build_skill_catalog
from station.chat.history import save_event
from station.chat.models import ChatCommand, ChatError
from station.chat.tracing import RequestTrace
from station.chat.turns import ChatTurn


class ChatService:
    def __init__(self, command: ChatCommand, user_id: str, *, started: float | None = None):
        self.command = command
        self.user_id = user_id
        self.started = time.perf_counter() if started is None else started
        self.turn: ChatTurn | None = None
        self.trace = RequestTrace(self.started)

    def events(self):
        """统一异常与收尾；显式关闭内部生成器，让断连先释放正在执行的 span。"""
        execution = self._execute()
        try:
            for event in execution:
                self.trace.observe(event)
                yield event
        except ChatError as exc:
            event = Event("error", {"text": str(exc)})
            self.trace.observe(event)
            yield event
        except Exception as exc:
            obs.fail(type(exc).__name__)
            if self.turn:
                self.turn.finish("failed", "对话执行失败，已完成解析产物保留")
            log_route({"kind": "chat_error", "error_type": type(exc).__name__})
            event = Event("error", {"text": "请求处理失败，请重新载入会话查看附件状态"})
            self.trace.observe(event)
            yield event
        finally:
            try:
                execution.close()
            finally:
                try:
                    self.trace.close()
                finally:
                    if self.turn:
                        self.turn.finish("interrupted", "对话连接中断，请明确重试")

    def _execute(self):
        self.turn = turn = ChatTurn.resolve(self.command, self.user_id)
        catalog, generic = build_skill_catalog(), build_generic_agent()
        trace_event = self.trace.begin(turn)
        if trace_event:
            yield trace_event

        # 上传先排后台解析。本次不生成回答；前端拿原轮次 ID 恢复，不重新上传正文。
        if turn.is_upload:
            turn_id, events = turn.submit_attachments()
            self.trace.annotate(attachment_turn_id=turn_id)
            yield from events
            return
        accepted = turn.prepare_messages()
        if not accepted:
            self.trace.skip()
        yield Event("meta", {"thread_id": turn.thread.id})
        if not accepted:
            yield Event("done")
            return

        # 处理批准不再混入路由分支；已有待批准工具决定本轮使用的技能。
        approval = resolve_pending(turn.thread, turn.message, generic)
        yield from approval.events
        if approval.stop:
            return
        selection = (routing.Selection(approval.agent, level="pending") if approval.agent else
                     routing.select_agent(turn, catalog, generic))
        agent = selection.agent
        self.trace.annotate(agent=agent.id)
        context = approval.context or runtime.create_context(turn.thread, agent, reply_only=turn.reply_only)
        available = {entry.id for entry in catalog}
        if agent.id in available:
            turn.thread.meta["active_skill"] = agent.id
        else:
            turn.thread.meta.pop("active_skill", None)
        log_route({"text": turn.message[:200], "agent": agent.id, "level": selection.level,
                   "skill": selection.skill_id, "route_ms": selection.duration_ms,
                   "l2": selection.diagnostics or None})
        yield from self._run_agent(agent, context, selection.duration_ms)

    def _run_agent(self, agent, context, route_ms: float):
        """执行并保存结构化事件；仅在这一层记录路由结束到首字/完成的耗时。"""
        turn = self.turn
        try:
            model = runtime.make_model(agent, self.user_id)
        except Exception as exc:
            log_route({"kind": "chat", "text": turn.message[:60], "agent": agent.id,
                       "route_ms": route_ms, "first_delta_ms": None, "total_ms": self.elapsed_ms(),
                       "error": f"{type(exc).__name__}: {exc}"[:200]})
            turn.finish("failed", "模型不可用，附件解析结果已保留")
            yield Event("error", {"text": f"模型不可用：{exc}"})
            return
        turn.thread.save()
        yield Event("stage", {"text": "正在思考…"})
        first_delta_ms = None
        with closing(run_agent(context, turn.thread, agent, model)) as events:
            for event in events:
                if event.type == "error":
                    turn.finish("failed", "对话执行失败，附件解析结果已保留")
                if event.type == "delta" and first_delta_ms is None:
                    first_delta_ms = self.elapsed_ms()
                save_event(turn.thread, event)
                # 完成事件发出前先保存终态；客户端收到 done 就断开也不会留下“回答中断”。
                if event.type == "done":
                    turn.finish("done")
                yield event
        log_route({"kind": "chat", "text": turn.message[:60], "agent": agent.id,
                   "route_ms": route_ms, "first_delta_ms": first_delta_ms, "total_ms": self.elapsed_ms()})

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)
