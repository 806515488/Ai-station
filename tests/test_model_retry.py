"""退避重连测试（全离线，不联网、不烧 key、不真睡）。

为什么值得单独钉这么细：退避重试的每一条规则写错，**都不会崩、也不报错**，
只是行为悄悄变了 ——
  · 该重试的没重试 → 用户看到一次偶发失败就整轮死掉（就是 09-15 那个 httpx 导入竞态）；
  · 不该重试的却重试了 → key 填错的人要干等 31 秒才看到"密钥不对"；
  · 吐过字还重试 → 用户看到两家的半句话拼在一起（本仓最老的铁律，见 test_model_fallback）。

手法与 test_model_fallback.py 一样：把 `Model._build` 换成查表函数，表里放假客户端
或要抛的异常，于是整条重试调度可以不碰网络地全测一遍。
"""
from __future__ import annotations

import threading
import time

import pytest
from langchain_core.messages import AIMessageChunk

from archive.engine import providers as archive_providers
from station.core import model as station_model
from station.core.model import Model


class FakeLLM:
    """假 ChatOpenAI：只实现链会用到的三个方法（bind_tools / invoke / stream）。

    err 可以在测试中途改掉 —— "退避期间网络自己好了"就是这么模拟的。
    """

    def __init__(self, *, content="好的", chunks=None, err=None, after=None, delay=0.0):
        self.content = content
        self.chunks = chunks
        self.err = err
        self.after = after            # 吐完前 after 片之后再抛（模拟"说到一半断了"）
        self.delay = delay
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, msgs):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.err:
            raise self.err
        return AIMessageChunk(content=self.content)

    def stream(self, msgs):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.err:
            raise self.err
        parts = self.chunks or [AIMessageChunk(content=self.content)]
        for i, ch in enumerate(parts):
            if self.after is not None and i >= self.after:
                raise ConnectionError("模拟：输出到一半连接断了")
            yield ch
        if self.after is not None and self.after >= len(parts):
            raise ConnectionError("模拟：输出到一半连接断了")


class HttpErr(Exception):
    """带状态码的假异常 —— openai 的异常就是这个形状（err.status_code）。"""

    def __init__(self, code: int):
        self.status_code = code
        super().__init__(f"HTTP {code}")


def _entry(pid: str, slot: str = "chat") -> dict:
    return {"id": pid, "label": pid, "base_url": "http://x/v1",
            "api_key": "k", "key_env": "", "model": "m-" + pid, "slot": slot}


def _arc_entry(pid: str, **kw) -> dict:
    """识别侧那条薄链的 entry（它不在 _PROVIDERS 里，字段少几个）。"""
    e = {"id": pid, "label": pid, "base_url": "http://x/v1",
         "api_key": "k", "key_env": "", "model": "m-" + pid}
    e.update(kw)
    return e


def _patch_build(monkeypatch, table: dict, built: list | None = None):
    """把 Model._build 换成查表（同 test_model_fallback.py）。"""
    def _fake_build(self, entry, timeout=None):
        if built is not None:
            built.append(entry["id"])
        v = table[entry["id"]]
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(Model, "_build", _fake_build)


@pytest.fixture
def fast_retry(monkeypatch):
    """把两处退避等待压成 0 —— 不然一条用例要真睡 31 秒。

    ★ 必须是**模块顶层常量**才 patch 得到（这个要求见 skills/video 的 CREATE_RETRY_WAITS）。
    ★ 刻意不做成 autouse：下面那条"常量别漂移"的用例要读**真值**，不能被压平。
    """
    monkeypatch.setattr(station_model, "RETRY_WAITS", (0.0,) * 5)
    monkeypatch.setattr(archive_providers, "RETRY_WAITS", (0.0,) * 5)


# ── 该重试的：瞬时故障 ────────────────────────────────────────────────

def test_transient_error_retries_the_whole_chain(monkeypatch, fast_retry):
    """★ 主用例：连不上 → 退避后整条链重跑 → 第二次成功。

    这正是用户报的那个场景（第一次调模型撞上导入竞态，之后就好了）。
    """
    llm = FakeLLM(content="重连之后答的", err=ConnectionError("网络抖了一下"))
    _patch_build(monkeypatch, {"a": llm})
    m = Model(entries=[_entry("a")])
    seen: list = []

    def on_retry(ev):
        seen.append(ev)
        llm.err = None                 # 退避期间"网络好了"

    got = m.respond([{"role": "user", "content": "hi"}], on_retry=on_retry)
    assert got["content"] == "重连之后答的"
    assert llm.calls == 2
    assert len(seen) == 1
    assert seen[0]["attempt"] == 1 and seen[0]["total"] == 5
    assert "ConnectionError" in seen[0]["error"]


def test_chain_switch_is_tried_before_any_backoff(monkeypatch, fast_retry):
    """★ 顺序要求：先换家（几百毫秒），全挂光了才退避（起步就 1 秒）。

    能靠换家解决的事，不该让用户等退避。
    """
    good = FakeLLM(content="第二家答的")
    _patch_build(monkeypatch, {"a": ConnectionError("连不上"), "b": good})
    m = Model(entries=[_entry("a"), _entry("b")])
    seen: list = []
    assert m.respond([{"role": "user", "content": "hi"}],
                     on_retry=seen.append)["content"] == "第二家答的"
    assert seen == []                  # 换家就解决了，一次退避都不该发生


def test_5xx_is_transient(monkeypatch, fast_retry):
    """对端 5xx 是"它自己的锅"，值得重试。"""
    llm = FakeLLM(content="好了", err=HttpErr(503))
    _patch_build(monkeypatch, {"a": llm})
    m = Model(entries=[_entry("a")], retries=1)
    seen: list = []

    def on_retry(ev):
        seen.append(ev)
        llm.err = None

    assert m.respond([{"role": "user", "content": "hi"}],
                     on_retry=on_retry)["content"] == "好了"
    assert len(seen) == 1


def test_real_connection_error_subclass_is_transient():
    """★ 用**真实的**异常类验一遍，别只用我自己起的假名字。

    这条是 09-15 端到端真跑才抓出来的：实际抛出来的是 langchain 包的
    `OpenAIConnectionError`，而它的**爹**才是我名单里的 `APIConnectionError`。
    第一版只比叶子类名 → **最常见的"连不上"根本不会重试**，而假异常在单测里
    永远暴露不了这点（假名字是我自己起的，正好起对了一个不存在的类）。
    所以这里直接 import 真货 —— 上游哪天改名，这条会红。
    """
    from langchain_openai.chat_models.base import OpenAIConnectionError
    from openai import APITimeoutError, AuthenticationError, RateLimitError

    err = OpenAIConnectionError.__new__(OpenAIConnectionError)   # 绕过 __init__ 的参数校验
    assert "APIConnectionError" not in type(err).__name__, "前提变了：叶子名现在就叫 APIConnectionError"
    assert station_model._is_transient(err) is True
    assert archive_providers._is_transient(err) is True

    # 带状态码的那几类走的是另一条分支，必须照旧
    for cls, code, want in ((RateLimitError, 429, False),
                            (AuthenticationError, 401, False)):
        e = cls.__new__(cls)
        e.status_code = code
        assert station_model._is_transient(e) is want
        assert archive_providers._is_transient(e) is want

    t = APITimeoutError.__new__(APITimeoutError)
    assert station_model._is_transient(t) is True


def test_import_race_text_is_transient():
    """★ 用户实际遇到的那个错：首次并发 import 撞出的 AttributeError 必须算瞬时。

    它几百毫秒后必然已经好了 —— 不重试的话，一次偶发竞态就白瞎一整轮对话。
    """
    err = AttributeError(
        "partially initialized module 'httpx' from '...\\httpx\\__init__.py' "
        "has no attribute 'URL' (most likely due to a circular import)")
    assert station_model._is_transient(err) is True
    assert archive_providers._is_transient(err) is True


# ── 不该重试的：再试一百次也一样 ──────────────────────────────────────

def test_auth_error_does_not_retry(monkeypatch, fast_retry):
    """★ 401 鉴权失败立刻报错 —— 让 key 填错的人干等 31 秒是最糟的体验。"""
    llm = FakeLLM(err=HttpErr(401))
    _patch_build(monkeypatch, {"a": llm})
    seen: list = []
    with pytest.raises(RuntimeError) as e:
        Model(entries=[_entry("a")]).respond(
            [{"role": "user", "content": "hi"}], on_retry=seen.append)
    assert "401" in str(e.value)
    assert llm.calls == 1              # 只试了一轮
    assert seen == []                  # 一次退避都没有


def test_rate_limit_429_does_not_retry(monkeypatch, fast_retry):
    """★ 429 配额不重试（本仓既定口径，先例见 skills/video 的 CREATE_RETRY_WAITS）。"""
    llm = FakeLLM(err=HttpErr(429))
    _patch_build(monkeypatch, {"a": llm})
    seen: list = []
    with pytest.raises(RuntimeError):
        Model(entries=[_entry("a")]).respond(
            [{"role": "user", "content": "hi"}], on_retry=seen.append)
    assert llm.calls == 1
    assert seen == []


def test_missing_key_does_not_retry(monkeypatch, fast_retry):
    """我们自己抛的"未配置 key"不是瞬时错 —— 重试只是把同一句话再说五遍。"""
    built: list = []
    _patch_build(monkeypatch, {"a": RuntimeError("a 未配置 key")}, built=built)
    seen: list = []
    with pytest.raises(RuntimeError):
        Model(entries=[_entry("a")]).respond(
            [{"role": "user", "content": "hi"}], on_retry=seen.append)
    assert built == ["a"]              # 只构造/尝试了一次
    assert seen == []


def test_retries_zero_disables_backoff(monkeypatch, fast_retry):
    """L2 判词用的形状：retries=0 = 一次就降级（它那 10 秒预算是"加速通道"的生命线）。"""
    llm = FakeLLM(err=ConnectionError("连不上"))
    _patch_build(monkeypatch, {"a": llm})
    with pytest.raises(RuntimeError):
        Model(entries=[_entry("a", "route")], retries=0).respond(
            [{"role": "user", "content": "hi"}])
    assert llm.calls == 1


# ── 铁律：吐过字就绝不再试 ────────────────────────────────────────────

def test_stream_never_retries_after_first_delta(monkeypatch, fast_retry):
    """★ 最老那条铁律不许被重试破坏：已经往外吐过字 → 报错，不换家、也不退避。

    否则用户看到的是"上一家的半句话 + 下一家的整句话"，是胡话。
    """
    built: list = []
    bad = FakeLLM(chunks=[AIMessageChunk(content="前半句")], after=1)
    _patch_build(monkeypatch, {"a": bad}, built=built)
    seen: list = []
    with pytest.raises(RuntimeError) as e:
        list(Model(entries=[_entry("a")]).stream(
            [{"role": "user", "content": "hi"}]))
    assert "不再切换通道" in str(e.value)
    assert built == ["a"]              # 没有再来一轮
    assert seen == []


# ── 上限与预算 ────────────────────────────────────────────────────────

def test_retries_stop_at_max(monkeypatch, fast_retry):
    """一直失败就试满上限：首发 + 5 次重试 = 6 轮，然后老实报错。"""
    llm = FakeLLM(err=ConnectionError("一直连不上"))
    _patch_build(monkeypatch, {"a": llm})
    seen: list = []
    with pytest.raises(RuntimeError):
        Model(entries=[_entry("a")]).respond(
            [{"role": "user", "content": "hi"}], on_retry=seen.append)
    assert llm.calls == 6              # 1 次首发 + 5 次重试
    assert [s["attempt"] for s in seen] == [1, 2, 3, 4, 5]
    assert [s["wait"] for s in seen] == [0.0] * 5   # 被 fast_retry 压平了


def test_total_budget_is_not_reset_by_retry_rounds(monkeypatch, fast_retry):
    """★ t0 只许取一次：每轮重新起算的话，总预算会被无限续命。

    判词槽的"整条链一共 N 秒"就是靠这个成立的（见 test_model_fallback 的
    test_total_budget_is_shared_across_chain）。这里总预算 0.05s、第一家慢 0.08s：
    第二轮**进来时**预算就该已经用光，所以只尝试了一次。
    """
    built: list = []
    _patch_build(monkeypatch, {"a": FakeLLM(err=ConnectionError("连不上"), delay=0.08)},
                 built=built)
    m = Model(entries=[_entry("a", "route")], timeout=5, total_budget=0.05)
    with pytest.raises(RuntimeError) as e:
        m.respond([{"role": "user", "content": "hi"}])
    assert "总预算" in str(e.value)     # 报的是预算耗尽，不是"所有通道都失败了"
    assert built == ["a"]              # 第二轮没有重新尝试


# ── 让前端看见 ────────────────────────────────────────────────────────

def test_stream_yields_retry_notice_before_any_delta(monkeypatch, fast_retry):
    """★ 对话侧的通知通道：退避前先 yield {"retry": …}，前端拿它显示倒计时。

    顺序很重要 —— 通知必须出现在**任何 delta 之前**（退了避才有后面的字）。
    """
    llm = FakeLLM(content="好了", err=ConnectionError("连不上"))
    _patch_build(monkeypatch, {"a": llm})
    parts: list = []
    for p in Model(entries=[_entry("a")]).stream([{"role": "user", "content": "hi"}]):
        parts.append(p)
        if "retry" in p:
            llm.err = None             # 退避期间"网络好了"
    assert "retry" in parts[0]         # 第一条就是重连通知
    assert parts[0]["retry"] == {"attempt": 1, "total": 5, "wait": 0.0}
    assert [p for p in parts if "delta" in p], "重连成功后应该有正文"
    assert [p for p in parts if "final" in p][0]["final"]["content"] == "好了"


def test_retry_callback_error_does_not_break_the_call(monkeypatch, fast_retry):
    """★ 显示回调坏掉绝不许影响调用本身：重连是为了拿到结果，不能被通知带崩。"""
    llm = FakeLLM(content="照样答出来", err=ConnectionError("连不上"))
    _patch_build(monkeypatch, {"a": llm})

    def boom(ev):
        llm.err = None
        raise RuntimeError("通知层自己炸了")

    assert Model(entries=[_entry("a")]).respond(
        [{"role": "user", "content": "hi"}], on_retry=boom)["content"] == "照样答出来"


# ── 识别侧那条薄链 ────────────────────────────────────────────────────

def test_archive_chain_retries_and_notifies(monkeypatch, fast_retry):
    """识别链同样要退避重连（它的 6 路并发正是那个导入竞态的高发地）。"""
    llm = FakeLLM(content="读出来了", err=ConnectionError("连不上"))
    seen: list = []

    def note(ev):
        seen.append(ev)
        llm.err = None

    chain = archive_providers._Chain("vision", [_arc_entry("a", _llm=llm)],
                                     on_retry=note)
    assert chain.invoke([{"role": "user", "content": "hi"}]).content == "读出来了"
    assert llm.calls == 2
    assert len(seen) == 1 and seen[0]["attempt"] == 1
    assert chain.channel == "a"        # 链标签不受重试影响（OCR 缓存键要用它）


def test_archive_chain_does_not_retry_on_missing_key(monkeypatch, fast_retry):
    """识别侧同一条边界：没配 key 就立刻抛，别让整卷干等。"""
    seen: list = []
    chain = archive_providers._Chain("text", [_arc_entry("a", api_key="")],
                                     on_retry=seen.append)
    with pytest.raises(RuntimeError) as e:
        chain.invoke([{"role": "user", "content": "hi"}])
    assert "未配置 key" in str(e.value)
    assert seen == []


# ── 后台面板：重连文案要写进 job，且不许冲掉进度 ──────────────────────

def test_job_retry_notifier_writes_message_without_clobbering_progress(monkeypatch, fast_retry):
    """★ 上站桥把"正在重连"写进 job 的进度文案，**且不许动进度百分比**。

    `Job.append` 的写法是 `progress = ev.get("percent", 当前值)`：不带 percent 就保持
    原进度。带了（或写成 0）会把进度条冲回去，用户看着像倒退了 —— 这条用例就是钉这个。
    """
    from station.jobs import manager as manager_mod
    from station.jobs.manager import Job
    from archive.station_adapter import _job_retry_notifier

    job = Job("archive", {})
    job.progress = 42
    job.message = "正在逐页读取内容"

    class _StubManager:
        def get(self, jid):
            return job
    monkeypatch.setattr(manager_mod, "get_manager", lambda: _StubManager())

    class _Ctx:
        job_id = job.id

    note = _job_retry_notifier(_Ctx())
    assert callable(note)
    note({"attempt": 2, "total": 5, "wait": 4.0, "error": "OpenAIConnectionError: x"})

    assert job.progress == 42, "重连文案把进度条冲掉了"
    assert job.message == "模型连接失败，4 秒后重试（第 2/5 次）"
    assert job.log[-1]["type"] == "progress"
    assert "percent" not in job.log[-1]


def test_job_retry_notifier_is_none_without_a_job():
    """CLI / 离线跑没有 job_id（ctx 上没这个字段）→ 返回 None，engine 那边安静跳过。"""
    from archive.station_adapter import _job_retry_notifier
    assert _job_retry_notifier(object()) is None


# ── 护栏：这两份常量必须同值 ──────────────────────────────────────────

def test_retry_policy_does_not_drift():
    """★ 漂移护栏：station 与 archive **各有一份**同值的重试策略（刻意不合并 ——
    archive 不许 import station，理由同 PROVIDERS 那两张表）。

    改了一边忘改另一边 → 这里的断言会红。同一个形状的先例见
    tests/test_model_config.py::test_provider_tables_do_not_drift。
    """
    assert station_model.RETRY_WAITS == archive_providers.RETRY_WAITS, (
        "两包的重试节奏漂移了：src/station/core/model.py 与 "
        "src/archive/engine/providers.py 要一起改")
    assert station_model._TRANSIENT_NAMES == archive_providers._TRANSIENT_NAMES
    samples = [ConnectionError("x"), TimeoutError("x"), HttpErr(500), HttpErr(503),
               HttpErr(408), HttpErr(429), HttpErr(401), HttpErr(404),
               RuntimeError("a 未配置 key"),
               AttributeError("partially initialized module 'httpx' has no attribute 'URL'")]
    for err in samples:
        assert (station_model._is_transient(err)
                == archive_providers._is_transient(err)), f"判定不一致：{err!r}"


# ── 根因：首次导入必须串行化 ──────────────────────────────────────────

def test_first_import_runs_once_under_concurrency(monkeypatch):
    """★ 根因护栏：多个线程同时要那句 import 时，**真正执行的只能有一次**。

    这堵的就是用户报的那个错：识别 job 的 6 路并发 worker 同时执行
    `from langchain_openai import ChatOpenAI`，后进来的线程读到还没初始化完的
    httpx → `partially initialized module`。症状是"只错一次、之后永远正常"，极难查。
    """
    monkeypatch.setattr(station_model, "_chat_openai", None)
    calls: list = []

    def slow_load():
        calls.append(1)
        time.sleep(0.05)               # 撑开窗口：没有锁的话别的线程这时就撞进来了
        return object()

    monkeypatch.setattr(station_model, "_load_chat_openai", slow_load)

    got: list = []
    lock = threading.Lock()

    def worker():
        v = station_model.import_chat_openai()
        with lock:
            got.append(v)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == [1], f"import 被执行了 {len(calls)} 次 —— 串行化没生效"
    assert len({id(v) for v in got}) == 1, "各线程拿到的不是同一个对象"
