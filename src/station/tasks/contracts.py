"""任务阶段的返回契约；业务只描述下一步，不接触 Celery 或消息确认。"""
from dataclasses import dataclass
from station.persistence.errors import ExecutionLost


@dataclass(frozen=True)
class StepResult:
    """没有 next_phase 表示业务完成；否则保存下一步并等待定时投递。"""
    next_phase: str = ""
    delay_seconds: float = 0
    message: str = ""


class NeedsReview(Exception):
    """外部操作是否成功不明确，停止自动重试并提示核验。"""


class RetryStep(Exception):
    """当前步骤可安全重试，例如只读查询临时失败；不用于未知的创建请求。"""


class ExecutionBusy(Exception):
    """另一个 Worker 正在处理同一业务任务，稍后重新消费。"""
