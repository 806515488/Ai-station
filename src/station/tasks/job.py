"""任务数据：字段、对外快照与普通记录转换，不访问数据库或管理线程。

Java 视角相当于任务实体；运行期的锁/执行 token 在 runtime.Execution。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid


@dataclass
class Job:
    skill_id: str
    args: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str = ""
    status: str = "queued"
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

    def snapshot(self, with_log: bool = True) -> dict:
        """保持原轮询字段；运行中的任务由 Execution 在锁内调用。"""
        return {"id": self.id, "skill_id": self.skill_id, "status": self.status,
                "progress": self.progress, "message": self.message, "created": self.created,
                "updated": self.updated, "log": list(self.log) if with_log else [],
                "artifacts": list(self.artifacts), "result": self.result}

    def to_record(self) -> dict:
        return {**self.snapshot(), "args": self.args, "user_id": self.user_id}

    @classmethod
    def from_record(cls, record: dict) -> "Job":
        return cls(**{key: record[key] for key in cls.__dataclass_fields__})
