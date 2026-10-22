"""本次请求的追踪生命周期：观察结构化事件，统一结束 request span/trace。

不解析 SSE 文本，也不修改附件业务状态。流断开时保持 interrupted；收到 done、
error、attachment_wait、approval_request 时，按现有事件契约记录最终状态。
"""
import time

from station import observability as obs
from station.core.events import Event


class RequestTrace:
    def __init__(self, started: float):
        self.started = started
        self.trace = None
        self.request_scope = None
        self.request_step = None
        self.state = "interrupted"

    def begin(self, turn) -> Event | None:
        command, attachment, thread = turn.command, turn.attachment, turn.thread
        key = ("attachment:" + attachment["client_key"] if attachment else
               "attachment:" + command.client_turn_id if command.attachment_ids and command.client_turn_id else "")
        self.trace = obs.begin(
            turn.user_id, thread.id, key=key, origin=(thread.pending or {}).get("trace_id", ""),
            attributes={"message_hash": obs.fingerprint(turn.message), "message_chars": len(turn.message),
                        "attachment_ids": attachment["attachment_ids"] if attachment else list(command.attachment_ids)},
        )
        if not self.trace:
            return
        elapsed = time.perf_counter() - self.started
        self.trace.started = time.monotonic() - elapsed
        self.trace.record["created"] = min(self.trace.record["created"], time.time() - elapsed)
        self.trace.save()
        obs.bind(self.trace)
        self.request_scope = obs.span("request", "恢复回答" if attachment else "接收消息",
                                      resume=bool(attachment), retry=command.retry_agent)
        self.request_step = self.request_scope.__enter__()
        if attachment:
            self.annotate(attachment_turn_id=attachment["id"], run_refs=attachment.get("run_refs", {}))
            for message in thread.msgs:
                if message.get("attachment_turn_id") == attachment["id"]:
                    message["trace_id"] = self.trace.record["id"]
        return Event("trace", {"trace_id": self.trace.record["id"], "thread_id": thread.id})

    def annotate(self, **attributes) -> None:
        if self.trace:
            self.trace.record["attributes"].update(attributes)
            self.trace.save()

    def skip(self) -> None:
        self.state = "skipped"
        if self.request_step:
            self.request_step.record["state"] = "skipped"

    def observe(self, event) -> None:
        states = {"error": "error", "attachment_wait": "waiting_parse", "approval_request": "waiting_approval"}
        if event.type in states:
            self.state = states[event.type]
        elif event.type == "delta":
            obs.first_text()
        elif event.type == "done" and self.state == "interrupted":
            self.state = "success"

    def close(self) -> None:
        if self.request_scope:
            if self.state in ("error", "interrupted"):
                obs.fail("RequestInterrupted" if self.state == "interrupted" else "RequestError")
                if self.state == "interrupted":
                    self.request_step.record["state"] = "interrupted"
            self.request_scope.__exit__(None, None, None)
        obs.finish(self.trace, self.state)
