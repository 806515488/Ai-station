"""任务中心用例：同一视图展示处理、回答和产物，操作由服务端白名单决定。

不接受浏览器补交执行参数，也不通过恢复按钮创建付费远端任务；查询/下载恢复
使用已持久化的远端 ID，重新解析/识别产生接续任务，历史结果原样保留。
"""
import base64
import json
import math
import re
import time
import uuid
from pathlib import Path

from station import db
from station.files import store as files
from station.tasks import runtime

ACTIVE={'queued','running','cancelling'}
ATTENTION={'failed','partial','interrupted','unknown','waiting_reply'}


def owned(job_id, owner):
    job=db.job_load(job_id)
    if not job or job.user_id!=owner:raise LookupError('任务不存在')
    return job


def turn_for(job, owner):
    if job.skill_id!='station-documents':return None
    return db.attachment_turn_get(job.args.get('turn_id',''),owner)


def view(job, owner, cache=None):
    control=cache['control'].get(job.id) if cache is not None else db.task_control_get(job.id,owner)
    checkpoint=(control or {}).get('record',{})
    processing=runtime.state(job,control)
    turn=(cache['turn'].get(job.args.get('turn_id','')) if job.skill_id=='station-documents' else None) if cache is not None else turn_for(job,owner)
    reply='none';artifacts=[];documents=[];good=0
    title={'video':'视频生成','archive':'档案识别','interview-coach':'音频转写'}.get(job.skill_id,job.skill_id)
    origin=(job.result or {}).get('_trace_origin',{})
    thread_id=checkpoint.get('thread_id') or origin.get('thread_id','')
    if turn:
        thread_id=turn.get('thread_id') or thread_id
        reply=turn['state'] if turn.get('thread_id') else 'none'
        good=bad=0
        for did in turn['attachment_ids']:
            doc=cache['document'].get(did) if cache is not None else db.document_get(did,owner)
            if not doc:continue
            run_id=turn.get('run_refs',{}).get(did)
            run=doc.get('runs',{}).get(run_id,{})
            documents.append({'id':did,'name':doc['name'],'run_id':run_id,'state':run.get('state',doc['status'])})
            if run.get('manifest'):
                artifacts.append({'name':doc['name']+' · 解析结果包','url':f'/api/documents/{did}/archive?run_id={run_id}'})
            if run.get('quality')=='unusable' or did in turn.get('failures',{}):bad+=1
            elif run.get('manifest'):good+=1
            if run.get('state')=='partial':bad+=1
        if processing=='done' and bad:processing='partial' if good else 'failed'
        title='解析 · '+('、'.join(d['name'] for d in documents) or '附件')
    elif job.skill_id=='video':title+=' · '+str(job.args.get('prompt',''))[:40]
    elif job.skill_id=='archive':title+=' · '+str(job.args.get('person',''))[:40]
    elif job.skill_id=='interview-coach':title+=' · '+str((files.meta(job.args.get('file_id','')) or {}).get('name',''))[:60]
    for fid in job.artifacts:
        meta=files.meta(fid)
        if meta and meta.get('owner')==owner:artifacts.append({'name':meta.get('name') or fid,'url':'/api/files/'+fid})
    status=processing
    if processing=='done' and reply in ('failed','interrupted'):status='waiting_reply'
    elif processing=='done' and reply=='ready':status='waiting_reply'
    elif processing=='done' and reply=='running_agent':status='running'
    actions=[]
    if control and processing in ('queued','running'):
        actions.append({'id':'cancel','label':'停止本地等待（远端可能继续）' if checkpoint.get('remote_id') or checkpoint.get('phase')=='submitting' else '取消处理'})
    if turn and reply in ('ready','failed','interrupted') and any(d.get('run_id') for d in documents):
        actions.append({'id':'continue_answer','label':'继续回答（复用已有结果）' if good else '继续说明处理结果'})
    terminal=processing not in ACTIVE
    successor_exists=checkpoint.get('successor_id') in cache['successors'] if cache is not None else bool(checkpoint.get('successor_id') and db.job_load(checkpoint['successor_id']))
    if terminal and not successor_exists:
        if turn:actions.append({'id':'reprocess','label':'重新解析与切块（新版本）'})
        elif job.skill_id=='archive' and processing in ('failed','interrupted','unknown'):
            actions.append({'id':'retry_cached','label':'重新识别（复用缓存）'})
        elif job.skill_id in ('video','interview-coach') and checkpoint.get('remote_id') and not checkpoint.get('remote_terminal') and processing!='done':
            actions.append({'id':'resume_remote','label':'继续查询原任务并取回结果'})
    hint=''
    if processing=='unknown':hint='旧任务缺少执行归属，当前状态待核验。已有结果保留，请确认后再重新处理。'
    elif checkpoint.get('phase')=='submitting' and not checkpoint.get('remote_id'):
        hint='提交结果尚未确认，不能确定远端是否已受理。请回原会话或供应商处核对，系统不会自动重发。'
    elif processing in ('failed','interrupted','cancelled') and checkpoint.get('remote_id'):
        hint='原远端任务编号已保留。继续操作只查询并取回原结果，不创建新的远端任务。'
    if checkpoint.get('successor_id'):hint='已有接续任务，历史结果保留，可查看下方接续任务。'
    if turn and processing=='partial':hint='部分正文或切块已保留，请核对质量报告；重新处理会生成新版本。'
    return {'id':job.id,'type':job.skill_id,'title':title[:180],'status':status,'processing_state':processing,
            'reply_state':reply,'body_available':bool(good),'message':job.message[:400],'progress':job.progress,'created':job.created,'updated':job.updated,
            'thread_id':thread_id,'trace_id':checkpoint.get('trace_id') or origin.get('trace_id',''),
            'parent_job_id':checkpoint.get('parent_job_id',''),'successor_id':checkpoint.get('successor_id',''),
            'phase':checkpoint.get('phase',''),'hint':hint,'artifacts':artifacts,'documents':documents,'actions':actions}


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
    jobs=db.task_job_list(owner,before,101)
    controls=db.task_records_many('control',owner,[j.id for j in jobs[:100]])
    turns=db.task_records_many('turn',owner,[j.args.get('turn_id','') for j in jobs[:100] if j.skill_id=='station-documents'])
    documents=db.task_records_many('document',owner,[did for turn in turns.values() for did in turn.get('attachment_ids',[])])
    successors=db.task_existing_jobs(owner,[c['record'].get('successor_id','') for c in controls.values()])
    cache={'control':controls,'turn':turns,'document':documents,'successors':successors}
    result=[]
    for job in jobs[:100]:
        row=view(job,owner,cache)
        match=(category=='all' or category=='active' and row['status'] in ACTIVE or
               category=='attention' and row['status'] in ATTENTION and not row['successor_id'] or
               category=='completed' and row['status']=='done')
        if match:result.append(row)
    following=''
    if len(jobs)>100:
        last=jobs[99];following=base64.urlsafe_b64encode(json.dumps([last.created,last.id]).encode()).decode()
    return {'tasks':result,'next_cursor':following}


def perform(owner, job_id, action, client_key):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',client_key):raise ValueError('操作编号无效')
    job=owned(job_id,owner);row=view(job,owner)
    if action=='continue_answer':
        if action not in {a['id'] for a in row['actions']}:raise ValueError('当前没有可恢复的回答')
        turn=turn_for(job,owner)
        from station.core.session import Thread
        thread=Thread.load(turn['thread_id'])
        if not thread or thread.user_id!=owner:raise ValueError('原会话已不存在')
        if thread.pending:raise ValueError('请先返回原会话完成上一操作的批准')
        if any(t['id']!=turn['id'] and t['state']=='running_agent' for t in db.attachment_turn_list(owner,thread.id)):
            raise ValueError('原会话正在生成其他回答，请稍后继续')
        if not any(m.get('attachment_turn_id')==turn['id'] for m in thread.msgs):
            thread.add({'role':'user','content':turn['message'],'attachment_turn_id':turn['id']})
            thread.save()
        db.attachment_turn_update(turn['id'],owner,{'reply_only':True,'manual_reply':False})
        return {'resume_turn_id':turn['id'],'thread_id':turn['thread_id'],'reply_only':True}
    if row['successor_id'] and action in ('reprocess','retry_cached','resume_remote') and db.job_load(row['successor_id']):
        existing_action=db.task_control_get(job.id,owner)['record'].get('successor_action')
        if existing_action and existing_action!=action:raise ValueError('已有其他接续操作，请查看接续任务')
        return {'task':view(owned(row['successor_id'],owner),owner)}
    if action not in {a['id'] for a in row['actions']}:raise ValueError('当前任务不支持该操作，请刷新状态')
    if action=='cancel':
        db.task_cancel(job_id,owner)
        turn=turn_for(job,owner)
        if turn:db.attachment_turn_update(turn['id'],owner,{'state':'cancelled'},expected=('waiting_parse',))
        return {'task':view(job,owner)}
    # 操作参数只能来自本人的原任务。预留的接续 ID 使重复点击/请求重发产生同一任务。
    if action=='reprocess':
        from station.documents import storage
        source=turn_for(job,owner)
        for did in source['attachment_ids']:
            doc=db.document_get(did,owner)
            if not doc or not (storage.root(owner,did)/('source'+doc['suffix'])).is_file():
                raise ValueError('当前实例没有原件，请在保存原件的实例操作')
    if action=='retry_cached' and not Path(job.args.get('project','')).is_file():
        raise ValueError('当前实例没有档案项目，请返回保存原件的实例操作')
    db.task_control_create(job.id,owner,{})
    reservation=db.task_action_reserve(owner,job.id,action,client_key,uuid.uuid4().hex[:16])
    next_id=reservation['next_job_id']
    existing=db.job_load(next_id)
    if existing:return {'task':view(owned(next_id,owner),owner)}
    control=db.task_control_get(job.id,owner)['record']
    metadata={k:control[k] for k in ('provider_ref','model','endpoint_hash','remote_id','remote_state','phase','thread_id','trace_id') if k in control}
    metadata.update(parent_job_id=job.id,thread_id=row['thread_id'])
    from station.jobs.manager import get_manager
    if action=='reprocess':
        from station.documents.pipeline import Executor
        from station.documents import settings,loaders
        source=turn_for(job,owner)
        cfg=settings.snapshot();cfg['components']=loaders.versions()
        record={'id':uuid.uuid4().hex,'owner_id':owner,'thread_id':row['thread_id'],'client_key':'task:'+next_id,
                'message':source['message'],'attachment_ids':source['attachment_ids'],'config':cfg,'state':'waiting_parse',
                'created':time.time(),'job_id':next_id,'run_refs':{},'failures':{},'active_skill':source.get('active_skill',''),
                'manual_reply':True}
        turn,_=db.attachment_turn_create(record)
        child=get_manager().submit(Executor(),{'turn_id':turn['id']},owner,job_id=next_id,task_metadata=metadata)
    else:
        from station.skills.registry import get_registry
        skill=get_registry().get(job.skill_id)
        if not skill or not skill.build_runner:raise ValueError('任务执行器不可用')
        args=dict(job.args)
        if action=='retry_cached':args['refresh']=False
        if job.skill_id=='interview-coach':
            db.interview_recording_start(args['recording_id'],owner,next_id)
        child=get_manager().submit(skill,args,owner,job_id=next_id,task_metadata=metadata)
    return {'task':view(child,owner)}
