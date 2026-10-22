"""对话输入契约：HTTP 校验完成后，交给应用层一份不可修改的命令。

ChatCommand 保留用户提交的请求；恢复时查到的原话、会话和执行权放在 turns.ChatTurn，
不再反过来修改请求体。这样能分清“用户传入了什么”和“本轮实际执行什么”。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatCommand:
    message: str
    thread_id: str | None = None
    attachment_ids: tuple[str, ...] = ()
    client_turn_id: str = ""
    resume_turn_id: str = ""
    retry_agent: bool = False
    reply_only: bool = False


class ChatError(Exception):
    """预期的业务拒绝；转换成 SSE error，保留现有客户端协议。"""
