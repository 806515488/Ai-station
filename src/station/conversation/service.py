"""请求编排与唯一终结出口：准备输入→驱动回答→可靠收尾→放行结束事件。

具体操作在对应 Workflow 中；这里只处理 Preparation、Checkpoint 和 Outcome。
HTTP 不解释状态，chat 不保存业务记录，Loop 只负责执行控制。

debug 阅读顺序：events → workflow.prepare → AnswerExecution.events → _checkpoint → workflow.finish。
这里有两种向上返回的东西：Event 给界面看，CheckpointRequest 只要求本层先保存。
"""
from __future__ import annotations
import time
from station import observability as obs
from station.chat.service import AnswerExecution
from station.chat.tracing import RequestTrace
from station.core.contracts import CheckpointRequest, Outcome
from station.core.events import Event
from station.core.router import log_route
from station.conversation.messages import MessageWorkflow
from station.conversation.models import ConversationError


class ConversationService:
    """一次 HTTP 聊天请求的协调者；负责保存和收尾，不负责模型/工具循环。"""

    def __init__(self, command, user_id, *, started=None):
        self.command, self.user_id = command, user_id
        self.started = time.perf_counter() if started is None else started
        self.trace = RequestTrace(self.started)
        # workflow 管本人会话与历史，answer 管一轮回答。
        self.workflow = self.answer = None
        # committed 表示最终业务收尾已保存；commit_failed 表示保存失败，不应在 finally 盲目重写。
        self.committed = self.commit_failed = False
        # 同一回答执行的同一检查点只保存一次；不是“整个请求只保存一次”。
        self.handled = set()
        self.first_delta_ms = None

    def _emit(self, event):
        """转发前记录首段文本耗时和追踪信息；这里还没有编码成 SSE。"""
        if event.type == "delta" and self.first_delta_ms is None:
            self.first_delta_ms = round((time.perf_counter() - self.started) * 1000, 1)
        self.trace.observe(event)
        return event

    def _checkpoint(self, signal, reply):
        """执行器停在 yield 检查点处；本方法保存成功后，外层才会取下一项。"""
        key = (self.answer.execution_id, signal.checkpoint_id)
        if key in self.handled:
            return
        # revision 是本次执行中历史的内存版本号，防止拿旧检查点保存新历史。
        if signal.history_revision != reply.history.revision:
            raise ConversationError("会话检查点版本不一致")
        if signal.kind == "prepared":
            prepared = self.answer.prepared
            if (not prepared or prepared.checkpoint_id != signal.checkpoint_id
                    or prepared.execution_id != self.answer.execution_id
                    or prepared.history_revision != signal.history_revision):
                raise ConversationError("回答准备结果与检查点不一致")
            # chat 只提出技能切换结果；由会话用例统一写入 Thread，再与历史一起保存。
            update = prepared.active_skill_update
            if update.action == "set":
                reply.thread.meta["active_skill"] = update.skill_id
            elif update.action == "clear":
                reply.thread.meta.pop("active_skill", None)
            self.trace.annotate(agent=prepared.selected_skill_id)
        # 先把卡片位置放进内存，再连同工具结果一起保存；成功后卡片才允许发给界面。
        reply.history.remember(signal.presentations)
        try:
            self.workflow.persist()
        except BaseException:
            self.commit_failed = True
            raise
        self.handled.add(key)

    def events(self):
        """生成器：调用本方法只得到迭代器；外层每取一次，才运行到下一个 yield。"""
        final = None
        try:
            # ① 加载本人会话，开启本次请求的执行追踪。
            self.workflow = MessageWorkflow(self.command, self.user_id)
            trace_event = self.trace.begin(self.workflow.trace_info())
            if trace_event:
                yield self._emit(trace_event)
            # ② 追加本次用户消息，准备历史；meta 事件把会话 ID 告知前端。
            prepared = self.workflow.prepare()
            for event in prepared.events:
                yield self._emit(event)
            self.answer = AnswerExecution(prepared.reply)
            for signal in self.answer.events():
                # 检查点先保存；文字/卡片等展示事件向前端转发。
                if isinstance(signal, CheckpointRequest):
                    self._checkpoint(signal, prepared.reply)
                else:
                    yield self._emit(signal)
            final = self.answer.outcome
            # ③ 唯一正常收尾出口：先保存历史/业务状态，再让前端收到 done。
            # final.kind 是真实结束原因；done 只是旧前端协议的一条结束通知。
            self.workflow.finish(final)
            self.committed = True
            self.trace.complete(final)
            if final.done is not None:
                yield self._emit(Event("done", final.done))
        except (ConversationError, ValueError) as exc:
            final = Outcome("failed", "business_rejection")
            yield self._emit(Event("error", {"text": str(exc)}))
        except Exception as exc:
            final = Outcome("failed", "execution_error")
            obs.fail(type(exc).__name__)
            log_route({"kind": "chat_error", "error_type": type(exc).__name__})
            yield self._emit(Event("error", {"text": "请求处理失败，请重新载入会话查看状态"}))
        finally:
            # 浏览器断开也会从生成器 close() 进入这里；不能只在正常 return 路径收尾。
            try:
                if self.answer:
                    try:
                        self.answer.close()
                    except Exception as exc:
                        obs.fail(type(exc).__name__)
                        final = Outcome("failed", "resource_close_error")
                # 若执行器已确定终态就沿用；还没确定就按中断处理，不能猜成成功。
                final = final or (self.answer.outcome if self.answer else None) or Outcome("interrupted")
                # 正常收尾已保存或保存曾失败时，不在这里重复保存。
                if self.workflow and not self.committed and not self.commit_failed:
                    try:
                        self.workflow.finish(final)
                    except Exception:
                        self.commit_failed = True
                if self.commit_failed:
                    final = Outcome("failed", "checkpoint_error")
                self.trace.complete(final)
            finally:
                # 即使业务保存或日志失败，也要走最内层 finally 关闭追踪资源。
                try:
                    if self.answer and self.answer.prepared:
                        log_route({"kind": "chat", "text": self.workflow.message[:60],
                            "agent": self.answer.prepared.selected_skill_id, "route_ms": self.answer.route_ms,
                            "first_delta_ms": self.first_delta_ms,
                            "total_ms": round((time.perf_counter() - self.started) * 1000, 1)})
                finally:
                    self.trace.close()
