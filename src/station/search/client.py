"""Loop-bound pooled HTTP transport for the two official retrieval endpoints."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading

BASE_URL = "https://api.deepseek.com/v1/tools"
MAX_BYTES = 2 * 1024 * 1024
_lock = threading.Lock()
_loop = None
_thread = None
_http = None


class SearchError(Exception):
    def __init__(self, code: str, message: str, *, retryable=False, retry_after=0.0):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.retry_after = retryable, retry_after


def _ensure_loop():
    global _loop, _thread
    with _lock:
        if _loop is None or not _loop.is_running():
            _loop = asyncio.new_event_loop()
            _thread = threading.Thread(target=_loop.run_forever, name="web-http", daemon=True)
            _thread.start()
        return _loop


async def _request(path, payload, key, timeout):
    import httpx
    global _http

    async def request():
        global _http
        if _http is None:
            _http = httpx.AsyncClient(follow_redirects=False,
                                     limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))
        async with _http.stream("POST", BASE_URL + "/" + path, json=payload,
                                headers={"Authorization": "Bearer " + key}, timeout=timeout) as response:
            status = response.status_code
            if status != 200:
                messages = {400: ("invalid_request", "检索请求参数不合法"),
                            401: ("invalid_key", "DeepSeek 密钥无效，请核对模型配置"),
                            402: ("insufficient_balance", "DeepSeek 余额不足"),
                            403: ("access_denied", "检索请求被服务端访问策略拒绝"),
                            404: (("page_unavailable", "网页不可用或没有可提取的正文") if path == "fetch"
                                  else ("endpoint_unavailable", "搜索接口不可用，请检查服务状态")),
                            429: ("rate_limited", "DeepSeek 检索接口限流，请稍后再试")}
                code, message = messages.get(status, ("service_error", "检索服务暂时不可用"))
                try:
                    delay = max(0.0, min(float(response.headers.get("Retry-After", "0.3")), 30.0))
                except ValueError:
                    delay = 0.3
                raise SearchError(code, message, retryable=status in (408, 429) or status >= 500,
                                  retry_after=delay)
            data = bytearray()
            async for part in response.aiter_bytes(chunk_size=65536):
                data.extend(part)
                if len(data) > MAX_BYTES:
                    raise SearchError("response_too_large", "网页响应过大，请换一个页面")
            try:
                body = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise SearchError("invalid_response", "检索服务返回了无效数据") from exc
            if not isinstance(body, dict):
                raise SearchError("invalid_response", "检索服务返回了无效数据")
            return body

    return await asyncio.wait_for(request(), timeout)


def post(path: str, payload: dict, key: str, timeout: float) -> dict:
    if path not in ("search", "fetch") or not key:
        raise SearchError("not_configured", "网页检索未配置")
    if timeout <= 0:
        raise SearchError("timeout", "网页检索时间预算已用完")
    future = asyncio.run_coroutine_threadsafe(_request(path, payload, key, timeout), _ensure_loop())
    try:
        return future.result(timeout + 0.1)
    except SearchError:
        raise
    except (TimeoutError, concurrent.futures.TimeoutError):
        future.cancel()
        raise SearchError("timeout", "网页检索超时", retryable=True) from None
    except Exception:
        # Do not return exception messages: URLs, request headers and keys may be included.
        raise SearchError("network_error", "无法连接 DeepSeek 检索服务", retryable=True) from None


def close():
    """Close pooled sockets on application shutdown; a later request can reopen them."""
    global _http
    async def cleanup():
        global _http
        if _http is not None:
            await _http.aclose()
            _http = None
    if _loop is not None and _loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(cleanup(), _loop).result(2)
        except Exception:
            pass
