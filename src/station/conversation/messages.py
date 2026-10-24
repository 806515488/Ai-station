"""普通消息：加载本人会话、追加本次输入、准备回答历史。"""
from station.chat.history import HistoryAdapter
from station.chat.models import ReplyInput
from station.conversation.models import ConversationError, Preparation, TraceInfo
from station.core.session import Thread
from station.core.events import Event
from station.core.guards import classify_answer
from station.persistence.repositories import threads
from station.persistence.errors import ThreadConflict


def load_thread(thread_id, owner):
    """加载本人会话；找不到时创建内存对象，此处还不保存。"""
    thread = Thread.load(thread_id) if thread_id else None
    if thread and thread.user_id != owner:
        raise ConversationError("会话不存在（不属于当前用户）")
    if thread and thread.pending and not thread.pending.get("id"):
        thread = Thread.from_record(threads.ensure_pending_id(thread.id, owner))
    if thread is None:
        thread = Thread("station")
        thread.user_id = owner
    # Thread 表示宿主会话；本轮选哪个业务技能另存 active_skill，由回答路由决定。
    thread.skill_id = "station"
    return thread


def prepare_reply(thread, message):
    """把当前会话历史包装成回答输入；这里只处理聊天消息。"""
    return ReplyInput(thread, HistoryAdapter(thread), message, thread.user_id,
                      thread.meta.get("active_skill", ""))


class MessageWorkflow:
    """一次普通聊天的会话操作；内存修改与数据库保存分开。"""
    def __init__(self, command, owner):
        self.command, self.owner = command, owner
        self.thread = load_thread(command.thread_id, owner)
        self.message = command.message
        self.ready = False  # 被拒绝/重复的确认请求不得在 finally 保存旧会话快照。

    def trace_info(self):
        return TraceInfo(self.owner, self.thread, self.message)

    def prepare(self):
        pending = self.thread.pending
        decision = classify_answer(self.message) if pending else ""
        if self.command.approval_id and (not pending or self.command.approval_id != pending.get("id")):
            raise ConversationError("这条确认已失效，请刷新会话后确认当前操作")
        if pending and pending.get("state", "waiting") != "waiting":
            raise ConversationError("该操作已被领取处理，请勿重复确认；未收到结果时请先核对执行详情")
        if pending and decision:
            if not self.command.approval_id:
                raise ConversationError("确认请求缺少操作编号，请刷新会话后重新确认")
            # SQL 条件更新决定唯一执行者；失败请求不追加消息，不装配或运行工具。
            record = threads.claim_approval(self.thread.id, self.owner, self.command.approval_id, decision)
            if record is None:
                raise ConversationError("该操作已被其他请求处理或发生变化，请刷新会话")
            self.thread = Thread.from_record(record)
        # add_user 只改内存，并由 Thread 添加当前 trace_id；真正保存等 prepared 检查点。
        self.thread.add_user(self.message)
        self.ready = True
        return Preparation(prepare_reply(self.thread, self.message), [Event("meta", {"thread_id": self.thread.id})])

    def persist(self):
        """由会话协调者在检查点调用，保存完整的当前会话。"""
        if not self.ready:
            return
        try:
            self.thread.save()
        except ThreadConflict as exc:
            raise ConversationError("会话状态已更新，已停止本次执行，请刷新查看结果") from exc

    def finish(self, outcome):
        # 正常回答结束或连接中断时，将当前已产生的历史保存下来。
        self.persist()
