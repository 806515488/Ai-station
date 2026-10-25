"""任务数据与展示快照；消息状态和工作状态分别保存，不管理线程或连接。"""
from dataclasses import asdict, dataclass, field
import time
import uuid


@dataclass
class Job:
    skill_id: str
    args: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str = ""
    work_status: str = "pending"      # 整项业务是否完成；等待厂家时也属于 running。
    message_status: str = "pending"   # 当前步骤消息是否已确认送达，独立于业务状态。
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "run"                # 当前业务步骤；下一步会生成新的 message_id。
    queue: str = ""
    meta: dict = field(default_factory=dict)
    execution_id: str = ""            # 本次 Worker 执行版本，用于拒绝旧现场写回。
    publish_attempts: int = 0
    next_publish_at: float = 0
    publish_error: str = ""
    step_attempts: int = 0
    cancel_requested: int = 0
    progress: int = 0
    message: str = ""
    created: float = field(default_factory=time.time)
    updated: float | None = None
    log: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    result: dict | None = None

    def __post_init__(self):
        if self.updated is None:
            self.updated = self.created

    @property
    def status(self):
        """旧进度卡的 status 只做映射，不是数据库中的第三份状态。"""
        if self.cancel_requested and self.work_status in ("pending", "running"):
            return "cancelling"
        return {"pending": "queued", "success": "done", "needs_review": "unknown"}.get(self.work_status, self.work_status)

    @status.setter
    def status(self, value):
        self.work_status = {"queued": "pending", "done": "success", "unknown": "needs_review", "interrupted": "needs_review"}.get(value, value)

    def snapshot(self, with_log: bool = True) -> dict:
        return {"id": self.id, "skill_id": self.skill_id, "status": self.status,
                "message_status": self.message_status, "work_status": self.work_status,
                "phase": self.phase, "publish_attempts": self.publish_attempts, "publish_error": self.publish_error,
                "progress": self.progress, "message": self.message, "created": self.created,
                "updated": self.updated, "log": list(self.log) if with_log else [],
                "artifacts": list(self.artifacts), "result": self.result}

    def to_record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict) -> "Job":
        return cls(**{key: record[key] for key in cls.__dataclass_fields__ if key in record})
