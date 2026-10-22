"""Per-user retrieval, bounded cache and per-turn network/text budgets."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import threading
import time
from urllib.parse import urlsplit

from station.models import config as modelcfg
from station.search import client

CACHE_TTL = 300
CACHE_BYTES = 32 * 1024 * 1024
MAX_RESULT = 12000
_cache = OrderedDict()
_cache_size = 0
_cache_lock = threading.Lock()


@dataclass
class Budget:
    requests: int = 0
    seconds: float = 0
    characters: int = 0
    lock: object = field(default_factory=threading.RLock, repr=False)


def budget_for(ctx):
    value = getattr(ctx, "web_budget", None)
    if not isinstance(value, Budget):
        value = Budget()
        ctx.web_budget = value
    return value


def public_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value):
        raise client.SearchError("invalid_url", "请提供有效的公网 HTTP/HTTPS 网页地址")
    try:
        u = urlsplit(value)
        host = (u.hostname or "").lower().rstrip(".")
        if (u.scheme not in ("http", "https") or not host or u.username is not None or u.password is not None
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
                or host in ("metadata.google.internal", "100.100.100.200") or u.port == 0):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError
        if address is None and (host.isdecimal() or "." not in host):
            raise ValueError
    except ValueError:
        raise client.SearchError("invalid_url", "请提供有效的公网 HTTP/HTTPS 网页地址") from None
    return value


def _cache_key(uid, entry, path, payload):
    # Key rotation/provider changes invalidate cache; no secret is stored in clear text.
    credential = hashlib.sha256(entry["api_key"].encode()).digest()
    return (uid, entry["id"], credential, path, json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _get(key):
    global _cache_size
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        expires, encoded = item
        if expires <= time.monotonic():
            _cache_size -= len(encoded)
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return json.loads(encoded)


def _put(key, value):
    global _cache_size
    encoded = json.dumps(value, ensure_ascii=False).encode()
    if len(encoded) > client.MAX_BYTES:
        return
    with _cache_lock:
        previous = _cache.pop(key, None)
        if previous:
            _cache_size -= len(previous[1])
        _cache[key] = (time.monotonic() + CACHE_TTL, encoded)
        _cache_size += len(encoded)
        while len(_cache) > 256 or _cache_size > CACHE_BYTES:
            _, (_, removed) = _cache.popitem(last=False)
            _cache_size -= len(removed)


def clear_cache():
    global _cache_size
    with _cache_lock:
        _cache.clear()
        _cache_size = 0


def _retrieve(ctx, entry, path, payload, timeout):
    uid = getattr(ctx, "user_id", "")
    key = _cache_key(uid, entry, path, payload)
    found = _get(key)
    if found is not None:
        return found, True
    budget = budget_for(ctx)
    deadline = time.monotonic() + min(timeout, max(0, 30 - budget.seconds))
    for attempt in range(2):
        left = min(deadline-time.monotonic(), 30-budget.seconds)
        if budget.requests >= 8 or left <= 0:
            raise client.SearchError("budget_exhausted", "本轮网页检索预算已用完，请根据已有资料回答")
        budget.requests += 1
        tick = time.monotonic()
        error = None
        try:
            body = client.post(path, payload, entry["api_key"], left)
        except client.SearchError as exc:
            error = exc
        finally:
            budget.seconds += time.monotonic()-tick
        if error is None:
            _validate(path, body)
            body = {**body, "fetched_at": datetime.now(timezone.utc).isoformat()}
            _put(key, body)
            return body, False
        wait = max(0.1, error.retry_after)
        if not error.retryable or attempt or budget.requests >= 8 or wait >= min(deadline-time.monotonic(), 30-budget.seconds):
            raise error
        time.sleep(wait)
        budget.seconds += wait
    raise error


def _validate(path, body):
    if path == "search":
        results = body.get("results")
        if not isinstance(results, list) or len(results) > 50:
            raise client.SearchError("invalid_response", "搜索结果格式不正确")
        for result in results:
            if not isinstance(result, dict) or not all(isinstance(result.get(k), str) for k in ("title", "url", "snippet", "summary")):
                raise client.SearchError("invalid_response", "搜索结果格式不正确")
    elif not isinstance(body.get("title"), str) or not isinstance(body.get("content"), str) or not body["content"].strip():
        raise client.SearchError("page_unavailable", "网页没有可提取的正文")


def _entry(ctx):
    entry, reason = modelcfg.search_entry(getattr(ctx, "user_id", ""))
    if entry is None:
        raise client.SearchError("not_configured", reason)
    return entry


def search(ctx, query, count=5):
    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise client.SearchError("invalid_query", "搜索词需为 1 到 128 个字符，请重新组织关键词")
    if type(count) is not int or not 1 <= count <= 10:
        raise client.SearchError("invalid_count", "搜索条数需为 1 到 10 的整数")
    entry = _entry(ctx)
    with budget_for(ctx).lock:
        body, cached = _retrieve(ctx, entry, "search", {"query": query, "count": count, "need_content": False}, 10)
        results = []
        for item in body["results"][:count]:
            try:
                url = public_url(item["url"])
            except client.SearchError:
                continue
            result = {"title": item["title"][:250], "url": url,
                      "summary": (item["summary"] or item["snippet"])[:800],
                      "date": item.get("date") if isinstance(item.get("date"), str) else None}
            candidate = {"ok": True, "results": results+[result], "provider": "deepseek", "cache_hit": cached, "fetched_at": body["fetched_at"]}
            if len(json.dumps(candidate, ensure_ascii=False)) > MAX_RESULT - 100:
                break
            results.append(result)
        if body["results"] and not results:
            raise client.SearchError("invalid_response", "搜索结果没有可用的公开网页来源")
        return {"ok": True, "provider": "deepseek", "results": results, "cache_hit": cached,
                "fetched_at": body["fetched_at"], "truncated": len(results) < len(body["results"])}


def fetch(ctx, url, offset=0, max_chars=8000):
    public_url(url)
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 10000:
        raise client.SearchError("invalid_range", "offset 需为非负整数，max_chars 需为 1 到 10000 的整数")
    entry = _entry(ctx)
    budget = budget_for(ctx)
    with budget.lock:
        if budget.characters >= 30000:
            raise client.SearchError("text_budget_exhausted", "本轮网页正文预算已用完，请根据已有资料回答")
        body, cached = _retrieve(ctx, entry, "fetch", {"url": url}, 15)
        text = body["content"]
        if offset >= len(text):
            raise client.SearchError("invalid_range", "offset 已超出正文范围")
        end = min(len(text), offset+max_chars, offset+30000-budget.characters)
        out = {"ok": True, "provider": "deepseek", "url": url, "title": body["title"][:250],
               "content": text[offset:end], "offset": offset, "truncated": end < len(text),
               "next_offset": end if end < len(text) else None, "cache_hit": cached, "fetched_at": body["fetched_at"]}
        # JSON escaping and URL/title metadata count toward the complete result bound.
        while len(json.dumps(out, ensure_ascii=False)) > MAX_RESULT:
            if end <= offset:
                raise client.SearchError("response_too_large", "网页来源信息过长，无法返回有效正文")
            end = max(offset, end-256)
            out.update(content=text[offset:end], truncated=end < len(text), next_offset=end)
        if end <= offset:
            raise client.SearchError("response_too_large", "网页来源信息过长，无法返回有效正文")
        budget.characters += end-offset
        return out
