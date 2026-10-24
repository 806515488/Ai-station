"""内核契约：模型、历史、工具和策略由外层提供，不导入业务或基础设施。

这里定义“双方约定传什么”，具体实现位于 chat 的适配器中。
dataclass 自动生成初始化方法；frozen=True 禁止重新给字段赋值，但不深度冻结 dict。
Protocol 描述一个对象必须提供哪些方法，不要求实现类继承它，也不在这里执行业务。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol, runtime_checkable
import uuid
from copy import deepcopy
from station.core.events import Event


def identity() -> str:
    return uuid.uuid4().hex


class ExecutionCancelled(BaseException):
    """通用协作式取消，穿过普通工具错误处理，仍执行所有 finally。"""


# 用户文字在 chat 解释；内核只接收明确决定，不接收 HTTP 或新的工具参数。
ApprovalDecision = Literal["allow", "deny"]
APPROVAL_REMINDER = "上一操作仍在等待确认，请回复：允许 / 拒绝。\n"


class ModelInitializationError(Exception):
    """外层适配器提供的模型初始化失败；str(exc) 是可展示的原错误文案。"""


@dataclass(frozen=True)
class HistoryItem:
    """模型可见消息及本次执行内的通用身份。"""
    item_id: str  # 本次执行内的消息身份，卡片/压缩用它定位；不是消息数组下标。
    message: dict  # 模型协议消息，如 {"role": "user", "content": "你好"}。


@dataclass(frozen=True)
class Replacement:
    """压缩替换报告：告诉历史适配器删了谁、保留了谁，以及卡片位置如何迁移。"""
    removed_item_ids: tuple[str, ...]
    positions: dict[int, int]
    reason: str = "compact"
    kept_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryAnchor:
    """展示内容属于哪条历史消息；持久化适配器再把身份换算成界面位置。"""
    item_id: str


@dataclass(frozen=True)
class Presentation:
    """给界面展示的事件及其历史归属，与给模型的工具文字分开。"""
    event: Event
    anchor: HistoryAnchor


@dataclass(frozen=True)
class CheckpointRequest:
    """内部保存屏障，成功消费之后执行器才可以继续 next；不直接发给前端。"""
    kind: str  # prepared / tool_result / approval_pending / history_replaced。
    history_revision: int  # 要求保存的内存历史版本，外层必须核对当前版本。
    presentations: tuple[Presentation, ...] = ()
    checkpoint_id: str = field(default_factory=identity)  # 每个实例生成新 ID，用于检查点去重。


@dataclass(frozen=True)
class Outcome:
    """不可变的结束原因；done 只是保留旧 HTTP 协议的载荷，不是成功判断。"""
    kind: Literal["completed", "waiting_approval", "failed", "interrupted", "cancelled", "step_limit", "invalid_tool_call"]
    reason: str = ""  # 更细的结束原因，如 model_error、tool_policy_violation。
    done: dict | None = None  # None：不发 done；{}：发空载荷 done。判断时不能写 if done。


@dataclass(frozen=True)
class CallLimit:
    """一个工具在本次 Loop 执行内允许请求的次数，以及超限后反馈给模型的文字。"""
    max_calls: int
    exhausted_text: str


@dataclass(frozen=True)
class ExecutionPolicy:
    """外层制定规则，Loop 统一执行；不在内核硬编码某个技能或网页检索特例。"""
    max_steps: int = 10  # 最多进行几步模型决策，防止工具循环失控。
    allow_tools: bool = True  # False 时不给工具描述，且拒绝模型返回的工具调用。
    auto_approve: bool = False  # 是否跳过 approve 风险工具的人工批准。
    compact_threshold: int = 0  # 历史压缩阈值，按 compact 的估算口径；0 表示关闭。
    keep_messages: int = 8  # 压缩保留的最近消息条数；必要时多保留以维持工具协议组完整。
    tool_limits: dict[str, CallLimit] = field(default_factory=dict)  # 按完整工具名配置上限。
    tools_disabled_text: str = "本次执行不允许调用工具。"  # 禁用工具后，违规时给用户的提示。


@dataclass(frozen=True)
class ToolSpec:
    """工具的描述快照：模型看 schema，Loop 用 name/risk 等元数据做通用判断。"""
    name: str
    label: str
    risk: str
    owner: str
    schema_data: dict

    def schema(self) -> dict:
        return deepcopy(self.schema_data)


@dataclass(frozen=True)
class ToolInvocation:
    call_id: str  # 本次调用编号，与模型消息中的 tool_call_id 配对。
    name: str  # 带命名空间的完整工具名，也是白名单的查找键。
    arguments: dict  # 已解析为字典的工具参数。
    presentation_anchor: HistoryAnchor  # 工具产生卡片时应挂到的历史消息。


@dataclass(frozen=True)
class ToolErrorInfo:
    """工具适配器捕获的错误类型；与可反馈给模型的错误文字分开保留。"""
    error_type: str


@dataclass(frozen=True)
class ToolResult:
    model_text: str  # 下一步模型读取的工具结果，写入 role=tool 消息。
    presentation_events: tuple[Presentation, ...] = ()  # 前端卡片，不混入模型正文。
    error_info: ToolErrorInfo | None = None


@runtime_checkable
class Model(Protocol):
    """stream 用于逐段回答，respond 用于压缩摘要；模型 SDK/HTTP 细节由适配器承担。"""
    def stream(self, messages: list[dict], tools=None) -> Iterator[dict]: ...
    def respond(self, messages: list[dict], tools=None) -> dict: ...


class Initializable(Protocol):
    """独立可选能力，不增加 Model 的必需方法；初始化返回一次性的展示事件。"""
    def initialize(self) -> list[Event]: ...


class History(Protocol):
    """只操作内存历史；真正写数据库由 conversation 消费检查点后负责。"""
    revision: int
    def snapshot(self) -> list[HistoryItem]: ...
    def append(self, message: dict, *, item_id: str = "") -> HistoryItem: ...
    def replace(self, items: list[HistoryItem], report: Replacement) -> None: ...
    def get_pending(self) -> dict | None: ...
    def set_pending(self, tool: str, arguments: dict, preview: str) -> str: ...
    def clear_pending(self) -> None: ...


class Tools(Protocol):
    """工具白名单、批准预览和执行入口；来源可以是全局工具、技能或 MCP。"""
    def specs(self) -> list[ToolSpec]: ...
    def spec(self, name: str) -> ToolSpec | None: ...
    def preview(self, name: str, arguments: dict) -> str: ...
    def execute(self, invocation: ToolInvocation) -> ToolResult: ...


class MessageBuilder(Protocol):
    """用最新历史构造模型输入；每一步都重建，避免漏掉刚产生的工具结果。"""
    def build(self, history: list[HistoryItem]) -> list[dict]: ...
