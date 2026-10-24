"""对话路由：POST /api/chat 校验输入、认证、调用对话服务、返回 SSE。

阅读主流程先进入 station.conversation.service，再进入 station.chat.service；本文件保留 HTTP DTO 和传输适配，
不会选择技能或执行工具。
"""
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from station.app.auth import require_user
from station.app.streaming import event_response
from station.conversation.models import SendMessage
from station.conversation.service import ConversationService
from station.core.events import Event

router = APIRouter(tags=["对话"])


class ChatIn(BaseModel):
    """前端只提交正文与会话 ID；旧上传/恢复字段会被拒绝，避免静默丢失输入。"""
    model_config = ConfigDict(extra="forbid")

    # 用户本次输入的正文。
    message: str

    # 会话 ID：传入则继续本人会话，留空则创建新会话。
    thread_id: str | None = None
    approval_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{32})?$")

    def to_command(self):
        return SendMessage(self.message, self.thread_id, self.approval_id)


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    # 内层是同步生成器；event_response 会在工作线程逐次取事件，避免阻塞异步 HTTP。
    def events():
        started = time.perf_counter()
        try:
            user = require_user(request)
        except HTTPException as exc:
            # 延续原协议：聊天未登录返回 SSE error，其他路由仍正常返回 HTTP 401。
            yield Event("error", {"text": exc.detail})
            return
        # yield from 逐项转发会话事件；业务执行由对话服务负责。
        yield from ConversationService(body.to_command(), user["id"], started=started).events()
    return event_response(events())
