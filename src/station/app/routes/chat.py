"""对话路由：POST /api/chat 校验输入、认证、调用对话服务、返回 SSE。

阅读主流程请进入 station.chat.service；本文件保留 HTTP DTO 和传输适配，
不会加载附件正文、更新轮次状态、选择技能或执行工具。
"""
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from station.app.auth import require_user
from station.app.streaming import event_response
from station.chat.models import ChatCommand
from station.chat.service import ChatService
from station.core.events import Event

router = APIRouter(tags=["对话"])


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None
    skill: str | None = None  # 保留旧客户端字段；实际技能由服务端选择。
    attachment_ids: list[str] = Field(default_factory=list)
    client_turn_id: str = ""
    resume_turn_id: str = ""
    retry_agent: bool = False
    reply_only: bool = False

    def to_command(self) -> ChatCommand:
        return ChatCommand(
            message=self.message, thread_id=self.thread_id, attachment_ids=tuple(self.attachment_ids),
            client_turn_id=self.client_turn_id, resume_turn_id=self.resume_turn_id,
            retry_agent=self.retry_agent, reply_only=self.reply_only,
        )


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    def events():
        started = time.perf_counter()
        try:
            user = require_user(request)
        except HTTPException as exc:
            # 延续原协议：聊天未登录返回 SSE error，其他路由仍正常返回 HTTP 401。
            yield Event("error", {"text": exc.detail})
            return
        yield from ChatService(body.to_command(), user["id"], started=started).events()
    return event_response(events())
