"""Native retrieval: real contracts and budgets, fake HTTP only, isolated SQLite."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from station import db, modelcfg
from station.core.context import Context
from station.search import client, service
from station.tools import web

REAL_POST = client.post


@pytest.fixture(autouse=True)
def clean_search(monkeypatch):
    monkeypatch.delenv("STATION_LEGACY_SEARCH_MCP", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client.close()
    service.clear_cache()
    def unexpected(*args, **kwargs):
        raise AssertionError("Live search is forbidden in offline tests")
    monkeypatch.setattr(client, "post", unexpected)
    yield
    client.close()
    service.clear_cache()


def configure(uid, *, key="synthetic-user-key", pid="deepseek", enabled=True):
    cfg = modelcfg.load_config(uid)
    provider = next((p for p in cfg["providers"] if p["id"] == pid), None)
    if provider is None:
        provider = {"id": pid, "label": pid, "base_url": "https://api.deepseek.com",
                    "text_model": "", "route_model": "", "vision_model": "", "key_env": ""}
        cfg["providers"].append(provider)
    provider["api_key"] = key
    cfg["web_search"] = {"enabled": enabled, "provider_id": pid}
    modelcfg.save_config(uid, cfg)


@pytest.fixture
def user():
    uid = db.create_user("search-user", "pw")["id"]
    configure(uid)
    return uid


def hit(url="https://example.com/article", **kw):
    return {"title": "资料标题", "url": url, "snippet": "短摘要", "summary": "原文摘要", **kw}


def fake_api(monkeypatch, results=None, content=None):
    calls = []
    def post(path, payload, key, timeout):
        calls.append({"path": path, "payload": payload, "key": key, "timeout": timeout})
        return {"results": results if results is not None else [hit()]} if path == "search" else {
            "title": "长文", "content": content or ("正文 -120 元。\n" * 4000)}
    monkeypatch.setattr(client, "post", post)
    return calls


def test_default_config_compatibility_and_explicit_choice(user):
    assert modelcfg.default_config()["web_search"] == {"enabled": True, "provider_id": "deepseek"}
    configure(user, enabled=False)
    old_client = modelcfg.load_config(user)
    old_client.pop("web_search")
    modelcfg.save_config(user, old_client)
    assert modelcfg.load_config(user)["web_search"]["enabled"] is False
    assert not web.available(user)


def test_search_only_source_without_model_and_deletion(user):
    configure(user, pid="search-only")
    entry, reason = modelcfg.search_entry(user)
    assert entry["id"] == "search-only" and not reason
    cfg = modelcfg.load_config(user)
    cfg["providers"] = [p for p in cfg["providers"] if p["id"] != "search-only"]
    modelcfg.save_config(user, cfg)
    assert modelcfg.load_config(user)["web_search"]["provider_id"] == ""
    assert not web.available(user)


@pytest.mark.parametrize("bad", [{"enabled":"yes","provider_id":"deepseek"},
                                 {"enabled":True,"provider_id":[]},
                                 {"enabled":True,"provider_id":{}}, None])
def test_malformed_search_config_is_validation_error(user, bad):
    cfg = modelcfg.load_config(user)
    cfg["web_search"] = bad
    with pytest.raises(ValueError):
        modelcfg.save_config(user, cfg)


def test_cached_result_does_not_bypass_disabled_credentials(monkeypatch, user):
    calls = fake_api(monkeypatch)
    ctx = Context(user_id=user)
    service.search(ctx, "cached")
    configure(user, enabled=False)
    with pytest.raises(client.SearchError) as exc:
        service.search(ctx, "cached")
    assert exc.value.code == "not_configured" and len(calls) == 1


@pytest.mark.parametrize("url", ["https://gateway.example/v1", "http://api.deepseek.com",
                                 "https://api.deepseek.com/v1/chat/completions",
                                 "https://user:pass@api.deepseek.com", "https://api.deepseek.com?secret=1"])
def test_third_party_credentials_cannot_be_sent_to_official_api(user, url):
    cfg = modelcfg.load_config(user)
    next(p for p in cfg["providers"] if p["id"] == "deepseek")["base_url"] = url
    with pytest.raises(ValueError, match="官方 DeepSeek"):
        modelcfg.save_config(user, cfg)


def test_owner_environment_fallback_is_not_shared(monkeypatch):
    owner = db.create_user("owner", "pw")["id"]
    guest = db.create_user("guest", "pw")["id"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "owner-env-key")
    assert modelcfg.search_entry(owner)[0]["api_key"] == "owner-env-key"
    assert modelcfg.search_entry(guest)[0] is None
    configure(guest, key="guest-key")
    assert modelcfg.search_entry(guest)[0]["api_key"] == "guest-key"


def test_search_payload_and_metadata(monkeypatch, user):
    calls = fake_api(monkeypatch, [hit(date="2026-10-01T00:00:00Z")])
    result = service.search(Context(user_id=user), "中文 site:example.com", 3)
    assert result["ok"] and result["results"][0]["summary"] == "原文摘要"
    assert result["results"][0]["date"] == "2026-10-01T00:00:00Z"
    assert calls[0]["payload"] == {"query": "中文 site:example.com", "count": 3, "need_content": False}
    assert calls[0]["path"] == "search" and calls[0]["timeout"] <= 10
    assert "synthetic-user-key" not in json.dumps(result)


def test_cache_is_user_and_credential_scoped(monkeypatch, user):
    guest = db.create_user("guest", "pw")["id"]
    configure(guest)
    calls = fake_api(monkeypatch)
    ctx = Context(user_id=user)
    assert not service.search(ctx, "same")["cache_hit"]
    assert service.search(ctx, "same")["cache_hit"]
    assert not service.search(Context(user_id=guest), "same")["cache_hit"]
    configure(user, key="rotated-key")
    assert not service.search(ctx, "same")["cache_hit"]
    assert len(calls) == 3 and ctx.web_budget.requests == 2
    assert "rotated-key" not in repr(list(service._cache))


def test_expired_and_capacity_limited_cache_refetches(monkeypatch, user):
    calls = fake_api(monkeypatch)
    monkeypatch.setattr(service, "CACHE_TTL", 0)
    service.search(Context(user_id=user), "expired")
    assert not service.search(Context(user_id=user), "expired")["cache_hit"]
    assert len(calls) == 2
    monkeypatch.setattr(service, "CACHE_TTL", 300)
    monkeypatch.setattr(service, "CACHE_BYTES", 1)
    service.search(Context(user_id=user), "too large")
    assert service._cache_size == 0


def test_fetch_pagination_reuses_one_upstream_request(monkeypatch, user):
    text = "中文 -120 元。\n" * 3000
    calls = fake_api(monkeypatch, content=text)
    ctx = Context(user_id=user)
    a = service.fetch(ctx, "https://example.com/a", max_chars=4000)
    b = service.fetch(ctx, "https://example.com/a", offset=a["next_offset"], max_chars=4000)
    assert a["content"] + b["content"] == text[:8000]
    assert b["cache_hit"] and len(calls) == 1
    assert ctx.web_budget.characters == 8000
    assert a["url"] == "https://example.com/a" and a["truncated"]


def test_escaped_output_and_turn_text_budget(monkeypatch, user):
    fake_api(monkeypatch, content='"\\\n中' * 20000)
    ctx = Context(user_id=user)
    offset = 0
    for _ in range(8):
        if ctx.web_budget and ctx.web_budget.characters == 30000:
            break
        result = service.fetch(ctx, "https://example.com/long", offset=offset, max_chars=10000)
        assert len(json.dumps(result, ensure_ascii=False)) <= 12000
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert ctx.web_budget.characters == 30000
    with pytest.raises(client.SearchError, match="正文预算"):
        service.fetch(ctx, "https://example.com/long", offset=offset)


def test_eight_attempt_limit_includes_retry(monkeypatch, user):
    calls = []
    def limited(path, payload, key, timeout):
        calls.append(path)
        if len(calls) == 1:
            raise client.SearchError("rate_limited", "limited", retryable=True)
        return {"results": [hit()]}
    monkeypatch.setattr(client, "post", limited)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    ctx = Context(user_id=user)
    for i in range(7):
        service.search(ctx, "query" + str(i))
    assert ctx.web_budget.requests == 8 and len(calls) == 8
    with pytest.raises(client.SearchError, match="预算"):
        service.search(ctx, "one more")


def test_turn_time_budget_also_applies_to_other_tool(monkeypatch, user):
    calls = fake_api(monkeypatch)
    ctx = Context(user_id=user)
    ctx.web_budget = service.Budget(seconds=30)
    with pytest.raises(client.SearchError, match="预算"):
        service.fetch(ctx, "https://example.com/a")
    assert not calls


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/a", "http://127.0.0.1",
                                 "http://[::1]", "http://169.254.169.254/", "http://100.100.100.200",
                                 "https://user:pass@example.com", "http://2130706433", "https://a.local/x"])
def test_fetch_rejects_non_public_targets(monkeypatch, user, url):
    calls = fake_api(monkeypatch)
    result = json.loads(web.fetch(Context(user_id=user), url))
    assert not result["ok"] and result["error"]["code"] == "invalid_url" and not calls


def test_empty_result_is_distinct_from_failure(monkeypatch, user):
    fake_api(monkeypatch, results=[])
    empty = json.loads(web.search(Context(user_id=user), "empty"))
    assert empty["ok"] and empty["results"] == []
    def fail(*_a, **_kw):
        raise client.SearchError("invalid_key", "密钥无效")
    monkeypatch.setattr(client, "post", fail)
    failure = json.loads(web.search(Context(user_id=user), "another"))
    assert not failure["ok"] and failure["error"]["code"] == "invalid_key"


def test_no_default_search_mcp_but_user_servers_remain(user):
    from station.mcp import config as mcfg
    from station.mcp import bridge
    assert mcfg.builtin_specs() == []
    mcfg.save_servers(user, [{"id": "custom", "label": "Custom", "transport": "http", "url": "https://example.com/mcp"}])
    mcfg.set_cache(user, "custom", [{"name": "lookup", "inputSchema": {"type": "object"}}])
    assert [t.name for t in bridge.mcp_tools(user)] == ["mcp.custom.lookup"]


def test_tools_available_to_all_skills_only_when_configured(user):
    from station.core.agent import tools_for
    generic = SimpleNamespace(tools=[])
    assert {t.name for t in tools_for(generic, user)} >= {"web.search", "web.fetch"}
    configure(user, enabled=False)
    assert not any(t.owner == "web" for t in tools_for(generic, user))
    assert "不能宣称" in web.system_note(user)


def _transport(monkeypatch, handler):
    factory = httpx.AsyncClient
    created = []
    def make(**kw):
        value = factory(transport=httpx.MockTransport(handler), **kw)
        created.append(value)
        return value
    monkeypatch.setattr(httpx, "AsyncClient", make)
    return created


def test_http_pool_reused_without_cross_user_auth(monkeypatch):
    seen = []
    def handle(request):
        seen.append((str(request.url), request.headers["Authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"results": []})
    created = _transport(monkeypatch, handle)
    for key in ("user-a-key", "user-b-key"):
        assert REAL_POST("search", {"query": "hello"}, key, 2) == {"results": []}
    assert [v[1] for v in seen] == ["Bearer user-a-key", "Bearer user-b-key"]
    assert all(v[0] == "https://api.deepseek.com/v1/tools/search" for v in seen)
    assert len(created) == 1


@pytest.mark.parametrize("status,code,retryable", [(401,"invalid_key",False),(402,"insufficient_balance",False),
    (404,"page_unavailable",False),(429,"rate_limited",True),(500,"service_error",True),(302,"service_error",False)])
def test_http_errors_are_safe_and_typed(monkeypatch, status, code, retryable):
    def handle(request):
        return httpx.Response(status, text="SECRET private upstream body", headers={"Location":"https://evil.example/"})
    _transport(monkeypatch, handle)
    with pytest.raises(client.SearchError) as error:
        REAL_POST("fetch", {"url":"https://example.com"}, "secret-key", 1)
    assert error.value.code == code and error.value.retryable is retryable
    assert "SECRET" not in str(error.value) and "secret-key" not in str(error.value)


def test_http_timeout_cancels_response(monkeypatch):
    closed = []
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(2)
            yield b"{}"
        async def aclose(self):
            closed.append(True)
    _transport(monkeypatch, lambda request: httpx.Response(200, stream=Slow()))
    tick = time.monotonic()
    with pytest.raises(client.SearchError) as error:
        REAL_POST("fetch", {}, "key", .05)
    assert error.value.code == "timeout" and time.monotonic()-tick < .5
    assert closed


def test_search_probe_and_config_api_are_user_scoped(monkeypatch, user):
    from fastapi.testclient import TestClient
    from station.app import server
    calls = fake_api(monkeypatch)
    monkeypatch.setattr(server, "_test_allowed", lambda uid: True)
    c = TestClient(server.app)
    assert c.post("/api/websearch/test").status_code == 401
    monkeypatch.setattr(server, "require_user", lambda request: {"id":user})
    cfg = c.get("/api/modelconfig").json()
    assert cfg["web_search_status"]["available"]
    assert c.post("/api/websearch/test").json()["ok"]
    assert calls[0]["payload"]["count"] == 1
    payload = cfg["config"]
    payload["web_search"]["enabled"] = False
    assert c.put("/api/modelconfig", json=payload).json()["web_search_status"]["available"] is False
    assert not c.post("/api/websearch/test").json()["ok"]
    assert len(calls) == 1


def test_agent_can_search_and_fetch_without_mcp_or_station_cap(monkeypatch, user):
    from station.core.agent import run_agent
    from station.core.session import Thread
    fake_api(monkeypatch)
    thread = Thread()
    thread.user_id = user
    thread.add({"role":"user","content":"查阅资料后回答"})
    steps = []
    class Model:
        def stream(self, messages, tools=None):
            yield {"final": self.respond(messages, tools)}
        def respond(self, messages, tools=None):
            index=len(steps);steps.append(messages)
            if index==0:
                return {"content":"","tool_calls":[{"id":"search","name":"web.search","arguments":{"query":"hello"}}]}
            if index<5:
                return {"content":"","tool_calls":[{"id":str(index),"name":"web.fetch","arguments":{"url":"https://example.com/a","offset":(index-1)*1000,"max_chars":1000}}]}
            return {"content":"资料来源：https://example.com/a","tool_calls":[]}
    ctx = Context(user_id=user)
    events=list(run_agent(ctx,thread,SimpleNamespace(tools=[],system="",id="station"),Model()))
    assert len(steps)==6 and ctx.web_budget.requests==2, events
    assert ctx.web_budget.characters==4000
    assert all('已经调用过' not in str(m.get('content','')) for m in thread.msgs if m['role']=='tool')
    assert "外部资料" in steps[0][0]["content"]
