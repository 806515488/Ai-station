"""任务的执行工牌与检查点。心跳只续自己持有的租约，不接管另一台机器的活。

新手视角：数据库保存“谁在干、干到哪里”；线程消失后状态也能解释，恢复时
只使用原件 ID、模型引用和远端任务 ID，不把 API Key 或临时下载地址落盘。
"""

import contextvars
import hashlib
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from station.observability.recorder import BOOT_ID, instance_id
from station.persistence.errors import LeaseLost
from station.persistence.repositories import jobs as job_records
from station.tasks.job import Job
from station.core.contracts import ExecutionCancelled

LEASE_SECONDS = 30
_origin=contextvars.ContextVar('task_origin',default=None)


@dataclass
class Execution:
    """本次执行的现场；历史 Job 记录无需创建锁或持有执行 token。"""
    job: Job
    token: str = ""
    lock: Any = field(default_factory=threading.RLock, repr=False)


def load_job(job_id: str) -> Job | None:
    record = job_records.job_load(job_id)
    return Job.from_record(record) if record else None


def persist(job: Job, lease_token: str = "") -> None:
    """保存记录；执行中更新先续租，再由最终 SQL 的 token 围栏确认写入权。"""
    if lease_token and not job_records.task_heartbeat(job.id, job.user_id, lease_token, LEASE_SECONDS):
        raise LeaseLost()
    job_records.job_save(job.to_record(), lease_token=lease_token)


def update(execution: Execution, **values) -> None:
    with execution.lock:
        for key, value in values.items():
            setattr(execution.job, key, value)
        execution.job.updated = time.time()
        persist(execution.job, execution.token)


def append(execution: Execution, event: dict) -> None:
    """保持原日志/进度/产物派生规则；未带 percent 时保留当前进度。"""
    with execution.lock:
        job = execution.job
        job.log.append(event)
        if event.get("type") == "progress":
            job.progress = event.get("percent", job.progress)
            job.message = event.get("message", job.message)
        elif event.get("type") == "artifact":
            job.artifacts.append(event.get("id"))
        job.updated = time.time()
        persist(job, execution.token)


def snapshot(execution: Execution, with_log: bool = True) -> dict:
    with execution.lock:
        return execution.job.snapshot(with_log)


@contextmanager
def bind_context(ctx):
    thread=getattr(ctx,'thread',None)
    token=_origin.set((getattr(ctx,'user_id',''),getattr(thread,'id','')))
    try:yield
    finally:_origin.reset(token)


def current_origin():
    return _origin.get()


class TaskCancelled(ExecutionCancelled):
    """协作式取消，穿过技能的普通异常兜底，但仍执行 finally 清理。"""


def entry_snapshot(entry):
    return {'provider_ref': entry['id'], 'model': entry['model'],
            'endpoint_hash': hashlib.sha256((entry.get('base_url') or '').encode()).hexdigest()}


def resolve_remote(owner, snapshot, slot):
    from station.models import config as modelcfg
    entries = modelcfg.resolve_one(owner, snapshot.get('provider_ref',''), slot)
    if not entries or entry_snapshot(entries[0]) != {k:snapshot.get(k) for k in ('provider_ref','model','endpoint_hash')}:
        raise ValueError('原任务的模型或接口已变更，请恢复原配置后继续查询；不会创建新的远端任务')
    return entries[0]


def state(job, control):
    if job.status not in ('queued','running'):
        return job.status
    if not control:
        return 'unknown'
    local_instance=instance_id()
    if local_instance and control['instance_id']==local_instance and control['boot_id']!=BOOT_ID:
        return 'interrupted'
    if control['lease_until'] <= time.time():
        return 'interrupted'
    if control['cancel_requested']:
        return 'cancelling'
    return job.status


def prepare(execution: Execution, metadata=None):
    job = execution.job
    job_records.task_control_create(job.id, job.user_id, metadata or {})
    token = uuid.uuid4().hex
    if not job_records.task_claim(job.id,job.user_id,instance_id(),BOOT_ID,token,LEASE_SECONDS):
        return False
    execution.token = token
    return True


def check(execution: Execution):
    job = execution.job
    control = job_records.task_control_get(job.id,job.user_id)
    if not control or control['token'] != execution.token or control['lease_until'] <= time.time():
        raise LeaseLost()
    if control['cancel_requested']:
        raise TaskCancelled()


def checkpoint(execution: Execution, **values):
    job = execution.job
    allowed = {'phase','remote_id','remote_state','provider_ref','model','endpoint_hash','parent_job_id',
               'thread_id','trace_id','successor_id','label','remote_terminal','error_type'}
    if set(values)-allowed:
        raise ValueError('检查点包含不允许持久化的字段')
    clean = {k:(v if isinstance(v,(bool,int,float)) else str(v)[:300]) for k,v in values.items()}
    if not job_records.task_checkpoint(job.id,job.user_id,execution.token,clean):
        raise LeaseLost()
