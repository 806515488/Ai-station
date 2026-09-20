"""工具名「上线名字」测试（全离线，不联网不烧 key）。

背景（09-20 用户报"每次 chat 都很慢"查出来的）：OpenAI 的函数名规范只认
`[a-zA-Z0-9_-]`，**点号不合法**；而本仓工具名一律带命名空间点号
（`station.remember` / `mcp.<服务>.<工具>`）。严格的服务商（实测 deepseek）
会因此把整个请求 400 掉，而 4xx 按既定策略不重试 → 降级链每次都跳过它、
落到更慢的那家 —— 表面上只剩"慢"（实测同一道题 deepseek 4.7s / glm 69.7s）。

所以有了「上线名」：内部一律用原名，只在发给模型/从模型收回时转换
（`core/model.py` 的 `wire_name` / `_wire_tools` / `_from_resp`）。

这个文件钉住四件事，少一件都会退化成静默故障：
  1. 发出去的 schema 名字**一定合法**（否则又回到 400 → 换家 → 慢）；
  2. 模型回来的上线名**一定换得回原名**（否则工具调用全部落空、
     而界面上只会看到"模型说要调工具，然后什么都没有"）；
  3. 历史回放时两处名字一致（否则同一段对话里模型会以为有两个工具）；
  4. `Tool.schema()` 本身**不变**（内部/显示的公开契约仍是带点原名）。
"""
from __future__ import annotations

import re

import pytest
from langchain_core.messages import AIMessageChunk

from station.core.agent import GLOBAL_TOOLS
from station.core.model import Model, _from_resp, _to_llm, _wire_tools, wire_name
from station.core.tool import Tool

# OpenAI 的函数名规范（报错原文里就是这条）
LEGAL = re.compile(r"^[a-zA-Z0-9_-]+$")


def _entry(pid: str = "p1") -> dict:
    """造一条降级链候选（同 tests/test_model_fallback.py 的形状）。"""
    return {"id": pid, "label": pid, "base_url": "http://x/v1",
            "api_key": "k", "key_env": "", "model": "m-" + pid, "slot": "chat"}


class FakeLLM:
    """假 ChatOpenAI：只实现链会用到的三个方法。记录它**实际收到的** tools 和 msgs。"""

    def __init__(self, chunks=None):
        self.chunks = chunks or [AIMessageChunk(content="好")]
        self.got_tools = None
        self.got_msgs = None

    def bind_tools(self, tools):
        self.got_tools = tools
        return self

    def invoke(self, msgs):
        self.got_msgs = msgs
        acc = None
        for c in self.chunks:
            acc = c if acc is None else acc + c
        return acc

    def stream(self, msgs):
        self.got_msgs = msgs
        yield from self.chunks


def _patch(monkeypatch, llm: FakeLLM):
    monkeypatch.setattr(Model, "_build", lambda self, entry, timeout=None: llm)


def _tool(name: str) -> Tool:
    return Tool(name=name, description="d", run=lambda ctx, **kw: "ok")


# ── 1. 正向：名字变成合法字符集 ────────────────────────────────────


def test_wire_name_replaces_dots():
    """点号必须换成下划线 —— 它是 deepseek 400 的直接原因。"""
    assert wire_name("station.remember") == "station_remember"
    assert wire_name("mcp.websearch.web_search") == "mcp_websearch_web_search"
    assert wire_name("mcp.mcp-81f6c80a.create_review_task") == \
        "mcp_mcp-81f6c80a_create_review_task"


def test_wire_name_keeps_already_legal_names_untouched():
    """本来就合法的名字**一个字符都不能动**（否则历史里的名字对不上）。"""
    for n in ["plain", "with-dash", "with_under", "Mixed123"]:
        assert wire_name(n) == n


@pytest.mark.parametrize("name", ["", None])
def test_wire_name_is_total(name):
    """空/None 不炸（Tool 名可能没填），返回空串交给上层。"""
    assert wire_name(name) == ""


def test_real_tool_set_produces_only_legal_names():
    """★★ 回归护栏：**真实工具表**发出去的每个名字都必须合法。

    这条就是"慢"那个 bug 的守门人：只要有人新加一个带非法字符的工具名
    （或改了命名规则），这里立刻红，而不是等用户抱怨"怎么又慢了"。
    """
    # 真实全局工具 + 按本仓命名规则构造的 MCP / 技能工具（覆盖三种命名形态）
    names = [t.name for t in GLOBAL_TOOLS]
    names += ["mcp.websearch.web_search",
              "mcp.mcp-81f6c80a.create_review_task",
              "archive.set_category", "archive.export", "video.make"]
    tools = [_tool(n) for n in names]
    schemas, back = _wire_tools(tools)
    bad = [s["function"]["name"] for s in schemas if not LEGAL.match(s["function"]["name"])]
    assert not bad, f"这些工具名发给模型会被严格的服务商 400 掉：{bad}"
    # 反查表必须一一对应（撞名会被 _wire_tools 直接抛，这里再确认一遍无损）
    assert len(back) == len(tools)


def test_wire_tools_preserves_parameters_untouched():
    """只许动名字：参数表是 MCP 那边原样带过来的完整 JSON Schema，不能碰。"""
    t = Tool(name="mcp.x.y", description="d", run=lambda ctx, **kw: "ok",
             input_schema={"type": "object",
                           "properties": {"n": {"type": "integer", "enum": [1, 2]}},
                           "required": ["n"]})
    schemas, _ = _wire_tools([t])
    params = schemas[0]["function"]["parameters"]
    assert params["properties"]["n"]["enum"] == [1, 2]      # 嵌套/enum 都在


def test_colliding_wire_names_raise():
    """两个工具撞成同一个上线名时必须**明说**，不能悄悄让模型二选一。"""
    with pytest.raises(RuntimeError, match="撞名"):
        _wire_tools([_tool("a.b"), _tool("a_b")])


# ── 2. 反向：模型回的上线名要换回原名 ──────────────────────────────


def test_from_resp_maps_wire_name_back_to_canonical():
    """模型回 `station_remember` → 宿主必须拿到 `station.remember`。

    这一步漏了的话，by_name 查表落空 —— 表现是"模型说要调工具，然后什么都没有"，
    没有任何报错。
    """
    class _Resp:
        content = ""
        tool_calls = [{"id": "c1", "name": "station_remember", "args": {"text": "x"}}]

    back = {"station_remember": "station.remember"}
    got = _from_resp(_Resp(), back)
    assert got["tool_calls"][0]["name"] == "station.remember"


def test_from_resp_without_map_returns_name_as_is():
    """不传反查表时原样返回 —— 单测/无工具的调用方不该被牵连。"""
    class _Resp:
        content = ""
        tool_calls = [{"id": "c1", "name": "demo.x", "args": {}}]

    assert _from_resp(_Resp())["tool_calls"][0]["name"] == "demo.x"


def test_from_resp_unknown_wire_name_is_kept_not_dropped():
    """反查表里没有的名字（工具已被删）**原样留着**，交给上层报"工具不存在"。

    在这里悄悄丢掉 = 一次调用凭空消失且无提示。
    """
    class _Resp:
        content = ""
        tool_calls = [{"id": "c1", "name": "gone_tool", "args": {}}]

    got = _from_resp(_Resp(), {"other": "other.name"})
    assert got["tool_calls"][0]["name"] == "gone_tool"


# ── 3. 端到端：一趟 stream 的进出都对 ─────────────────────────────


def test_stream_round_trip_binds_legal_names_and_returns_canonical(monkeypatch):
    """一次完整流式调用：发出去的合法、收回来的还原。"""
    chunks = [
        AIMessageChunk(content="", tool_call_chunks=[
            {"name": "station_recall", "args": '{"q":', "id": "c1", "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[
            {"name": None, "args": '"记忆"}', "id": None, "index": 0}]),
    ]
    llm = FakeLLM(chunks)
    _patch(monkeypatch, llm)

    m = Model("text", entries=[_entry()])
    parts = list(m.stream([{"role": "user", "content": "hi"}],
                          [_tool("station.recall")]))

    # 发出去的是合法名
    assert llm.got_tools[0]["function"]["name"] == "station_recall"
    # 收回来的还原成原名，参数也聚合正确
    final = [p["final"] for p in parts if "final" in p][0]
    assert final["tool_calls"] == [{"id": "c1", "name": "station.recall",
                                    "arguments": {"q": "记忆"}}]


def test_respond_round_trip(monkeypatch):
    """非流式那条路（L2 判词/压缩摘要走的 respond）同样要转换。"""
    llm = FakeLLM([AIMessageChunk(content="", tool_call_chunks=[
        {"name": "archive_export", "args": "{}", "id": "c9", "index": 0}])])
    _patch(monkeypatch, llm)

    m = Model("text", entries=[_entry()])
    got = m.respond([{"role": "user", "content": "hi"}], [_tool("archive.export")])
    assert llm.got_tools[0]["function"]["name"] == "archive_export"
    assert got["tool_calls"][0]["name"] == "archive.export"


def test_no_tools_means_nothing_changes(monkeypatch):
    """不带工具时不绑、不转换（判词/压缩走的就是这条路）。"""
    llm = FakeLLM()
    _patch(monkeypatch, llm)
    m = Model("text", entries=[_entry()])
    got = m.respond([{"role": "user", "content": "hi"}], None)
    assert llm.got_tools is None
    assert got["tool_calls"] == []


# ── 4. 历史回放：两处名字必须一致 ────────────────────────────────


def test_to_llm_sends_wire_names_in_history():
    """历史里存的是原名，回放给模型时必须换成上线名。

    不换的话：同一段对话前半段模型看到 `station_remember`、后半段是
    `station.remember`，它会当成两个不同的工具。
    """
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "station.remember", "arguments": {"text": "x"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "station.remember",
         "content": "已记住"},
    ]
    out = _to_llm(msgs)
    assert out[0]["tool_calls"][0]["function"]["name"] == "station_remember"
    assert out[1]["name"] == "station_remember"


def test_history_round_trip_is_stable():
    """把"发出去 → 收回来"跑两遍，名字必须稳定不漂。"""
    canonical = "mcp.mcp-81f6c80a.create_review_task"
    w = wire_name(canonical)
    assert w == "mcp_mcp-81f6c80a_create_review_task"
    assert LEGAL.match(w)
    # 回放两次都得到同一个上线名（否则历史会自相矛盾）
    for _ in range(2):
        out = _to_llm([{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": canonical, "arguments": {}}]}])
        assert out[0]["tool_calls"][0]["function"]["name"] == w


# ── 5. 内部契约不变 ──────────────────────────────────────────────


def test_tool_schema_still_returns_canonical_name():
    """★ `Tool.schema()` 保持原样（带点原名）—— 它是内部/显示的公开契约。

    归一化只发生在 model.py 的边界上。若这条红了，说明有人把转换下沉到了
    Tool 里，那会让 by_name 查表、_tool_label 显示、测试全都要跟着改。
    """
    assert _tool("demo.x").schema()["function"]["name"] == "demo.x"
