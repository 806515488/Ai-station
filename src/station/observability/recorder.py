"""追踪上下文与步骤记录；写入失败只标记缺失，不能让追踪异常中止业务。

ContextVar 相当于“本次执行的工牌”。生成器跨线程取 next 时必须使用
isolated_stream，后台任务则显式 activate，防止不同用户的追踪串在一起。
"""
from __future__ import annotations

from station.persistence.repositories import traces as trace_records


import contextvars
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager

from station import config

_active = contextvars.ContextVar('station_trace', default=None)
_step = contextvars.ContextVar('station_span', default=None)
BOOT_ID = uuid.uuid4().hex
MAX_SPANS = 400


def instance_id():
    """同一个数据卷有稳定实例号：本地/服务器共库时不能把对方误判为已重启。"""
    path = config.DATA_DIR / 'trace-instance.id'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x', encoding='ascii') as stream:
                stream.write(uuid.uuid4().hex)
        except FileExistsError:
            pass
        return path.read_text(encoding='ascii').strip()
    except OSError:
        return ''  # 无法取得稳定身份时宁可不推断中断。


def fingerprint(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def input_info(value):
    """参数仅留字段名、长度和摘要；不把 query、URL 或任意工具值复制进追踪库。"""
    digest, chars = hashlib.sha256(), 0
    for chunk in json.JSONEncoder(ensure_ascii=False, default=str).iterencode(value):
        chars += len(chunk)
        digest.update(chunk.encode('utf-8'))
    return {'input_chars': chars, 'input_hash': digest.hexdigest(),
            'argument_keys': [str(k)[:60] for k in list(value)[:40]] if isinstance(value, dict) else []}


def model_info(entry):
    # 显式挑字段，绝不能把含 api_key/base_url 的整个 entry 交给日志。
    return {**{k: entry.get(k) for k in ('id', 'model', 'slot', 'route_protocol', 'reasoning_effort')},
            'endpoint_hash': fingerprint(entry.get('base_url', ''))}


def _safe(value, key='', depth=0):
    """元数据上限 + 凭据键剔除；调用点还须使用白名单，不能传异常正文。"""
    if any(s in key.lower() for s in ('api_key', 'authorization', 'password', 'secret', 'headers', 'cookie')):
        return '[redacted]'
    if depth > 4:
        return '[bounded]'
    if isinstance(value, dict):
        return {str(k)[:80]: _safe(v, str(k), depth+1) for k, v in list(value.items())[:80]}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth=depth+1) for v in value[:80]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


class Trace:
    def __init__(self, record):
        self.record = record
        self.started = time.monotonic()
        self.request_id = uuid.uuid4().hex
        self.count = 0
        self.incomplete = bool(record.get('incomplete'))
        self.first = False

    def save(self):
        try:
            self.record['incomplete'] = self.incomplete
            self.record = trace_records.trace_save(self.record)
        except Exception:
            self.incomplete = True
            logging.getLogger(__name__).warning('trace write unavailable', exc_info=False)


def current():
    return _active.get()


def begin(user_id, thread_id, *, key='', kind='chat', origin='', attributes=None):
    """同一附件轮次通过 key 找回 trace；一次恢复仍创建独立 request 步骤。"""
    if not user_id or os.environ.get('STATION_TRACE', '1') == '0':
        return None
    try:
        now = time.time()
        value = {'id': uuid.uuid4().hex, 'user_id': user_id, 'thread_id': thread_id,
                 'kind': kind, 'origin_trace_id': origin, 'state': 'running',
                 'created': now, 'updated': now, 'boot_id': BOOT_ID,
                 'attributes': _safe(attributes or {}), 'active_ms': 0,
                 'first_text_ms': None, 'incomplete': False}
        value['instance_id'] = instance_id()
        record = trace_records.trace_create(value, key)
        trace = Trace(record)
        trace.record.update(boot_id=BOOT_ID, instance_id=value['instance_id'])
        trace.save()
        return trace
    except Exception:
        logging.getLogger(__name__).warning('trace unavailable', exc_info=False)
        return None


@contextmanager
def activate(trace):
    token = _active.set(trace)
    parent = _step.set(None)
    try:
        yield trace
    finally:
        _step.reset(parent)
        _active.reset(token)


def bind(trace):
    """仅在 isolated_stream 的私有上下文内使用，不能污染 HTTP worker。"""
    _active.set(trace)
    _step.set(None)


def finish(trace, state):
    if not trace:
        return
    try:
        record = trace_records.trace_finish(trace.record['id'], trace.record['user_id'], state,
                                 (time.monotonic()-trace.started)*1000, trace.incomplete)
        if record:
            trace.record = record
    except Exception:
        trace.incomplete = True
        logging.getLogger(__name__).warning('trace completion unavailable', exc_info=False)


def first_text():
    trace = current()
    if trace and not trace.first:
        trace.first = True
        # 跨附件等待的总墙钟时间与本次请求等待时间分开保存。
        trace.record['first_text_ms'] = round((time.monotonic()-trace.started)*1000, 2)
        trace.save()


class Step:
    def __init__(self, kind, name, attributes):
        self.trace = current()
        self.parent = _step.get()
        self.started = time.monotonic()
        self.record = {'id': uuid.uuid4().hex[:16], 'trace_id': self.trace.record['id'] if self.trace else '',
                       'parent_span_id': self.parent.record['id'] if self.parent else '',
                       'request_id': self.trace.request_id if self.trace else '',
                       'kind': kind, 'name': name[:160], 'state': 'running',
                       'boot_id': BOOT_ID, 'instance_id': self.trace.record.get('instance_id', '') if self.trace else '',
                       'created': time.time(), 'updated': time.time(), 'duration_ms': None,
                       'attributes': _safe(attributes)}
        self.closed = False

    def save(self):
        if not self.trace:
            return
        try:
            trace_records.trace_span_save(self.record)
        except Exception:
            self.trace.incomplete = True

    def update(self, **attrs):
        self.record['attributes'].update(_safe(attrs))

    def error(self, code):
        self.record['state'] = 'error'
        self.update(error_type=code)
        # 内层将失败转成文本时，上层工具也必须保留失败标记。
        parent = self.parent
        while parent:
            if parent.record['kind'] == 'tool':
                parent.record['state'] = 'error'
                parent.update(error_type=code)
                if parent.closed:
                    parent.save()
                break
            parent = parent.parent

    def end(self):
        if self.closed:
            return
        self.closed = True
        if self.record['state'] == 'running':
            self.record['state'] = 'success'
        self.record.update(updated=time.time(), duration_ms=round((time.monotonic()-self.started)*1000, 2))
        self.save()


@contextmanager
def span(kind, name, **attributes):
    trace = current()
    step = Step(kind, name, attributes)
    if trace:
        trace.count += 1
        if trace.count > MAX_SPANS:
            trace.incomplete = True
            step.trace = None
    step.save()
    token = _step.set(step)
    try:
        yield step
    except BaseException as exc:
        if isinstance(exc, GeneratorExit):
            step.record['state'] = 'interrupted'
        else:
            step.error(type(exc).__name__)
        raise
    finally:
        step.end()
        _step.reset(token)


def fail(code):
    step = _step.get()
    if step:
        step.error(code)


def isolated_stream(generator):
    """SSE 每次 next 可能换线程；固定私有 Context，保证 span 的 token 可正确退出。"""
    context = contextvars.copy_context()
    try:
        while True:
            try:
                value = context.run(next, generator)
            except StopIteration:
                return
            yield value
    finally:
        context.run(generator.close)
