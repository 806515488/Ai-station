"""降级链测试（全离线，不联网不烧 key）。

为什么要单独一个文件、还测得这么细：降级链的四条规则里有一条"已经吐过字就绝不换家"
——写错了不会崩，而是**用户看到两家模型的话拼在一起**，是那种上线很久才会被发现的
静默错误。所以这里用假模型把每条规则钉死。

手法：把 `Model._build`（真正去造 ChatOpenAI 的地方）替换成查表函数，表里放
"假客户端"或"要抛的异常"，于是整条链的调度逻辑可以不碰网络地全测一遍。
"""
from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessageChunk

from station.core.model import Model


class FakeLLM:
    """假 ChatOpenAI：只实现链会用到的三个方法（bind_tools / invoke / stream）。"""

    def __init__(self, *, content="好的", chunks=None, err=None, after=None, delay=0.0):
        self.content = content
        self.chunks = chunks          # 流式要吐的分片列表（AIMessageChunk）
        self.err = err                # 一调用就抛的异常（模拟网络/鉴权失败）
        self.after = after            # 吐完前 after 片之后再抛（模拟"说到一半断了"）
        self.delay = delay            # 每次调用先睡一会儿（模拟慢）
        self.calls = 0

    def bind_tools(self, tools):
        return self                       # 假装绑了工具，链测不需要真绑

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


def _entry(pid: str, slot: str = "chat") -> dict:
    """造一条 entry（降级链里的一个候选通道）。"""
    return {"id": pid, "label": pid, "base_url": "http://x/v1",
            "api_key": "k", "key_env": "", "model": "m-" + pid, "slot": slot}


def _patch_build(monkeypatch, table: dict, built: list | None = None):
    """把 Model._build 换成查表：表里是假客户端就返回它，是异常就抛。

    built 传列表时，会把"真正被构造过的 provider id"记进去 ——
    用来断言"某一家**根本没有被尝试**"。
    """
    def _fake_build(self, entry, timeout=None):
        if built is not None:
            built.append(entry["id"])
        v = table[entry["id"]]
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(Model, "_build", _fake_build)


# ── 构造期：没 key / 建不起来 → 换下一家 ──────────────────────────────

def test_chain_switches_on_constructor_error(monkeypatch):
    """验证构造期降级：第一家没配 key（构造就抛）时，自动用第二家把活干完。

    这正是"某家没填 key 也能跑"的保证 —— 改造前这种情况直接报错整轮失败。
    """
    good = FakeLLM(content="第二家答的")
    _patch_build(monkeypatch, {"a": RuntimeError("a 未配置 key"), "b": good})
    m = Model(entries=[_entry("a"), _entry("b")])
    assert m.respond([{"role": "user", "content": "hi"}])["content"] == "第二家答的"
    assert good.calls == 1


def test_legacy_single_channel_still_raises(monkeypatch):
    """验证老路径没被改坏：不给 entries 时，未知通道仍然在**构造时**就抛。

    老调用方（L2 判词）依赖这个时机，别改成懒抛。
    """
    with pytest.raises(RuntimeError) as e:
        Model("text", channel="不存在的通道")
    assert "未知通道" in str(e.value)


# ── 流式：吐字前 vs 吐字后（这一组是重点）────────────────────────────

def test_chain_switches_before_first_delta(monkeypatch):
    """验证"还没吐字就失败 → 可以换家"：第一片都没发出去，换家不会串话。"""
    good = FakeLLM(chunks=[AIMessageChunk(content="你"), AIMessageChunk(content="好")])
    _patch_build(monkeypatch, {"a": FakeLLM(err=ConnectionError("连不上")),
                               "b": good})
    parts = list(Model(entries=[_entry("a"), _entry("b")]).stream(
        [{"role": "user", "content": "hi"}]))
    text = "".join(p["delta"] for p in parts if "delta" in p)
    assert text == "你好"                       # 全部来自第二家
    assert [p["final"] for p in parts if "final" in p][0]["content"] == "你好"
    assert good.calls == 1


def test_no_switch_after_first_delta(monkeypatch):
    """★ 最要命的一条：已经吐过字之后失败，必须**报错而不是换家**。

    换家的话前端会先显示第一家的半句话、再接着显示第二家的整句话 —— 拼出来的胡话
    看起来像模型在胡说，其实是"两家的输出粘在一起"。断言第二家**从未被构造**。
    """
    built: list = []
    bad = FakeLLM(chunks=[AIMessageChunk(content="前半句")], after=1)
    good = FakeLLM(content="后半句")
    _patch_build(monkeypatch, {"a": bad, "b": good}, built=built)

    with pytest.raises(RuntimeError) as e:
        list(Model(entries=[_entry("a"), _entry("b")]).stream(
            [{"role": "user", "content": "hi"}]))
    assert "不再切换通道" in str(e.value)
    assert built == ["a"]                       # 第二家根本没被碰过


# ── 非流式：invoke 是原子的，失败随便换 ────────────────────────────────

def test_respond_switches_on_call_failure(monkeypatch):
    """验证非流式降级：invoke 要么完整拿到要么什么都没有，失败换家没有副作用。"""
    good = FakeLLM(content="兜底答的")
    _patch_build(monkeypatch, {"a": FakeLLM(err=TimeoutError("超时")), "b": good})
    assert Model(entries=[_entry("a"), _entry("b")]).respond(
        [{"role": "user", "content": "hi"}])["content"] == "兜底答的"


# ── 总预算：判词槽的生命线 ────────────────────────────────────────────

def test_total_budget_is_shared_across_chain(monkeypatch):
    """★ 验证总预算是**整条链共享**的，不是每条各算一份。

    ROUTE_TIMEOUT=1.5 的语义是"判词最多花 1.5 秒"；若实现成"每条链各 1.5 秒"，
    3 条链就是 4.5 秒卡顿 —— 等于把 09-10 刚修好的那个卡顿原样还回来。
    这里总预算 0.05s、第一家就慢 0.08s，第二家必须**一次都不试**。
    """
    built: list = []
    # 第一家又慢又失败（慢过总预算）→ 第二家必须一次都不试
    _patch_build(monkeypatch, {"a": FakeLLM(err=TimeoutError("超时"), delay=0.08),
                               "b": FakeLLM(content="快的")}, built=built)
    m = Model(entries=[_entry("a", "route"), _entry("b", "route")],
              timeout=5, total_budget=0.05)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as e:
        m.respond([{"role": "user", "content": "hi"}])
    assert "总预算" in str(e.value)
    assert built == ["a"]                       # 第二家没被尝试
    assert time.monotonic() - t0 < 1.0          # 没有傻等 timeout=5


def test_no_budget_means_each_attempt_gets_full_timeout(monkeypatch):
    """验证不给总预算时不像判词那样卡：每次尝试各自拿满 timeout（默认行为）。"""
    good = FakeLLM(content="第二家")
    _patch_build(monkeypatch, {"a": FakeLLM(err=TimeoutError("超时")), "b": good})
    m = Model(entries=[_entry("a"), _entry("b")])       # 不给 total_budget
    assert m.respond([{"role": "user", "content": "hi"}])["content"] == "第二家"


# ── 全挂：报错要说人话 ────────────────────────────────────────────────

def test_all_entries_fail_raises_actionable(monkeypatch):
    """验证全挂时的文案：说清是哪个槽位、试过哪些家，用户能照着去改配置。"""
    _patch_build(monkeypatch, {"a": RuntimeError("a 未配置 key"),
                               "b": RuntimeError("b 未配置 key")})
    m = Model(entries=[_entry("a", "vision"), _entry("b", "vision")])
    with pytest.raises(RuntimeError) as e:
        m.respond([{"role": "user", "content": "hi"}])
    msg = str(e.value)
    assert "vision" in msg and "a → b" in msg


def test_empty_chain_raises(monkeypatch):
    """验证空链：配置里该槽位没留任何候选时，构造就报错（别留个哑巴对象）。"""
    with pytest.raises(RuntimeError) as e:
        Model(entries=[])
    assert "降级链是空的" in str(e.value)


# ── 识别侧那条薄链（archive/engine/providers.py）──────────────────────

def _arc_entry(pid: str, **kw) -> dict:
    e = {"id": pid, "label": pid, "base_url": "http://x/v1",
         "api_key": "k", "key_env": "", "model": "m-" + pid}
    e.update(kw)
    return e


def test_archive_chain_switches_on_missing_key():
    """验证识别侧降级：建档那家没配 key 时自动换下一家，且链标签是链首 id。"""
    from archive.engine.providers import make_model
    good = FakeLLM(content="读到了")
    ch = make_model("vision", entries=[
        _arc_entry("a", api_key=""),                       # 没 key → 构造就抛
        _arc_entry("b", _llm=good),
    ])
    assert ch.channel == "a"                               # 缓存键取链首
    assert ch.invoke([{"role": "user", "content": "x"}]).content == "读到了"


def test_archive_chain_all_fail_raises():
    """验证识别侧全挂时说清试过谁（单页失败由 node_mark 兜成空卡，不拖垮整卷）。"""
    from archive.engine.providers import make_model
    ch = make_model("vision", entries=[_arc_entry("a", api_key=""),
                                       _arc_entry("b", api_key="")])
    with pytest.raises(RuntimeError) as e:
        ch.invoke([{"role": "user", "content": "x"}])
    assert "a → b" in str(e.value)


def test_archive_chain_keeps_multimodal_content():
    """★ 红线：识别链必须把多模态消息**原样**交给 langchain。

    node_mark 发的是 [{type:text},{type:image_url}] 结构块。如果哪天有人图省事
    把这里的薄链换成 station 的 Model，`_to_llm` 会把 content 一律 str()，
    图片块变成一串 Python repr —— 建档会全盘报错，且报的是模型的 400，很难查。
    """
    from archive.engine.providers import make_model
    seen: dict = {}

    class Rec(FakeLLM):
        def invoke(self, msgs):
            seen["msgs"] = msgs
            return AIMessageChunk(content="ok")

    ch = make_model("vision", entries=[_arc_entry("a", _llm=Rec())])
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "读这页"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]
    ch.invoke(msgs)
    assert isinstance(seen["msgs"][0]["content"], list)     # 还是结构块
    assert seen["msgs"][0]["content"][1]["type"] == "image_url"
