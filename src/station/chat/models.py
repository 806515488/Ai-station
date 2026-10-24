"""回答输入和路由准备结果；会话保存由 conversation 负责。"""
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class ReplyInput:
    """普通聊天的回答输入：会话、最新历史、本轮正文和当前技能提示。"""
    thread: Any  # 会话对象，用于外层装配和保存；不把它直接传给 Loop。
    history: Any  # HistoryAdapter，给 Loop 暴露通用历史操作。
    message: str  # 本轮正文，用于批准判断和意图识别。
    user_id: str  # 当前用户，用于选择本人模型和工具配置。
    active_skill: str = ""  # 上次使用的技能提示，供路由参考，不强制选中。


@dataclass(frozen=True)
class ActiveSkillUpdate:
    """chat 提议如何更新会话技能：set 指定、clear 清除、keep 保留；保存由会话层做。"""
    action: Literal["set", "clear", "keep"]
    skill_id: str = ""


@dataclass(frozen=True)
class PreparedAnswer:
    """路由和装配完成后的固定结果，与 prepared 检查点一一对应。"""
    execution_id: str  # 哪一次 AnswerExecution。
    checkpoint_id: str  # 哪一个 prepared 检查点，防止保存时串用旧结果。
    history_revision: int  # 准备时对应的历史版本。
    selected_skill_id: str  # 本次实际选择的助手/技能。
    active_skill_update: ActiveSkillUpdate  # 需要会话层落库的技能更新意图。
