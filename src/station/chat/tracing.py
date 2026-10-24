"""本次请求的追踪生命周期：观察结构化事件，统一结束 request span/trace。

observe 随事件记录过程，complete 最终依据 Outcome 决定追踪状态，由会话终结器调用。
"""
import time

from station import observability as obs
from station.core.events import Event


class RequestTrace:
    """一个请求的追踪资源；业务保存归 Workflow，这里只负责可观测性。"""
    def __init__(self, started: float):
        self.started = started
        self.trace = None
        self.request_scope = None
        self.request_step = None
        self.state = "interrupted"  # 默认尚未完整执行；不能因为生成器被关闭就猜成成功。

    def begin(self, info) -> Event | None:
        # info 描述当前会话；origin 关联上一轮批准请求。
        thread = info.thread
        self.trace = obs.begin(
            info.user_id, thread.id, origin=(thread.pending or {}).get("trace_id", ""),
            attributes={"message_hash": obs.fingerprint(info.message), "message_chars": len(info.message)},
        )
        if not self.trace:
            return
        elapsed = time.perf_counter() - self.started
        self.trace.started = time.monotonic() - elapsed
        self.trace.record["created"] = min(self.trace.record["created"], time.time() - elapsed)
        self.trace.save()
        obs.bind(self.trace)  # 后续模型/工具 span 和新增历史消息都能取到当前 trace。
        self.request_scope = obs.span("request", info.label)
        self.request_step = self.request_scope.__enter__()
        return Event("trace", {"trace_id": self.trace.record["id"], "thread_id": thread.id})

    def annotate(self, **attributes) -> None:
        if self.trace:
            self.trace.record["attributes"].update(attributes)
            self.trace.save()

    def observe(self, event) -> None:
        """随过程事件更新追踪；首字记录从 delta 触发，不把工具卡片算作回答首字。"""
        states = {"error": "error", "approval_request": "waiting_approval"}
        if event.type in states:
            self.state = states[event.type]
        elif event.type == "delta":
            obs.first_text()
        elif event.type == "done" and self.state == "interrupted":
            self.state = "success"

    def complete(self, outcome):
        # 追踪 state 与内核 outcome 不是同一套枚举，例如 completed 映射 success。
        if outcome.kind == "failed":
            self.state = "error"
        elif outcome.kind in {"interrupted", "cancelled"}:
            self.state = "interrupted"
        elif outcome.kind == "waiting_approval" and outcome.reason != "approval_reminder":
            self.state = "waiting_approval"
        else:
            self.state = "success"

    def close(self) -> None:
        """关闭 request span 并提交 trace 终态；断连也必须由外层 finally 调用。"""
        if self.request_scope:
            if self.state in ("error", "interrupted"):
                obs.fail("RequestInterrupted" if self.state == "interrupted" else "RequestError")
                if self.state == "interrupted":
                    self.request_step.record["state"] = "interrupted"
            self.request_scope.__exit__(None, None, None)
        obs.finish(self.trace, self.state)
