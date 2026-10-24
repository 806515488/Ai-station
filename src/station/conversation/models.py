"""普通聊天请求与准备结果；不包含文件上传或恢复状态。"""
from dataclasses import dataclass, field
from station.core.events import Event


class ConversationError(Exception):
    """可向用户说明的业务错误，例如访问了不属于自己的会话。"""
    pass


@dataclass(frozen=True)
class SendMessage:
    """普通发消息命令：正文和会话 ID。"""
    message: str  # 当前用户正文。
    thread_id: str | None = None  # 留空新建会话，否则加载并校验归属。
    approval_id: str = ""  # 确认请求必须引用看到的具体操作，不能把旧按钮套到新 pending 上。


@dataclass(frozen=True)
class TraceInfo:
    """当前用户、会话和消息的追踪描述。"""
    user_id: str
    thread: object
    message: str
    label: str = "接收消息"


@dataclass
class Preparation:
    """普通聊天准备结果：本次回答输入，以及告知前端会话 ID 的事件。"""
    reply: object  # chat.models.ReplyInput。
    events: list[Event] = field(default_factory=list)  # 每个实例拥有独立的事件列表。
