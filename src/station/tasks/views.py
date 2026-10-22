"""任务展示投影：结合执行、附件回答和产物生成原有状态与可用按钮。

只读取数据并组装响应；不启动、取消或恢复任务。列表页可传入批量查询缓存。
"""
from station.persistence.repositories import attachments as attachment_records
from station.persistence.repositories import jobs as job_records
from station.files import store as files
from station.tasks import runtime

ACTIVE={'queued','running','cancelling'}
ATTENTION={'failed','partial','interrupted','unknown','waiting_reply'}

def turn_for(job, owner):
    if job.skill_id!='station-documents':return None
    return attachment_records.attachment_turn_get(job.args.get('turn_id',''),owner)


def view(job, owner, cache=None):
    control=cache['control'].get(job.id) if cache is not None else job_records.task_control_get(job.id,owner)
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
            doc=cache['document'].get(did) if cache is not None else attachment_records.document_get(did,owner)
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
    successor_exists=checkpoint.get('successor_id') in cache['successors'] if cache is not None else bool(checkpoint.get('successor_id') and runtime.load_job(checkpoint['successor_id']))
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
