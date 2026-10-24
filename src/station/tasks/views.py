"""任务展示投影：根据执行与产物生成状态和可用按钮。

只读取数据并组装响应；不启动、取消或恢复任务。列表页可传入批量查询缓存。
"""
from station.persistence.repositories import jobs as job_records
from station.files import store as files
from station.tasks import runtime

ACTIVE={'queued','running','cancelling'}
ATTENTION={'failed','partial','interrupted','unknown'}

def view(job, owner, cache=None):
    control=cache['control'].get(job.id) if cache is not None else job_records.task_control_get(job.id,owner)
    checkpoint=(control or {}).get('record',{})
    processing=runtime.state(job,control)
    artifacts=[]
    title={'video':'视频生成','archive':'档案识别','interview-coach':'音频转写'}.get(job.skill_id,job.skill_id)
    origin=(job.result or {}).get('_trace_origin',{})
    thread_id=checkpoint.get('thread_id') or origin.get('thread_id','')
    if job.skill_id=='video':title+=' · '+str(job.args.get('prompt',''))[:40]
    elif job.skill_id=='archive':title+=' · '+str(job.args.get('person',''))[:40]
    elif job.skill_id=='interview-coach':title+=' · '+str((files.meta(job.args.get('file_id','')) or {}).get('name',''))[:60]
    for fid in job.artifacts:
        meta=files.meta(fid)
        if meta and meta.get('owner')==owner:artifacts.append({'name':meta.get('name') or fid,'url':'/api/files/'+fid})
    status=processing
    actions=[]
    if control and processing in ('queued','running'):
        actions.append({'id':'cancel','label':'停止本地等待（远端可能继续）' if checkpoint.get('remote_id') or checkpoint.get('phase')=='submitting' else '取消处理'})
    terminal=processing not in ACTIVE
    successor_exists=checkpoint.get('successor_id') in cache['successors'] if cache is not None else bool(checkpoint.get('successor_id') and runtime.load_job(checkpoint['successor_id']))
    if terminal and not successor_exists:
        if job.skill_id=='archive' and processing in ('failed','interrupted','unknown'):
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
    return {'id':job.id,'type':job.skill_id,'title':title[:180],'status':status,'processing_state':processing,
            'message':job.message[:400],'progress':job.progress,'created':job.created,'updated':job.updated,
            'thread_id':thread_id,'trace_id':checkpoint.get('trace_id') or origin.get('trace_id',''),
            'parent_job_id':checkpoint.get('parent_job_id',''),'successor_id':checkpoint.get('successor_id',''),
            'phase':checkpoint.get('phase',''),'hint':hint,'artifacts':artifacts,'actions':actions}
