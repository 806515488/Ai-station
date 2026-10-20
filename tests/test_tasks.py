"""任务生命周期离线回归；外部供应商全部用替身，不产生收费任务。"""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from station import db,observability as obs
from station.app import server
from station.core.context import Context
from station.jobs.manager import Job,JobManager,get_manager
from station.tasks import runtime,service


@pytest.fixture
def owner():return db.create_user('task-owner','p')['id']


def wait(manager,job):
    until=time.monotonic()+8
    while job.id in manager._active and time.monotonic()<until:time.sleep(.01)
    assert job.id not in manager._active
    return manager.get(job.id)


def test_foreign_live_lease_cannot_be_claimed(owner):
    job=Job('demo',{});job.user_id=owner;job._persist()
    db.task_control_create(job.id,owner,{})
    assert db.task_claim(job.id,owner,'other-instance','other-boot','foreign',30)
    assert not runtime.prepare(job)
    skill=SimpleNamespace(id='demo',keys=[],heavy=False,build_runner=lambda ctx:iter(()))
    assert JobManager().resume(job.id,skill) is None
    assert db.job_load(job.id).status=='queued'
    assert runtime.state(job,db.task_control_get(job.id,owner))=='queued'
    assert not db.task_heartbeat(job.id,owner,'wrong',30)


def test_legacy_running_job_is_unknown_and_expired_job_is_interrupted(owner):
    job=Job('demo',{});job.user_id=owner;job.status='running';job._persist()
    assert JobManager().get(job.id)['status']=='unknown'
    db.task_control_create(job.id,owner,{})
    assert db.task_claim(job.id,owner,'foreign','boot','foreign',-1)
    assert JobManager().get(job.id)['status']=='interrupted'


def test_cancel_queue_releases_slot_without_running(owner):
    from station.core import heavy
    entered=[]
    def runner(ctx,**kwargs):
        entered.append(True);yield {'type':'progress','percent':100,'message':'done'}
    skill=SimpleNamespace(id='queue-test',name='示例',keys=[],heavy=True,build_runner=runner)
    manager=JobManager()
    with heavy.heavy_slot('holding'):
        job=manager.submit(skill,{},owner)
        assert manager.get(job.id)['status']=='queued'
        db.task_cancel(job.id,owner)
        snapshot=wait(manager,job)
        assert snapshot['status']=='cancelled' and not entered
    job=manager.submit(skill,{},owner)
    assert wait(manager,job)['status']=='done' and entered==[True]


def test_task_action_and_registration_are_idempotent(owner):
    job=Job('archive',{'project':'saved'});job.user_id=owner;job.status='failed';job._persist()
    db.task_control_create(job.id,owner,{})
    first=db.task_action_reserve(owner,job.id,'retry_cached','same','a'*16)
    second=db.task_action_reserve(owner,job.id,'retry_cached','same','b'*16)
    third=db.task_action_reserve(owner,job.id,'retry_cached','another','c'*16)
    assert first['next_job_id']==second['next_job_id']==third['next_job_id']=='a'*16
    child=Job('archive',{});child.user_id=owner;child.id='a'*16
    assert db.task_job_register(child,{'parent_job_id':job.id})
    child.status='done';db.job_save(child)
    duplicate=Job('archive',{});duplicate.user_id=owner;duplicate.id=child.id
    assert not db.task_job_register(duplicate,{})
    assert db.job_load(child.id).status=='done'


def test_listing_is_owner_scoped_and_actions_do_not_accept_parameters(owner):
    job=Job('video',{'prompt':'private prompt'});job.user_id=owner;job.status='failed';job._persist()
    assert service.listing('other')['tasks']==[]
    with pytest.raises(LookupError):service.owned(job.id,'other')
    row=service.view(job,owner)
    assert row['status']=='failed' and not row['actions']
    assert 'private prompt' in row['title']
    with pytest.raises(ValueError):service.perform(owner,job.id,'resume_remote','key')
    with pytest.raises(ValueError):service.listing(owner,'not-a-cursor')


def test_reply_only_never_replays_tool(owner):
    from station.core.agent import run_agent
    from station.core.session import Thread
    from station.core.tool import Tool
    called=[]
    tool=Tool('demo.side_effect','d',lambda *a,**k:called.append(True))
    skill=SimpleNamespace(tools=[tool])
    class Model:
        def stream(self,msgs,tools):
            assert tools is None
            yield {'final':{'content':'','tool_calls':[{'name':tool.name,'arguments':{},'id':'call'}]}}
    thread=Thread();thread.user_id=owner;thread.add_user('继续回答')
    events=list(run_agent(Context(user_id=owner,reply_only=True),thread,skill,Model()))
    assert any(e.type=='error' for e in events) and not called


def test_resume_video_download_does_not_create_again(owner,monkeypatch):
    from station.skills.registry import get_registry
    skill=get_registry().get('video')
    import video_tools as vt
    import agnes
    from station import modelcfg
    entry={'id':'video-source','model':'example-video','base_url':'https://example.invalid/v1','api_key':'synthetic'}
    checkpoint={**runtime.entry_snapshot(entry),'remote_id':'remote-existing','phase':'download'}
    monkeypatch.setattr(modelcfg,'resolve_one',lambda *a:[entry])
    calls=[]
    monkeypatch.setattr(agnes,'create_task',lambda *a,**k:(_ for _ in ()).throw(AssertionError('must not create')))
    monkeypatch.setattr(agnes,'query_task',lambda *a,**k:{'status':'completed','url':'https://example.invalid/video'})
    monkeypatch.setattr(agnes,'download',lambda url:calls.append(url) or b'fake-video')
    ctx=Context(user_id=owner,skill_id='video',task_recovery=checkpoint)
    values=list(vt.build_runner(ctx,prompt='example'))
    assert len(calls)==1 and any(e['type']=='artifact' for e in values)


def test_resume_asr_queries_original_id_without_submit(owner,monkeypatch):
    from station.skills.registry import get_registry
    get_registry().get('interview-coach')
    import dashscope_filetrans as protocol
    monkeypatch.setattr(protocol,'submit_task',lambda *a,**k:(_ for _ in ()).throw(AssertionError('must not submit')))
    ids=[]
    monkeypatch.setattr(protocol,'query_task',lambda base,key,task:ids.append(task) or {'output':{'task_status':'SUCCEEDED','results':[{'transcription_url':'https://example.invalid/result'}]}})
    monkeypatch.setattr(protocol,'download_result',lambda url:{'transcripts':[]})
    values=list(protocol.iter_transcribe('https://example.invalid','synthetic','qwen-audio-3.0-filetrans','',existing_task_id='existing'))
    assert ids==['existing'] and any(e['type']=='result' for e in values)


def test_old_worker_cannot_overwrite_new_lease(owner,monkeypatch):
    job=Job('demo',{});job.user_id=owner;job._persist()
    assert runtime.prepare(job)
    old=job._lease_token
    db.task_release(job.id,owner,old)
    assert db.task_claim(job.id,owner,'foreign','new-boot','new-token',30)
    with pytest.raises(runtime.LeaseLost):job.update(status='done')
    assert db.job_load(job.id).status=='queued'
    # 即使旧线程刚通过心跳检查、随后被接管，最终 SQL 也必须拒绝它的写入。
    monkeypatch.setattr(db,'task_heartbeat',lambda *a:True)
    with pytest.raises(runtime.LeaseLost):job.update(status='done')
    assert db.job_load(job.id).status=='queued'
    db.task_release(job.id,owner,old)
    assert db.task_control_get(job.id,owner)['lease_until']>time.time()


def test_api_retry_uses_saved_arguments_and_only_one_successor(monkeypatch,tmp_path):
    from station.jobs import manager as manager_module
    from station.skills import registry
    manager=JobManager();monkeypatch.setattr(manager_module,'get_manager',lambda:manager)
    calls=[]
    def runner(ctx,**args):calls.append(args);yield {'type':'progress','percent':100,'message':'done'}
    skill=SimpleNamespace(id='archive',name='测试',keys=[],heavy=False,build_runner=runner)
    monkeypatch.setattr(registry,'get_registry',lambda:SimpleNamespace(get=lambda sid:skill))
    monkeypatch.setenv('STATION_FAKE','1')
    with TestClient(server.app) as client:
        owner=client.post('/api/auth/register',json={'name':'task-api','password':'p'}).json()['user']['id']
        project=tmp_path/'saved-project.json';project.write_text('{}')
        parent=Job('archive',{'project':str(project),'person':'saved-person'});parent.user_id=owner;parent.status='failed';parent._persist()
        payload={'action':'retry_cached','client_key':'retry-once','args':{'project':'cannot-replace'}}
        first=client.post('/api/tasks/'+parent.id+'/actions',json=payload)
        assert first.status_code==200,first.text
        child=first.json()['task']['id']
        assert client.post('/api/tasks/'+parent.id+'/actions',json=payload).json()['task']['id']==child
        assert client.post('/api/tasks/'+parent.id+'/actions',json={**payload,'client_key':'other-key'}).json()['task']['id']==child
        assert wait(manager,db.job_load(child))['status']=='done'
        assert calls==[{'project':str(project),'person':'saved-person','refresh':False}]
        assert client.get('/api/tasks').status_code==200
        assert client.get('/api/tasks?cursor=bad').status_code==400
        client.post('/api/auth/logout')
        assert client.get('/api/tasks').status_code==401
        client.post('/api/auth/register',json={'name':'task-other','password':'p'})
        assert client.get('/api/tasks/'+parent.id).status_code==404
        assert client.post('/api/tasks/'+parent.id+'/actions',json=payload).status_code==404


def test_video_submission_500_does_not_fall_back_and_duplicate(owner,monkeypatch):
    from station.skills.registry import get_registry
    get_registry().get('video')
    import video_tools as vt
    import agnes
    from station import modelcfg
    calls=[]
    entries=[{'id':i,'model':'example','base_url':'https://example.invalid','api_key':'synthetic'} for i in ('first','second')]
    monkeypatch.setattr(modelcfg,'resolve',lambda *a:entries)
    def create(*a,**k):calls.append(True);raise agnes.AgnesError('unknown outcome',status=500)
    monkeypatch.setattr(agnes,'create_task',create)
    with pytest.raises(agnes.AgnesError):list(vt.build_runner(Context(user_id=owner),prompt='example'))
    assert calls==[True]
