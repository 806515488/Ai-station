"""归属校验后的执行详情；历史缺失不补造，进程重启留下的步骤标记中断。"""
import time
from station import db
from .recorder import BOOT_ID, instance_id


def detail(trace_id, user_id):
    record = db.trace_get(trace_id, user_id)
    if not record:
        return None
    spans = db.trace_spans(trace_id, user_id)
    instance = instance_id()
    stale = bool(instance) and record.get('instance_id') == instance and record.get('boot_id') != BOOT_ID
    for step in spans:
        old_step = bool(instance) and step.get('instance_id') == instance and step.get('boot_id') != BOOT_ID
        if step['state'] == 'running' and old_step:
            step['state'] = 'interrupted'
        step.pop('boot_id', None)
        step.pop('instance_id', None)
    if stale and record['state'] == 'running':
        record['state'] = 'interrupted'
    if any(s['kind'] == 'request' and s['state'] == 'running' for s in spans):
        record['state'] = 'running'
    record.pop('boot_id', None)
    record.pop('instance_id', None)
    end = time.time() if record['state'] == 'running' else record['updated']
    record['elapsed_ms'] = round(max(0, end-record['created'])*1000, 2)
    running = [s for s in spans if s['kind'] == 'request' and s['state'] == 'running']
    if running:
        record['active_ms'] += max(0, time.time()-running[-1]['created'])*1000
    record['spans'] = spans
    record['has_errors'] = any(s['state'] == 'error' for s in spans)
    record['related'] = db.trace_children(trace_id, user_id)
    for child in record['related']:
        if child.get('instance_id') == instance and child.get('boot_id') != BOOT_ID and child['state'] == 'running':
            child['state'] = 'interrupted'
        child.pop('boot_id', None)
        child.pop('instance_id', None)
    return record
