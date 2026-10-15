"""Jev routing contracts, isolation, fallback and deadline tests; no live credentials."""
from __future__ import annotations

import asyncio
import json
import time
import threading
from types import SimpleNamespace

import httpx
import pytest

from station import db, modelcfg
from station.core import intent, router, systemone


CATALOG = [SimpleNamespace(id="archive", name="档案", description="整理干部档案", hints="档案整理"),
           SimpleNamespace(id="video", name="视频", description="生成视频", hints="生成一段视频")]
JEV = {"id": "jev", "model": "jev-1.13-free", "base_url": "https://opencode.ai/zen/v1",
       "api_key": "test-private-key", "route_protocol": "systemone"}
CHAT = {"id": "qwen", "model": "qwen-flash", "api_key": "test-backup-key"}


def answer(choice="skill:archive", confidence=0.95, options=None):
    options = options or ["skill:archive", "skill:video", "general", "uncertain"]
    return {"answers": {"route": {"type": "choice", "choice": choice,
            "confidence": confidence, "probabilities": {k: int(k == choice) for k in options}}}}


@pytest.fixture
def route(monkeypatch):
    monkeypatch.delenv("STATION_FAKE", raising=False)
    monkeypatch.setenv("ROUTE_TIMEOUT", "1")
    monkeypatch.setenv("ROUTE_THRESHOLD", "0.8")
    monkeypatch.setattr(modelcfg, "resolve", lambda uid, slot: [dict(JEV), dict(CHAT)])

    def run(text="请帮我归整这些干部材料"):
        diag = {}
        return router.l2_route(text, CATALOG, diag, "owner"), diag
    return run


def fake_post(monkeypatch, body):
    calls = []
    async def post(url, key, payload, timeout):
        calls.append((url, key, payload, timeout))
        return body
    monkeypatch.setattr(systemone, "_post", post)
    return calls


def chat_model(monkeypatch, replies):
    calls = []
    class Model:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            assert kwargs["retries"] == kwargs["max_retries"] == 0
        def respond(self, messages, tools):
            assert tools is None
            result = replies.pop(0)
            if isinstance(result, Exception):
                raise result
            return {"content": json.dumps(result)}
    monkeypatch.setattr("station.core.model.Model", Model)
    return calls


def test_jev_request_and_catalog_contract(monkeypatch, route):
    calls = fake_post(monkeypatch, answer())
    chats = chat_model(monkeypatch, [])
    hit, diag = route()
    assert hit.skill_id == "archive"
    url, key, payload, budget = calls[0]
    assert url == "https://opencode.ai/zen/v1/systemone"
    assert key == JEV["api_key"] and budget <= 1
    assert set(payload) == {"model", "state", "questions"}
    assert payload["model"] == "jev-1.13-free"
    assert payload["state"] == "请帮我归整这些干部材料"
    criteria = payload["questions"]["route"]["criteria"]
    assert set(criteria) == {"skill:archive", "skill:video", "general", "uncertain"}
    assert "整理干部档案" in criteria["skill:archive"]
    assert not chats
    assert diag["provider"] == "jev" and diag["protocol"] == "systemone"
    assert diag["probabilities"]["skill:archive"] == 1
    assert JEV["api_key"] not in json.dumps(diag)


@pytest.mark.parametrize("choice,confidence,reason", [
    ("uncertain", 0.99, "uncertain"), ("skill:archive", 0.3, "below_threshold")])
def test_uncertainty_tries_backup(monkeypatch, route, choice, confidence, reason):
    fake_post(monkeypatch, answer(choice, confidence))
    calls = chat_model(monkeypatch, [{"skill": "video", "confidence": 0.95}])
    hit, diag = route()
    assert hit.skill_id == "video"
    assert diag["attempts"][0]["reason"] == reason
    assert calls[0]["entries"] == [CHAT]
    assert calls[0]["total_budget"] < 1


def test_general_chat_is_final(monkeypatch, route):
    fake_post(monkeypatch, answer("general"))
    calls = chat_model(monkeypatch, [])
    hit, diag = route("你好")
    assert hit.skill_id == "" and diag["reason"] == "chat"
    assert not calls


@pytest.mark.parametrize("code", [401, 403, 404, 410, 429, 500])
def test_upstream_failure_does_not_retry_or_switch_to_paid(monkeypatch, route, code):
    models = []
    def fail(entry, *args, **kwargs):
        models.append(entry["model"])
        raise systemone.SystemOneHTTPError(code)
    monkeypatch.setattr(systemone, "evaluate", fail)
    chat_model(monkeypatch, [{"skill": "archive", "confidence": 0.9}])
    hit, diag = route()
    assert hit.skill_id == "archive" and models == ["jev-1.13-free"]
    assert diag["attempts"][0]["status_code"] == code


@pytest.mark.parametrize("broken", [
    {}, {"answers": []},
    {"answers": {"route": {"type": "choice", "choice": "invented"}}},
])
def test_invalid_jev_response_falls_back(monkeypatch, route, broken):
    fake_post(monkeypatch, broken)
    chat_model(monkeypatch, [{"skill": "video", "confidence": 0.9}])
    hit, diag = route()
    assert hit.skill_id == "video"
    assert diag["attempts"][0]["reason"] == "error:InvalidDecision"


@pytest.mark.parametrize("mutation", ["nan", "boolean", "outside", "missing", "sum", "wrong_winner"])
def test_distribution_validation(mutation):
    body = answer()
    item = body["answers"]["route"]
    if mutation == "nan": item["confidence"] = float("nan")
    if mutation == "boolean": item["confidence"] = True
    if mutation == "outside": item["confidence"] = 1.1
    if mutation == "missing": item["probabilities"].pop("general")
    if mutation == "sum": item["probabilities"]["general"] = 0.2
    if mutation == "wrong_winner": item["choice"] = "general"
    with pytest.raises(systemone.InvalidDecision):
        systemone.parse_choice(body, "route", dict.fromkeys(answer()["answers"]["route"]["probabilities"]))


def test_independent_jev_threshold(monkeypatch, route):
    monkeypatch.setenv("ROUTE_THRESHOLD", "0.99")
    fake_post(monkeypatch, answer(confidence=0.85))
    hit, diag = route()
    assert hit.skill_id == "archive" and diag["threshold"] == 0.8
    monkeypatch.setattr(modelcfg, "resolve", lambda *_: [{**JEV, "route_threshold": 0.9}, CHAT])
    chat_model(monkeypatch, [{"skill": "video", "confidence": 1}])
    hit, diag = route()
    assert hit.skill_id == "video" and diag["attempts"][0]["threshold"] == 0.9


@pytest.mark.parametrize("first", [{"skill": "bad", "confidence": 1},
                                   {"skill": "archive", "confidence": "nan"},
                                   {"skill": "archive", "confidence": 0.1}])
def test_chat_invalid_results_also_try_next_entry(monkeypatch, route, first):
    monkeypatch.setattr(modelcfg, "resolve", lambda *_: [CHAT, {**CHAT, "id": "backup"}])
    chat_model(monkeypatch, [first, {"skill": "archive", "confidence": "0.95"}])
    hit, diag = route()
    assert hit.skill_id == "archive" and len(diag["attempts"]) == 2


def test_systemone_three_second_cap_and_shared_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(intent.time, "monotonic", lambda: clock[0])
    seen = []
    def jev(entry, text, catalog, budget):
        seen.append(budget)
        clock[0] += 3.0
        raise TimeoutError()
    def chat(entry, text, catalog, threshold, budget):
        seen.append(budget)
        clock[0] += budget
        return {"skill": "archive", "conf": 1}
    monkeypatch.setattr(intent, "_systemone", jev)
    monkeypatch.setattr(intent, "_chat", chat)
    decision, diag = intent.identify("x", CATALOG, [JEV, CHAT], timeout=10, threshold=0.8,
                                    with_deadline=lambda fn, budget: fn(), deadline_error=router.RouteDeadline)
    assert seen == [3.0, 7.0]
    assert decision["skill"] == "archive"


def test_entire_chain_hard_deadline(monkeypatch, route):
    monkeypatch.setenv("ROUTE_TIMEOUT", "0.05")
    def slow(*args, **kwargs):
        time.sleep(0.2)
        return {"skill": "archive", "conf": 1}
    monkeypatch.setattr(intent, "_systemone", slow)
    calls = chat_model(monkeypatch, [])
    tick = time.monotonic()
    hit, diag = route()
    assert hit is None and time.monotonic() - tick < 0.15
    assert diag["reason"] == "timeout" and not calls


def test_http_adapter_payload_and_no_redirect(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=answer())
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    result = systemone.evaluate(JEV, "hello", dict.fromkeys(answer()["answers"]["route"]["probabilities"]), timeout=1)
    assert result["choice"] == "skill:archive"
    assert seen[0].headers["Authorization"] == "Bearer test-private-key"
    assert seen[0].url.path == "/zen/v1/systemone"
    assert set(json.loads(seen[0].content)) == {"model", "state", "questions"}


def test_http_timeout_cancels_and_closes_response(monkeypatch):
    closed = []
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b"{}"
        async def aclose(self):
            closed.append(True)
    def handler(request):
        return httpx.Response(200, stream=SlowStream())
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(TimeoutError):
        systemone.evaluate(JEV, "x", {"a": "a", "b": "b"}, timeout=0.05)
    assert closed


def test_real_local_http_timeout_closes_socket(monkeypatch):
    """Exercise the actual transport, not only a mocked cancellation callback."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    closed = threading.Event()
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "1024")
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(2)
            if self.connection.recv(1) == b"":
                closed.set()
        def log_message(self, *_args):
            pass
    # Never inherit machine proxy settings for the isolated local transport test.
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(trust_env=False, **kw))
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            entry = {**JEV, "base_url": f"http://127.0.0.1:{server.server_port}/v1"}
            with pytest.raises((TimeoutError, httpx.TimeoutException)):
                systemone.evaluate(entry, "local", {"a": "a", "b": "b"}, timeout=0.15)
            assert closed.wait(1), "Timed-out request did not close its TCP connection"
            assert received[0]["model"] == "jev-1.13-free"
        finally:
            server.shutdown()
            thread.join(1)


def test_new_skills_and_reserved_names_are_supported(monkeypatch, route):
    # This also ensures the wire names keep generic and skill choices distinct.
    catalog = [*CATALOG, SimpleNamespace(id="general", name="新增技能", description="扩展功能", hints="扩展")]
    monkeypatch.setattr(modelcfg, "resolve", lambda *_: [JEV])
    body = answer("skill:general", options=["skill:archive", "skill:video", "skill:general", "general", "uncertain"])
    fake_post(monkeypatch, body)
    diag = {}
    hit = router.l2_route("扩展功能", catalog, diag, "owner")
    assert hit.skill_id == "general"
    assert set(diag["probabilities"]) == set(body["answers"]["route"]["probabilities"])
    fake_post(monkeypatch, answer("uncertain", options=list(body["answers"]["route"]["probabilities"])))
    assert router.l2_route("继续", catalog, {}, "owner") is None


@pytest.mark.parametrize("code", [302, 401, 404, 429, 500])
def test_http_errors_do_not_leak_upstream_body(monkeypatch, code):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(code, text="secret-upstream-body", headers={"Location": "https://other.invalid"})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(systemone.SystemOneHTTPError) as error:
        systemone.evaluate(JEV, "x", {"a": "a", "b": "b"}, timeout=1)
    assert error.value.status_code == code and len(seen) == 1
    assert "secret" not in str(error.value) and "secret" not in systemone.error_hint(error.value)


def test_configuration_roundtrip_owner_keys_and_old_defaults(monkeypatch):
    owner = db.create_user("owner", "test")
    guest = db.create_user("guest", "test")
    cfg = modelcfg.default_config()
    preset = next(p for p in modelcfg.presets() if p["id"] == "opencode-jev")
    preset.update(api_key="owner-secret", route_threshold=0.87)
    cfg["providers"].append(preset)
    cfg["slots"]["route"] = [preset["id"], "qwen"]
    modelcfg.save_config(owner["id"], cfg)
    resolved = modelcfg.resolve(owner["id"], "route")
    assert resolved[0]["route_protocol"] == "systemone" and resolved[0]["route_threshold"] == 0.87
    assert resolved[1]["route_protocol"] == "chat_completions"
    assert resolved[0]["api_key"] == "owner-secret"
    assert "owner-secret" not in json.dumps(modelcfg.mask_config(modelcfg.load_config(owner["id"])))
    assert "opencode-jev" not in [e["id"] for e in modelcfg.resolve(guest["id"], "route")]
    assert db.model_config_load(guest["id"]) is None


@pytest.mark.parametrize("change", [
    {"route_protocol": "invalid"}, {"route_threshold": float("nan")},
    {"route_threshold": True}, {"route_threshold": 1.1}, {"route_threshold": "0.8"},
    {"base_url": "https://opencode.ai/zen/v1/systemone"}])
def test_invalid_protocol_config_is_rejected(change):
    cfg = modelcfg.default_config()
    preset = next(p for p in modelcfg.presets() if p["id"] == "opencode-jev")
    preset.update(change)
    cfg["providers"].append(preset)
    cfg["slots"]["route"] = [preset["id"]]
    with pytest.raises(ValueError):
        modelcfg.save_config("u", cfg)


def test_systemone_cannot_be_selected_as_chat_model():
    cfg = modelcfg.default_config()
    preset = next(p for p in modelcfg.presets() if p["id"] == "opencode-jev")
    preset["text_model"] = "jev-1.13-free"
    cfg["providers"].append(preset)
    cfg["slots"]["chat"] = [preset["id"]]
    with pytest.raises(ValueError, match="通用模型"):
        modelcfg.save_config("u", cfg)


def test_systemone_probe_uses_real_adapter(monkeypatch):
    from station.app import server
    user = db.create_user("probe-owner", "pw")
    cfg = modelcfg.default_config()
    preset = next(p for p in modelcfg.presets() if p["id"] == "opencode-jev")
    preset["api_key"] = "probe-secret"
    cfg["providers"].append(preset)
    cfg["slots"]["route"] = [preset["id"]]
    modelcfg.save_config(user["id"], cfg)
    monkeypatch.setattr(server, "require_user", lambda request: user)
    monkeypatch.setattr(server, "_test_allowed", lambda uid: True)
    body = answer("greeting", options=["greeting", "other"])
    calls = fake_post(monkeypatch, body)
    result = server.test_model_config(server.ProviderTestIn(provider_id=preset["id"]), None)
    assert result["ok"] and result["protocol"] == "systemone"
    assert calls[0][2]["model"] == "jev-1.13-free"
    fake_post(monkeypatch, {})
    result = server.test_model_config(server.ProviderTestIn(provider_id=preset["id"]), None)
    assert result["ok"] is False and result["error"] == "InvalidDecision"


def test_l1_and_active_skill_guard_remain_in_force():
    assert router.l1_route("请写周报", {"weekly-report"}).skill_id == "weekly-report"
    assert not router.may_switch_to("重新上传照片", "archive", "video")


def test_probe_validates_the_same_configuration_snapshot_it_calls(monkeypatch):
    from fastapi import HTTPException
    from station.app import server
    monkeypatch.setattr(server, "require_user", lambda request: {"id": "owner"})
    monkeypatch.setattr(server, "_test_allowed", lambda uid: True)
    monkeypatch.setattr(modelcfg, "probe_entry", lambda *_: (
        JEV["base_url"], JEV["api_key"], JEV["model"], "route", ""))
    monkeypatch.setattr(modelcfg, "resolve_one", lambda *_: [
        {**JEV, "base_url": "http://169.254.169.254/v1"}])
    with pytest.raises(HTTPException) as error:
        server.test_model_config(server.ProviderTestIn(provider_id="jev"), None)
    assert error.value.status_code == 400
