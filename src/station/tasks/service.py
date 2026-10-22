"""任务中心用例：本人查询、分页、取消、仅继续回答与幂等接续。

展示在 views.py，运行状态在 runtime.py，技能自己的恢复参数与本地检查归技能。
不接受前端补交原文、路径、配置或执行参数；保持原动作白名单和预留接续 ID。
"""
import base64
import json
import math
import re
import uuid

from station.persistence.repositories import attachments as attachment_records
from station.persistence.repositories import jobs as job_records
from station.tasks.job import Job
from station.tasks import runtime, views

def owned(job_id, owner):
    job=runtime.load_job(job_id)
    if not job or job.user_id!=owner:raise LookupError('任务不存在')
    return job


def listing(owner, cursor='', category='all'):
    if category not in ('all','active','attention','completed'):raise ValueError('任务筛选无效')
    before=None
    if cursor:
        try:
            if len(cursor)>256:raise ValueError()
            raw=json.loads(base64.urlsafe_b64decode(cursor.encode()))
            before=(float(raw[0]),str(raw[1]))
            if not math.isfinite(before[0]) or not re.fullmatch(r'[0-9a-f]{16,32}',before[1]):raise ValueError()
        except Exception:raise ValueError('分页标记无效') from None
    jobs=[Job.from_record(record) for record in job_records.task_job_list(owner,before,101)]
    controls=job_records.task_records_many('control',owner,[j.id for j in jobs[:100]])
    turns=job_records.task_records_many('turn',owner,[j.args.get('turn_id','') for j in jobs[:100] if j.skill_id=='station-documents'])
    documents=job_records.task_records_many('document',owner,[did for turn in turns.values() for did in turn.get('attachment_ids',[])])
    successors=job_records.task_existing_jobs(owner,[c['record'].get('successor_id','') for c in controls.values()])
    cache={'control':controls,'turn':turns,'document':documents,'successors':successors}
    result=[]
    for job in jobs[:100]:
        row=views.view(job,owner,cache)
        match=(category=='all' or category=='active' and row['status'] in views.ACTIVE or
               category=='attention' and row['status'] in views.ATTENTION and not row['successor_id'] or
               category=='completed' and row['status']=='done')
        if match:result.append(row)
    following=''
    if len(jobs)>100:
        last=jobs[99];following=base64.urlsafe_b64encode(json.dumps([last.created,last.id]).encode()).decode()
    return {'tasks':result,'next_cursor':following}


def perform(owner, job_id, action, client_key):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',client_key):raise ValueError('操作编号无效')
    job=owned(job_id,owner);row=views.view(job,owner)
    if action=='continue_answer':
        if action not in {a['id'] for a in row['actions']}:raise ValueError('当前没有可恢复的回答')
        turn=views.turn_for(job,owner)
        from station.core.session import Thread
        thread=Thread.load(turn['thread_id'])
        if not thread or thread.user_id!=owner:raise ValueError('原会话已不存在')
        if thread.pending:raise ValueError('请先返回原会话完成上一操作的批准')
        if any(t['id']!=turn['id'] and t['state']=='running_agent' for t in attachment_records.attachment_turn_list(owner,thread.id)):
            raise ValueError('原会话正在生成其他回答，请稍后继续')
        if not any(m.get('attachment_turn_id')==turn['id'] for m in thread.msgs):
            thread.add({'role':'user','content':turn['message'],'attachment_turn_id':turn['id']})
            thread.save()
        attachment_records.attachment_turn_update(turn['id'],owner,{'reply_only':True,'manual_reply':False})
        return {'resume_turn_id':turn['id'],'thread_id':turn['thread_id'],'reply_only':True}
    if row['successor_id'] and action in ('reprocess','retry_cached','resume_remote') and runtime.load_job(row['successor_id']):
        existing_action=job_records.task_control_get(job.id,owner)['record'].get('successor_action')
        if existing_action and existing_action!=action:raise ValueError('已有其他接续操作，请查看接续任务')
        return {'task':views.view(owned(row['successor_id'],owner),owner)}
    if action not in {a['id'] for a in row['actions']}:raise ValueError('当前任务不支持该操作，请刷新状态')
    if action=='cancel':
        job_records.task_cancel(job_id,owner)
        turn=views.turn_for(job,owner)
        if turn:attachment_records.attachment_turn_update(turn['id'],owner,{'state':'cancelled'},expected=('waiting_parse',))
        return {'task':views.view(job,owner)}
    # 操作参数只能来自本人的原任务。预留的接续 ID 使重复点击/请求重发产生同一任务。
    skill, handler = recovery_for(job)
    handler.validate(job, owner)
    job_records.task_control_create(job.id,owner,{})
    reservation=job_records.task_action_reserve(owner,job.id,action,client_key,uuid.uuid4().hex[:16])
    next_id=reservation['next_job_id']
    existing=runtime.load_job(next_id)
    if existing:return {'task':views.view(owned(next_id,owner),owner)}
    control=job_records.task_control_get(job.id,owner)['record']
    metadata={k:control[k] for k in ('provider_ref','model','endpoint_hash','remote_id','remote_state','phase','thread_id','trace_id') if k in control}
    metadata.update(parent_job_id=job.id,thread_id=row['thread_id'])
    from station.tasks.executor import get_manager
    args = handler.prepare(job, owner, next_id, metadata)
    child = get_manager().submit(skill, args, owner, job_id=next_id, task_metadata=metadata)
    return {'task':views.view(child,owner)}


def recovery_for(job):
    """选择原任务的既有执行器与恢复适配；宿主不解释技能参数。"""
    if job.skill_id == "station-documents":
        from station.documents.pipeline import Executor
        skill = Executor()
    else:
        from station.skills.registry import get_registry
        skill = get_registry().get(job.skill_id)
    handler = getattr(skill, "task_recovery", None)
    if not skill or not skill.build_runner or handler is None:
        raise ValueError("任务执行器不可用")
    return skill, handler
