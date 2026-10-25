"""Worker 的一次执行现场：进度、业务检查点和阶段推进；不自行开线程或续租。"""
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
import uuid

from station.core.contracts import ExecutionCancelled
from station.persistence.repositories import jobs as records
from station.tasks.contracts import ExecutionLost, StepResult
from station.tasks.job import Job

_origin = contextvars.ContextVar("task_origin", default=None)


@dataclass
class Execution:
    job: Job
    lock: object = field(default_factory=threading.RLock)

    def __post_init__(self):
        self.message_id = self.job.message_id
        self.execution_id = self.job.execution_id


class TaskCancelled(ExecutionCancelled):
    """只停止本地后续处理；外部任务可能继续。"""


def load_job(job_id):
    record = records.job_load(job_id)
    return Job.from_record(record) if record else None


def persist(job):
    """登记历史/外部导入记录；业务提交使用 service.submit。"""
    records.job_save(job.to_record())


def check(execution):
    row = records.job_load(execution.job.id)
    if not row or row["message_id"] != execution.message_id or row["execution_id"] != execution.execution_id:
        raise ExecutionLost("当前消息或执行版本已经变化")
    if row["cancel_requested"]:
        raise TaskCancelled()


def _save(execution, changes=None):
    execution.job.updated = time.time()
    records.execution_update(execution.job.to_record(), execution.message_id, execution.execution_id, changes)


def update(execution, **values):
    with execution.lock:
        for key, value in values.items():
            setattr(execution.job, key, value)
        _save(execution)


def append(execution, event):
    with execution.lock:
        job = execution.job
        if event.get("type") == "progress":
            if event.get("percent") is not None:
                job.progress = max(job.progress, min(100, max(0, int(event["percent"]))))
            job.message = str(event.get("message") or job.message)[:600]
        elif event.get("type") == "artifact":
            fid = str(event.get("id") or "")
            if fid and fid not in job.artifacts:
                job.artifacts.append(fid)
        # 保存有界事件，避免每次写回持续增长的整份日志。
        job.log = [*job.log[-199:], event]
        _save(execution)


def checkpoint(execution, **values):
    """只保存恢复参数，不把密钥、完整正文或带签名下载 URL 放进任务元数据。"""
    allowed = {"phase", "remote_id", "remote_state", "provider_ref", "model", "endpoint_hash",
               "parent_job_id", "thread_id", "trace_id", "successor_id", "successor_action",
               "label", "remote_terminal", "error_type", "submission_state", "ref_tokens",
               "deadline", "retry_count", "resource"}
    if not set(values) <= allowed or len(json.dumps(values)) > 16384:
        raise ValueError("无效或过大的业务检查点")
    with execution.lock:
        execution.job.meta.update(values)
        _save(execution)


def advance(execution, result):
    """下一步、下一条消息和待投递状态一次提交；不在这里连接 RabbitMQ。"""
    job = execution.job
    check(execution)
    if not result.next_phase:
        job.work_status = "success"
        job.message = result.message or "完成"
        job.progress = 100
        _save(execution)
        return
    job.message = result.message or job.message
    job.meta["phase"] = result.next_phase
    changes = {"phase": result.next_phase, "message_id": uuid.uuid4().hex,
               "message_status": "pending", "publish_attempts": 0, "publish_error": "",
               "next_publish_at": time.time() + max(0, result.delay_seconds),
               "execution_id": "", "step_attempts": 0}
    _save(execution, changes)


@contextmanager
def bind_context(ctx):
    thread = getattr(ctx, "thread", None)
    token = _origin.set((getattr(ctx, "user_id", ""), getattr(thread, "id", "")))
    try:
        yield
    finally:
        _origin.reset(token)


def current_origin():
    return _origin.get()


def entry_snapshot(entry):
    return {"provider_ref": entry["id"], "model": entry["model"],
            "endpoint_hash": hashlib.sha256((entry.get("base_url") or "").encode()).hexdigest()}


def resolve_remote(owner, snapshot, slot):
    from station.models import config as modelcfg
    entries = modelcfg.resolve_one(owner, snapshot.get("provider_ref", ""), slot)
    if not entries or entry_snapshot(entries[0]) != {k: snapshot.get(k) for k in ("provider_ref", "model", "endpoint_hash")}:
        raise ValueError("原任务的模型或接口已变更，请恢复原配置后继续查询；不会创建新的远端任务")
    return entries[0]
