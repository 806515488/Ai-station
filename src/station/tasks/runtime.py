"""任务的执行工牌与检查点。心跳只续自己持有的租约，不接管另一台机器的活。

新手视角：数据库保存“谁在干、干到哪里”；线程消失后状态也能解释，恢复时
只使用原件 ID、模型引用和远端任务 ID，不把 API Key 或临时下载地址落盘。
"""
import hashlib
import time
import uuid
import contextvars
from contextlib import contextmanager

from station import db
from station.observability.recorder import BOOT_ID, instance_id

LEASE_SECONDS = 30
_origin=contextvars.ContextVar('task_origin',default=None)


@contextmanager
def bind_context(ctx):
    thread=getattr(ctx,'thread',None)
    token=_origin.set((getattr(ctx,'user_id',''),getattr(thread,'id','')))
    try:yield
    finally:_origin.reset(token)


def current_origin():
    return _origin.get()


class TaskCancelled(BaseException):
    """协作式取消，穿过技能的普通异常兜底，但仍执行 finally 清理。"""


class LeaseLost(BaseException):
    """执行权已过期/被接管，旧 worker 不再执行下一步或覆盖新状态。"""


def entry_snapshot(entry):
    return {'provider_ref': entry['id'], 'model': entry['model'],
            'endpoint_hash': hashlib.sha256((entry.get('base_url') or '').encode()).hexdigest()}


def resolve_remote(owner, snapshot, slot):
    from station import modelcfg
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


def prepare(job, metadata=None):
    db.task_control_create(job.id, job.user_id, metadata or {})
    token = uuid.uuid4().hex
    if not db.task_claim(job.id,job.user_id,instance_id(),BOOT_ID,token,LEASE_SECONDS):
        return False
    job._lease_token = token
    return True


def check(job):
    control = db.task_control_get(job.id,job.user_id)
    if not control or control['token'] != job._lease_token or control['lease_until'] <= time.time():
        raise LeaseLost()
    if control['cancel_requested']:
        raise TaskCancelled()


def checkpoint(job, **values):
    allowed = {'phase','remote_id','remote_state','provider_ref','model','endpoint_hash','parent_job_id',
               'thread_id','trace_id','successor_id','label','remote_terminal','error_type'}
    if set(values)-allowed:
        raise ValueError('检查点包含不允许持久化的字段')
    clean = {k:(v if isinstance(v,(bool,int,float)) else str(v)[:300]) for k,v in values.items()}
    if not db.task_checkpoint(job.id,job.user_id,job._lease_token,clean):
        raise LeaseLost()
