"""DashScope 异步文件转写协议。

当前用于 Qwen-Audio-3.x ASR Filetrans。百炼同接口下的其它模型不一定复用同一套
提交/响应字段，接入前必须按官方文档逐项核对，不能只凭“都在百炼”就切过来。

它不是 OpenAI 兼容适配器：百炼的 ``/compatible-mode/v1`` 只覆盖
Qwen3-ASR-Flash 等同步模型，Filetrans 必须走：
  POST {服务根}/api/v1/services/audio/asr/transcription
  GET  {服务根}/api/v1/tasks/{task_id}

因此这里会先把前端配置的 ``.../compatible-mode/v1`` 还原成服务根，再调用异步接口。
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

import requests


SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
TASK_PATH = "/api/v1/tasks/{task_id}"
SUCCESS_STATES = {"SUCCEEDED", "SUCCESS", "DONE", "COMPLETED"}
FAILED_STATES = {"FAILED", "ERROR", "CANCELLED", "CANCELED"}


class DashScopeAsrError(RuntimeError):
    """百炼 ASR 的统一异常；消息直接给人看。"""

    def __init__(self, message: str, *, status: int = 0,
                 retry_after: float = 0.0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.code = code


def _notify(cb=None, percent: int = 0, message: str = "", **extra) -> None:
    """进度回调只用于显示，坏掉也不许影响转写本身。"""
    if cb is None:
        return
    try:
        cb({"percent": percent, "message": message, **extra})
    except Exception:                    # noqa
        pass


def _origin(base_url: str) -> str:
    """把 provider 的兼容地址还原成百炼服务根。

    支持两种常见写法：
      https://dashscope.aliyuncs.com/compatible-mode/v1
      https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1

    也接受已经写成服务根或完整 ASR 地址的输入。
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        raise DashScopeAsrError("未填写百炼接口地址")
    p = urlsplit(raw)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise DashScopeAsrError("百炼接口地址必须以 http:// 或 https:// 开头")

    path = p.path.rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-mode"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    at = path.find("/api/v1")
    if at >= 0:
        path = path[:at]
    return urlunsplit((p.scheme, p.netloc, path.rstrip("/"), "", ""))


def _headers(key: str, *, async_submit: bool = False) -> dict:
    key = (key or "").strip()
    if not key:
        raise DashScopeAsrError("未配置百炼 API Key")
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if async_submit:
        h["X-DashScope-Async"] = "enable"
    return h


def _retry_after(resp) -> float:
    try:
        return float((resp.headers.get("Retry-After") or "").strip())
    except (TypeError, ValueError):
        return 0.0


def _detail(resp) -> tuple[str, str]:
    """从百炼错误 JSON 里只取 code/message，避免把整页 HTML 塞给用户。"""
    try:
        d = resp.json() or {}
    except ValueError:
        return "", ""
    out = d.get("output") if isinstance(d.get("output"), dict) else {}
    code = str(d.get("code") or out.get("code") or "").strip()
    msg = str(d.get("message") or out.get("message") or "").strip()
    return code, msg


def _fail(action: str, resp) -> DashScopeAsrError:
    code, msg = _detail(resp)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 401:
        human = "百炼 API Key 不对或无权访问该模型"
    elif status == 403:
        human = "百炼拒绝访问，请检查 API Key 权限和模型开通状态"
    elif status == 404:
        human = "接口地址或任务 ID 不存在"
    elif status == 429:
        human = "百炼接口被限流，请稍后重试"
    elif status >= 500:
        human = "百炼服务暂时不可用"
    else:
        human = f"百炼拒绝了{action}请求"
    if msg:
        human += f"：{msg[:240]}"
    elif code:
        human += f"（{code}）"
    return DashScopeAsrError(human, status=status,
                             retry_after=_retry_after(resp), code=code)


def _json(resp, action: str) -> dict:
    try:
        d = resp.json()
    except ValueError as e:
        raise DashScopeAsrError(f"{action}返回的不是 JSON，可能是网关错误页") from e
    if not isinstance(d, dict):
        raise DashScopeAsrError(f"{action}返回结构不正确")
    return d


def submit_task(base_url: str, key: str, model: str, audio_url: str, *,
                language_hints: list[str] | None = None,
                diarization_enabled: bool = True,
                channel_id: int = 0,
                timeout: float = 30.0) -> str:
    """提交一条异步转写任务，返回 task_id。

    ★ 提交请求**不重试网络超时**：请求可能已经到达百炼并建好了任务，重发会产生
      第二条任务、重复计费。这与视频建任务的处理口径一致。
    """
    model = (model or "").strip()
    audio_url = (audio_url or "").strip()
    if not model:
        raise DashScopeAsrError("未填写语音识别模型名")
    if not audio_url.lower().startswith(("http://", "https://")):
        raise DashScopeAsrError("音频地址必须是百炼可访问的 http/https URL")

    root = _origin(base_url)
    params: dict = {
        "channel_id": [int(channel_id)],
        "diarization_enabled": bool(diarization_enabled),
    }
    if language_hints:
        params["language_hints"] = list(language_hints)
    body = {
        "model": model,
        "input": {"file_urls": [audio_url]},
        "parameters": params,
    }
    try:
        resp = requests.post(
            f"{root}{SUBMIT_PATH}",
            headers=_headers(key, async_submit=True),
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise DashScopeAsrError(
            f"连接百炼失败（{type(e).__name__}）；为避免重复建任务，未自动重发") from e
    if resp.status_code >= 400:
        raise _fail("提交转写任务", resp)
    d = _json(resp, "提交转写任务")
    out = d.get("output") or {}
    task_id = str(out.get("task_id") or d.get("task_id") or "").strip()
    if not task_id:
        raise DashScopeAsrError("百炼没有返回 task_id，无法查询转写结果")
    return task_id


def query_task(base_url: str, key: str, task_id: str,
               *, timeout: float = 30.0) -> dict:
    """查询一条异步转写任务。"""
    if not (task_id or "").strip():
        raise DashScopeAsrError("缺少 task_id")
    root = _origin(base_url)
    try:
        resp = requests.get(
            f"{root}{TASK_PATH.format(task_id=task_id)}",
            headers=_headers(key),
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise DashScopeAsrError(f"查询百炼任务失败（{type(e).__name__}）") from e
    if resp.status_code >= 400:
        raise _fail("查询转写任务", resp)
    return _json(resp, "查询转写任务")


def _output(data: dict) -> dict:
    out = data.get("output")
    return out if isinstance(out, dict) else data


def task_status(data: dict) -> str:
    out = _output(data)
    return str(out.get("task_status") or out.get("status") or "").upper()


def task_error(data: dict) -> str:
    out = _output(data)
    code = str(out.get("code") or "").strip()
    msg = str(out.get("message") or "").strip()
    return f"{code}: {msg}" if code and msg else (msg or code or "百炼未说明失败原因")


def result_url(data: dict) -> str:
    """从任务响应里取 transcription_url，兼容 Qwen-Audio 与 Qwen3 两种层级。"""
    out = _output(data)
    candidates: list[dict] = [out, data]
    if isinstance(out.get("result"), dict):
        candidates.append(out["result"])
    for src in candidates:
        if not isinstance(src, dict):
            continue
        u = str(src.get("transcription_url") or "").strip()
        if u:
            return u
        for row in src.get("results") or []:
            if isinstance(row, dict) and row.get("transcription_url"):
                return str(row["transcription_url"]).strip()
    return ""


def wait_for_task(base_url: str, key: str, task_id: str, *,
                  poll_interval: float = 5.0,
                  poll_deadline: float = 7200.0,
                  on_progress=None) -> dict:
    """轮询到成功/失败/超时，返回最终任务响应。"""
    started = time.monotonic()
    pct = 5
    while True:
        data = query_task(base_url, key, task_id)
        status = task_status(data)
        if status in SUCCESS_STATES:
            return data
        if status in FAILED_STATES:
            raise DashScopeAsrError(f"语音识别失败：{task_error(data)}")
        elapsed = time.monotonic() - started
        if elapsed >= poll_deadline:
            raise DashScopeAsrError(
                f"语音识别超时（已等 {int(poll_deadline)} 秒，任务 {task_id}）")
        pct = min(90, max(pct, 10 + int(elapsed / max(1.0, poll_deadline) * 75)))
        _notify(on_progress, pct,
                f"语音识别中（{status or '处理中'}）", task_id=task_id)
        time.sleep(min(max(0.1, poll_interval), max(0.1, poll_deadline - elapsed)))


def download_result(url: str, *, timeout: float = 120.0) -> dict:
    """下载最终转写 JSON。transcription_url 通常是带签名的临时地址。"""
    u = (url or "").strip()
    if not u:
        raise DashScopeAsrError("任务完成，但没有 transcription_url")
    try:
        resp = requests.get(u, timeout=timeout)
    except requests.RequestException as e:
        raise DashScopeAsrError(f"下载转写结果失败（{type(e).__name__}）") from e
    if resp.status_code >= 400:
        raise DashScopeAsrError(f"下载转写结果失败：HTTP {resp.status_code}")
    return _json(resp, "下载转写结果")


def _ms_seconds(v) -> float:
    try:
        return round(float(v) / 1000.0, 3)
    except (TypeError, ValueError):
        return 0.0


def normalize_result(raw: dict, *, model: str, task_id: str = "") -> dict:
    """把百炼结果归一成 asr.Transcript；speaker_id 转成 speaker_0/speaker_1。"""
    transcripts = raw.get("transcripts") or []
    segments: list[dict] = []
    language = ""
    for tr in transcripts:
        if not isinstance(tr, dict):
            continue
        language = language or str(tr.get("language") or "")
        for s in tr.get("sentences") or []:
            if not isinstance(s, dict):
                continue
            text = str(s.get("text") or "").strip()
            if not text:
                continue
            sid = s.get("speaker_id")
            segments.append({
                "start": _ms_seconds(s.get("begin_time")),
                "end": _ms_seconds(s.get("end_time")),
                "speaker": (f"speaker_{sid}" if sid is not None else ""),
                "text": text,
            })
    full = "\n".join(str(s.get("text") or "") for s in segments).strip()
    return {
        "text": full,
        "language": language,
        "provider": "dashscope",
        "model": model,
        "task_id": task_id,
        "segments": segments,
    }


def iter_transcribe(base_url: str, key: str, model: str, audio_url: str, *,
                    language_hints: list[str] | None = None,
                    diarization_enabled: bool = True,
                    channel_id: int = 0,
                    poll_interval: float = 5.0,
                    poll_deadline: float = 7200.0):
    """生成器版完整转写：逐条产 progress，最后产一条 result。

    build_runner 直接消费这个生成器，把每次 yield 落成 job 进度；普通调用方用
    `transcribe()` 包一层拿最终结果。
    """
    yield {"type": "progress", "percent": 2, "message": "正在提交语音识别任务"}
    task_id = submit_task(
        base_url, key, model, audio_url,
        language_hints=language_hints,
        diarization_enabled=diarization_enabled,
        channel_id=channel_id,
    )
    yield {"type": "progress", "percent": 8,
           "message": f"任务已提交（{task_id}）", "task_id": task_id}

    started = time.monotonic()
    pct = 8
    data: dict = {}
    while True:
        data = query_task(base_url, key, task_id)
        status = task_status(data)
        if status in SUCCESS_STATES:
            break
        if status in FAILED_STATES:
            raise DashScopeAsrError(f"语音识别失败：{task_error(data)}")
        elapsed = time.monotonic() - started
        if elapsed >= poll_deadline:
            raise DashScopeAsrError(
                f"语音识别超时（已等 {int(poll_deadline)} 秒，任务 {task_id}）")
        pct = min(90, max(pct, 10 + int(elapsed / max(1.0, poll_deadline) * 75)))
        yield {"type": "progress", "percent": pct,
               "message": f"语音识别中（{status or '处理中'}）", "task_id": task_id}
        time.sleep(min(max(0.1, poll_interval), max(0.1, poll_deadline - elapsed)))

    url = result_url(data)
    if not url:
        raise DashScopeAsrError(
            f"任务完成但没有返回 transcription_url；响应字段：{sorted(_output(data))}")
    yield {"type": "progress", "percent": 94,
           "message": "正在下载转写结果", "task_id": task_id}
    raw = download_result(url)
    out = normalize_result(raw, model=model, task_id=task_id)
    yield {"type": "progress", "percent": 100,
           "message": "语音识别完成", "task_id": task_id}
    yield {"type": "result", "result": out}


def transcribe(base_url: str, key: str, model: str, audio_url: str, *,
               language_hints: list[str] | None = None,
               diarization_enabled: bool = True,
               channel_id: int = 0,
               poll_interval: float = 5.0,
               poll_deadline: float = 7200.0,
               on_progress=None) -> dict:
    """完整执行：提交 → 轮询 → 下载 JSON → 归一化。"""
    for ev in iter_transcribe(
            base_url, key, model, audio_url,
            language_hints=language_hints,
            diarization_enabled=diarization_enabled,
            channel_id=channel_id,
            poll_interval=poll_interval,
            poll_deadline=poll_deadline):
        if ev.get("type") == "progress":
            _notify(on_progress, int(ev.get("percent") or 0),
                    str(ev.get("message") or ""), task_id=ev.get("task_id") or "")
        elif ev.get("type") == "result":
            return ev["result"]
    raise DashScopeAsrError("语音识别没有返回结果")
