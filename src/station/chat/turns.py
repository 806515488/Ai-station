"""一轮对话的会话与附件生命周期：归属、冻结输入、领取执行权和收尾。

所有回答阶段的附件状态更新集中在 ChatTurn.finish。只有本轮领取成功且仍持有
agent_claim_id 的请求可以结束轮次，重复恢复或断连收尾不会改写另一请求的结果。
原件解析/切块继续由 documents.service 负责，本模块只协调它与聊天的衔接。
"""
from __future__ import annotations

from station.persistence.repositories import attachments as attachment_records


from dataclasses import dataclass, field
import uuid


from station.core.events import Event
from station.core.session import Thread
from station.chat.history import remember_card
from station.chat.models import ChatCommand, ChatError


@dataclass
class ChatTurn:
    command: ChatCommand
    user_id: str
    thread: Thread
    message: str
    attachment: dict | None = None
    claim_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    claimed: bool = False

    @classmethod
    def resolve(cls, command: ChatCommand, user_id: str) -> ChatTurn:
        """恢复只使用服务器冻结的原话/会话；command 始终保留前端原始输入。"""
        thread_id, message, attachment = command.thread_id, command.message, None
        if command.attachment_ids and not thread_id and command.client_turn_id:
            previous = attachment_records.attachment_turn_find(command.client_turn_id, user_id)
            if previous:
                thread_id = previous["thread_id"]
        if command.resume_turn_id:
            attachment = attachment_records.attachment_turn_get(command.resume_turn_id, user_id)
            if (not attachment or not attachment["thread_id"] or
                    (thread_id and thread_id != attachment["thread_id"])):
                raise ChatError("附件轮次不存在")
            thread_id, message = attachment["thread_id"], attachment["message"]

        thread = Thread.load(thread_id) if thread_id else None
        if thread is not None and thread.user_id != user_id:
            raise ChatError("会话不存在（不属于当前用户）")
        if thread is None:
            thread = Thread(skill_id="station")
            thread.user_id = user_id
        else:
            thread.skill_id = "station"  # 老技能会话也可通过统一入口继续。
        return cls(command, user_id, thread, message, attachment)

    @property
    def is_upload(self) -> bool:
        return bool(self.command.attachment_ids and not self.attachment)

    @property
    def reply_only(self) -> bool:
        return bool(self.attachment and (self.command.reply_only or self.attachment.get("reply_only")))

    def submit_attachments(self) -> tuple[str, list[Event]]:
        """登记附件轮次与后台解析任务；本次只返回等待卡，不调用主模型。"""
        if self.thread.pending:
            raise ChatError("请先完成上一操作的批准，再发送附件")
        from station.documents import service
        try:
            turn, created = service.submit(
                self.thread, self.message or "请阅读附件，说明内容和处理情况",
                list(self.command.attachment_ids), self.command.client_turn_id,
            )
        except ValueError as exc:
            raise ChatError(str(exc)) from exc
        card = {"type": "attachments", "turn_id": turn["id"], "thread_id": self.thread.id}
        if created:
            remember_card(self.thread, card)
            self.thread.save()
        events = [Event("meta", {"thread_id": self.thread.id}), Event("render", {"card": card}),
                  Event("attachment_wait", {"turn_id": turn["id"], "thread_id": self.thread.id}), Event("done")]
        return turn["id"], events

    def prepare_messages(self) -> bool:
        """普通消息直接记入历史；恢复先原子领取，重复请求返回 False。"""
        if not self.attachment:
            self.thread.add_user(self.message)
            return True
        if self.thread.pending:
            raise ChatError("请先完成上一操作的批准")
        from station.documents.pipeline import BOOT_ID
        from station.observability.recorder import instance_id
        allowed = ("ready", "failed", "interrupted") if self.command.retry_agent else ("ready",)
        self.claimed = bool(attachment_records.attachment_turn_update(
            self.attachment["id"], self.user_id,
            {"state": "running_agent", "error": "", "agent_boot_id": BOOT_ID,
             "agent_claim_id": self.claim_id, "agent_instance_id": instance_id(), "reply_only": self.reply_only},
            expected=allowed,
        ))
        if not self.claimed:
            return False
        if not any(message.get("attachment_turn_id") == self.attachment["id"] for message in self.thread.msgs):
            self.thread.add({"role": "user", "content": self.message, "attachment_turn_id": self.attachment["id"]})
        return True

    def finish(self, state: str, error: str = "") -> None:
        """统一成功/失败/中断出口；已结束或不属于本次执行的轮次保持原样。"""
        if not self.claimed:
            return
        attachment_records.attachment_turn_update(self.attachment["id"], self.user_id,
                                  {"state": state, "error": error}, expected=("running_agent",),
                                  expected_claim_id=self.claim_id)
