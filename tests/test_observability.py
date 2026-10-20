"""执行追踪离线回归：真实临时 DB、HTTP/SSE、失败降级和用户隔离。"""
import concurrent.futures
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from station import db, observability as obs
from station.app import server
from station.core.context import Context
from station.core.tool import Tool
from station.observability.service import detail


def events(response):
    assert response.status_code == 200
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('STATION_FAKE','1')
    with TestClient(server.app) as value:
        assert value.post('/api/auth/register',json={'name':'tracer','password':'p'}).status_code==200
        yield value


def test_chat_trace_survives_reload_and_is_owner_scoped(client):
    rows=events(client.post('/api/chat',json={'message':'你好'}))
    assert not [r for r in rows if r['type']=='error'], rows
    trace=next(r for r in rows if r['type']=='trace')
    response=client.get('/api/traces/'+trace['trace_id'])
    assert response.status_code==200
    value=response.json()
    assert value['state']=='success' and value['active_ms']>=0
    assert value['elapsed_ms']+2>=value['active_ms']>=value['first_text_ms']
    assert {'request','route','assembly','model'} <= {s['kind'] for s in value['spans']}
    assert all(s['state']!='running' for s in value['spans'])
    session=client.get('/api/sessions/'+trace['thread_id']).json()
    assert next(m for m in session['msgs'] if m['role']=='user')['trace_id']==trace['trace_id']
    assert len(client.get('/api/sessions/'+trace['thread_id']+'/traces').json()['traces'])==1
    client.post('/api/auth/logout')
    assert client.get('/api/traces/'+trace['trace_id']).status_code==401
    client.post('/api/auth/register',json={'name':'other-tracer','password':'p'})
    assert client.get('/api/traces/'+trace['trace_id']).status_code==404
    assert client.get('/api/sessions/'+trace['thread_id']+'/traces').status_code==404


def test_tool_exception_is_error_even_when_returned_as_text():
    from station.core.agent import run_tool
    user=db.create_user('tool-owner','p')['id']
    trace=obs.begin(user,'thread')
    def broken(ctx,**kwargs):raise ValueError('secret-in-exception')
    with obs.activate(trace):
        result=run_tool(Context(user_id=user),Tool('demo.fail','d',broken),{'password':'secret-value'})
    obs.finish(trace,'success')
    assert 'ValueError' in result
    value=detail(trace.record['id'],user)
    step=value['spans'][0]
    assert step['state']=='error' and value['has_errors']
    assert 'secret-value' not in json.dumps(value) and 'secret-in-exception' not in json.dumps(value)


def test_trace_storage_failure_does_not_break_chat(client,monkeypatch):
    monkeypatch.setattr(db,'trace_create',lambda *a: (_ for _ in ()).throw(RuntimeError('db')))
    rows=events(client.post('/api/chat',json={'message':'你好'}))
    assert any(r['type']=='delta' for r in rows)
    assert not any(r['type']=='error' for r in rows)


def test_stream_context_isolation_across_worker_threads():
    user=db.create_user('contexts','p')['id']
    trace=obs.begin(user,'thread')
    def generate():
        with obs.activate(trace),obs.span('request','request'):
            yield obs.current().record['id']
            yield obs.current().record['id']
    iterator=obs.isolated_stream(generate())
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert pool.submit(next,iterator).result()==trace.record['id']
        assert pool.submit(next,iterator).result()==trace.record['id']
        pool.submit(iterator.close).result()
    assert obs.current() is None
    assert detail(trace.record['id'],user)['spans'][0]['state']=='interrupted'


def test_model_attempt_fallback_records_actual_provider_without_key(monkeypatch):
    from station.core import model
    from langchain_core.messages import AIMessageChunk
    user=db.create_user('model-owner','p')['id']
    trace=obs.begin(user,'thread')
    class LLM:
        def __init__(self,bad):self.bad=bad
        def stream(self,msgs):
            if self.bad:raise ConnectionError('secret-upstream')
            yield AIMessageChunk(content='成功',usage_metadata={'input_tokens':3,'output_tokens':2,'total_tokens':5})
    entries=[{'id':'first','model':'a','api_key':'synthetic-private'}, {'id':'second','model':'b','api_key':'synthetic-private'}]
    instance=model.Model(entries=entries,retries=0)
    monkeypatch.setattr(instance,'_client',lambda entry,budget: LLM(entry['id']=='first'))
    with obs.activate(trace):parts=list(instance.stream([{'role':'user','content':'hello'}]))
    obs.finish(trace,'success')
    value=detail(trace.record['id'],user)
    assert [s['state'] for s in value['spans']]==['error','success']
    assert [s['attributes']['id'] for s in value['spans']]==['first','second']
    assert value['spans'][-1]['attributes']['usage']['total_tokens']==5
    assert 'synthetic-private' not in json.dumps(value) and 'secret-upstream' not in json.dumps(value)


def test_resume_key_reuses_trace_but_has_distinct_request_ids():
    user=db.create_user('resume-owner','p')['id']
    a=obs.begin(user,'thread',key='attachment:fixed')
    with obs.activate(a),obs.span('request','submit'):pass
    obs.finish(a,'waiting_parse')
    b=obs.begin(user,'thread',key='attachment:fixed')
    with obs.activate(b),obs.span('request','resume'):pass
    obs.finish(b,'success')
    assert a.record['id']==b.record['id'] and a.request_id!=b.request_id
    assert len(detail(b.record['id'],user)['spans'])==2


def test_attachment_submit_duplicate_and_resume_share_trace(client,monkeypatch):
    from station.jobs.manager import get_manager
    monkeypatch.setattr(get_manager(),'submit',lambda *a,**k:None)
    response=client.post('/api/documents/upload',data={'client_upload_id':'trace-upload'},
                         files=[('files',('example.txt',b'example text'))])
    document=response.json()['documents'][0]
    payload={'message':'总结附件','attachment_ids':[document['id']],'client_turn_id':'trace-turn'}
    initial=events(client.post('/api/chat',json=payload))
    tid=next(r['thread_id'] for r in initial if r['type']=='trace')
    trace_id=next(r['trace_id'] for r in initial if r['type']=='trace')
    turn_id=next(r['turn_id'] for r in initial if r['type']=='attachment_wait')
    assert client.get('/api/traces/'+trace_id).json()['state']=='waiting_parse'
    duplicate=events(client.post('/api/chat',json=payload))
    assert next(r['trace_id'] for r in duplicate if r['type']=='trace')==trace_id
    owner=db.thread_load(tid).user_id
    db.attachment_turn_update(turn_id,owner,{'state':'ready'})
    resumed=events(client.post('/api/chat',json={'resume_turn_id':turn_id,'message':'不能替换原话'}))
    assert not any(r['type']=='error' for r in resumed),resumed
    assert next(r['trace_id'] for r in resumed if r['type']=='trace')==trace_id
    value=client.get('/api/traces/'+trace_id).json()
    assert value['state']=='success'
    requests=[s for s in value['spans'] if s['kind']=='request']
    assert len(requests)==3 and len({s['request_id'] for s in requests})==3
    messages=db.thread_load(tid).msgs
    assert len([m for m in messages if m['role']=='user'])==1
    assert messages[0]['content']=='总结附件'


def test_jobs_keep_origin_after_request_finishes():
    from station.jobs.manager import JobManager
    user=db.create_user('job-owner','p')['id']
    trace=obs.begin(user,'thread')
    def runner(ctx):
        with obs.span('work','处理任务'):yield {'type':'artifact','id':'artifact-one'}
    skill=SimpleNamespace(id='fake-job',name='示例',keys=[],heavy=False,build_runner=lambda ctx,**kw:runner(ctx))
    manager=JobManager()
    with obs.activate(trace):job=manager.submit(skill,{},user)
    obs.finish(trace,'success')
    until=time.monotonic()+5
    while manager._active and time.monotonic()<until:time.sleep(.01)
    assert not manager._active
    related=detail(trace.record['id'],user)['related']
    assert len(related)==1 and related[0]['state']=='success'
    child=detail(related[0]['id'],user)
    assert child['origin_trace_id']==trace.record['id']
    assert {'job','queue','work'} <= {s['kind'] for s in child['spans']}
    assert next(s for s in child['spans'] if s['kind']=='job')['attributes']['artifacts']==['artifact-one']


def test_mcp_protocol_error_marks_tool_failed_without_changing_result(monkeypatch):
    from station.mcp import client as mcli
    from station.mcp.config import ServerSpec
    from station.core.agent import run_tool
    user=db.create_user('mcp-owner','p')['id']
    trace=obs.begin(user,'thread')
    class Session:
        async def call_tool(self,name,args):
            return SimpleNamespace(isError=True,content=[SimpleNamespace(type='text',text='上游错误')])
    async def session(spec,fn):return await fn(Session())
    monkeypatch.setattr(mcli,'_with_session',session)
    tool=Tool('mcp.test.search','d',lambda ctx,**kwargs:mcli.call_tool(ServerSpec('test','test','stdio'),'search',kwargs))
    with obs.activate(trace):result=run_tool(Context(user_id=user),tool,{})
    obs.finish(trace,'success')
    assert '上游错误' in result
    value=detail(trace.record['id'],user)
    assert all(s['state']=='error' for s in value['spans'])


def test_restart_marks_only_local_instance_inflight_steps(monkeypatch):
    from station.observability import recorder
    user=db.create_user('restart-owner','p')['id']
    trace=obs.begin(user,'thread')
    with obs.activate(trace):
        cm=obs.span('request','request');step=cm.__enter__()
        try:
            monkeypatch.setattr(recorder,'BOOT_ID','new-boot')
            # service 持有导入时的 ID，因此模拟旧记录而不是改变 service 的当前 ID。
            trace.record['boot_id']='old-boot';trace.save()
            step.record['boot_id']='old-boot';step.save()
            value=detail(trace.record['id'],user)
            assert value['state']=='interrupted' and value['spans'][0]['state']=='interrupted'
            trace.record['instance_id']='another-server';trace.save()
            step.record['instance_id']='another-server';step.save()
            value=detail(trace.record['id'],user)
            assert value['state']=='running' and value['spans'][0]['state']=='running'
        finally:cm.__exit__(None,None,None)


def test_retention_and_span_bounds_are_real(monkeypatch):
    from station.observability import recorder
    user=db.create_user('retention-owner','p')['id']
    trace=obs.begin(user,'thread')
    trace.record['updated']=time.time()-31*86400;trace.save()
    newer=obs.begin(user,'thread')
    assert db.trace_get(trace.record['id'],user) is None
    monkeypatch.setattr(recorder,'MAX_SPANS',2)
    with obs.activate(newer):
        for _ in range(4):
            with obs.span('work','step'):pass
    obs.finish(newer,'success')
    value=detail(newer.record['id'],user)
    assert len(value['spans'])==2 and value['incomplete']


def test_route_attempts_capture_timeout_and_backup(monkeypatch):
    from station.core import intent
    from station.core.router import RouteDeadline
    user=db.create_user('route-owner','p')['id']
    trace=obs.begin(user,'thread')
    def identify(entry,*args):
        if entry['id']=='first':raise RouteDeadline('timeout-secret')
        return {'skill':'','conf':1.0}
    monkeypatch.setattr(intent,'_chat',identify)
    with obs.activate(trace),obs.span('route','route'):
        answer,info=intent.identify('你好',[],[{'id':'first','model':'a'},{'id':'backup','model':'b'}],
              threshold=.8,timeout=1,with_deadline=lambda fn,t:fn(),deadline_error=RouteDeadline)
    value=detail(trace.record['id'],user)
    attempts=[s for s in value['spans'] if s['kind']=='route_attempt']
    assert [s['state'] for s in attempts]==['error','success'] and answer['skill']==''
    assert 'timeout-secret' not in json.dumps(value)


def test_sse_close_finalizes_request_without_context_leak(monkeypatch):
    import asyncio
    from starlette.requests import Request
    user=db.create_user('disconnect-owner','p')['id']
    monkeypatch.setattr(server,'require_user',lambda request:{'id':user})
    async def check():
        request=Request({'type':'http','headers':[]})
        response=await server.chat(server.ChatIn(message='你好'),request)
        first=await anext(response.body_iterator)
        meta=events(SimpleNamespace(status_code=200,text=first))[0]
        await response.body_iterator.aclose()
        return detail(meta['trace_id'],user)
    value=asyncio.run(check())
    assert value['state']=='interrupted'
    assert value['spans'][0]['state']=='interrupted'
    assert obs.current() is None


def test_duplicate_resume_does_not_overwrite_winner_or_lose_duration():
    user=db.create_user('duplicate-owner','p')['id']
    a=obs.begin(user,'thread',key='attachment:duplicate')
    b=obs.begin(user,'thread',key='attachment:duplicate')
    obs.finish(a,'success')
    duration=db.trace_get(a.record['id'],user)['active_ms']
    # 后到请求的旧快照保存/收尾不许覆盖已结束请求的成功状态和累计时长。
    b.save()
    obs.finish(b,'skipped')
    value=detail(a.record['id'],user)
    assert value['state']=='success' and value['active_ms']>=duration
