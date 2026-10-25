"""任务用例：保存后由指定 Web 定时投递，执行交给项目 Celery Worker。"""
import base64
import json
import math
import re
import uuid

from station import observability as obs
from station.persistence import database
from station.persistence.repositories import jobs as records
from station.tasks import runtime, settings, views
from station.tasks.job import Job


def submit(skill, args, user_id="", job_id="", task_metadata=None, phase=None):
    """HTTP/工具只登记任务，不在调用进程启动执行线程。"""
    settings.broker_url()
    if not (getattr(skill, "run_step", None) or getattr(skill, "build_runner", None)):
        raise ValueError("该技能没有后台执行入口")
    if len(json.dumps(args, ensure_ascii=False)) > 65536:
        raise ValueError("任务参数过大，请使用文件引用")
    if job_id:
        existing = runtime.load_job(job_id)
        if existing:
            if existing.user_id != user_id or existing.skill_id != skill.id or existing.args != args:
                raise ValueError("任务编号已被使用")
            return existing
    meta = dict(task_metadata or {})
    origin = obs.current()
    caller = runtime.current_origin()
    if caller and caller[0] == user_id and caller[1]:
        meta["thread_id"] = caller[1]
    if origin and origin.record["user_id"] == user_id:
        meta.update(thread_id=origin.record["thread_id"], trace_id=origin.record["id"])
    first = phase or getattr(skill, "task_initial_phase", "run")
    job = Job(skill.id, dict(args), user_id=user_id, phase=first,
              queue=settings.queue(getattr(skill, "heavy", True)), meta=meta,
              message="任务已保存，等待投递")
    if job_id:
        job.id = job_id
    if meta.get("trace_id"):
        job.result = {"_trace_origin": {"trace_id": meta["trace_id"], "thread_id": meta.get("thread_id", "")}}
    with obs.span("job_submit", "登记后台任务", job_id=job.id, skill=skill.id):
        records.task_job_register(job.to_record())
    return runtime.load_job(job.id)


def snapshot(job_id):
    job = runtime.load_job(job_id)
    return job.snapshot() if job else None


def owned(job_id, owner):
    job = runtime.load_job(job_id)
    if not job or job.user_id != owner:
        raise LookupError("任务不存在")
    return job


def listing(owner, cursor="", category="all"):
    if category not in ("all", "active", "attention", "completed"):
        raise ValueError("任务筛选无效")
    before = None
    if cursor:
        try:
            if len(cursor) > 256:
                raise ValueError()
            raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            before = float(raw[0]), str(raw[1])
            if not math.isfinite(before[0]) or not re.fullmatch(r"[0-9a-f]{16,32}", before[1]):
                raise ValueError()
        except Exception:
            raise ValueError("分页标记无效") from None
    jobs = [Job.from_record(r) for r in records.task_job_list(owner, before, 101)]
    result = []
    for job in jobs[:100]:
        row = views.view(job, owner)
        if (category == "all" or category == "active" and row["status"] in views.ACTIVE
                or category == "attention" and row["status"] in views.ATTENTION and not row["successor_id"]
                or category == "completed" and row["status"] == "done"):
            result.append(row)
    following = ""
    if len(jobs) > 100:
        following = base64.urlsafe_b64encode(json.dumps([jobs[99].created, jobs[99].id]).encode()).decode()
    return {"tasks": result, "next_cursor": following}


def perform(owner, job_id, action, client_key):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", client_key):
        raise ValueError("操作编号无效")
    job = owned(job_id, owner)
    # 同一手动请求重发时不重新生成步骤消息。
    previous = records.action_exists(owner, job_id, action, client_key)
    if previous:
        return {"task": views.view(owned(previous["next_job_id"], owner), owner)}
    row = views.view(job, owner)
    if row["successor_id"] and action == "retry_cached":
        return {"task": views.view(owned(row["successor_id"], owner), owner)}
    if action not in {a["id"] for a in row["actions"]}:
        raise ValueError("当前任务不支持该操作，请刷新状态")
    if action == "cancel":
        with database.transaction():
            if records.reserve_self_action(owner, job_id, action, client_key):
                records.task_cancel(job_id, owner)
    elif action == "retry_publish":
        with database.transaction():
            if records.reserve_self_action(owner, job_id, action, client_key):
                records.retry_publish(job_id, owner)
    else:
        skill, handler = recovery_for(job)
        handler.validate(job, owner)
        with database.transaction():
            if action == "resume_remote":
                if records.reserve_self_action(owner, job_id, action, client_key):
                    handler.prepare(job, owner, job_id, job.meta)
                    job.meta["retry_count"] = 0
                    records.reschedule(job_id, owner, phase="collect" if job.meta.get("phase") in ("download", "collect") else "poll",
                                       queue=settings.queue(False), metadata=job.meta)
            else:
                reservation = records.task_action_reserve(owner, job_id, action, client_key, uuid.uuid4().hex[:16])
                child_id = reservation["next_job_id"]
                meta = {**job.meta, "parent_job_id": job_id}
                meta.pop("successor_id", None)
                meta.pop("successor_action", None)
                args = handler.prepare(job, owner, child_id, meta)
                child = submit(skill, args, owner, job_id=child_id, task_metadata=meta,
                               phase="export" if job.phase == "export" else None)
                return {"task": views.view(child, owner)}
    return {"task": views.view(owned(job_id, owner), owner)}


def recovery_for(job):
    from station.skills.registry import get_registry
    skill = get_registry().get(job.skill_id)
    handler = getattr(skill, "task_recovery", None)
    if not skill or not (skill.run_step or skill.build_runner) or handler is None:
        raise ValueError("任务执行器不可用")
    return skill, handler
