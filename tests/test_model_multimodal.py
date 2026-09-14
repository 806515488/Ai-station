"""宿主 Model 的**多模态**消息支持（全离线，不联网不烧 key）。

为什么值得单独一组：`_to_llm` 曾经把 content 一律 `str()`，图片块会变成一串 Python
repr、图彻底丢失；`_from_resp` 则对非字符串的 content 直接置空。两个方向都是**静默**的
—— 不报错，只是"图没发出去"或"结果读成了空串"，上层看到的现象是"模型没给出结果"。
09-14 加人像提取时踩到第二条，才有了这个文件。

手法和 tests/test_model_fallback.py 一样：把 `Model._build` 换成查表，塞一个记录型的
假客户端，于是整条链不用碰网络就能断言"到底把什么发给了对端"。
"""
from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from station.core.model import Model, _from_resp, _to_llm


class RecordingLLM:
    """假 ChatOpenAI：把收到的 msgs 记下来，好断言"透传的是什么"。"""

    def __init__(self, content="好的"):
        self.content = content
        self.seen: list = []

    def bind_tools(self, tools):
        return self

    def invoke(self, msgs):
        self.seen.append(msgs)
        return AIMessageChunk(content=self.content)


class _Resp:
    """最小响应对象（只有 _from_resp 会读的 content 字段）。"""

    def __init__(self, content):
        self.content = content
        self.tool_calls = None


def _entry(pid: str = "a", slot: str = "vision") -> dict:
    return {"id": pid, "label": pid, "base_url": "http://x/v1",
            "api_key": "k", "key_env": "", "model": "m-" + pid, "slot": slot}


def _patch_build(monkeypatch, llm):
    monkeypatch.setattr(Model, "_build", lambda self, entry, timeout=None: llm)


# ── 发出去的方向：图片块要原样过 ─────────────────────────────────────

def test_multimodal_blocks_are_passed_through(monkeypatch):
    """★ 结构块列表必须**原样**交给 langchain —— 拍平了图就没了。

    把 `_to_llm` 改回 `str(...)`，这条立刻红（而那正是它存在的理由）。
    """
    llm = RecordingLLM()
    _patch_build(monkeypatch, llm)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "读这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]

    Model("vision", entries=[_entry()]).respond(msgs)

    got = llm.seen[0][0]["content"]
    assert isinstance(got, list), f"图片块被拍平了（现在是 {type(got).__name__}）"
    assert got[0] == {"type": "text", "text": "读这张图"}
    assert got[1]["type"] == "image_url"
    assert got[1]["image_url"]["url"].endswith("AAAA")


def test_plain_text_content_is_still_a_string(monkeypatch):
    """纯文本调用方走的还是老路 —— 行为一字不变（全仓 respond/stream 都靠这条）。"""
    llm = RecordingLLM()
    _patch_build(monkeypatch, llm)
    Model("text", entries=[_entry(slot="chat")]).respond(
        [{"role": "user", "content": "你好"}])
    assert llm.seen[0][0]["content"] == "你好"
    assert isinstance(llm.seen[0][0]["content"], str)


def test_missing_content_keeps_old_behavior():
    """content 缺失/None 仍转成字符串（**别顺手"修好"它** —— 那是无关的行为变更）。"""
    assert _to_llm([{"role": "user"}])[0]["content"] == ""
    assert _to_llm([{"role": "user", "content": None}])[0]["content"] == "None"


def test_tool_call_messages_are_untouched():
    """改动只碰"普通消息"那一支 —— 带 tool_calls 的助手消息与工具结果照旧。"""
    msgs = [{"role": "assistant", "content": "我来调工具",
             "tool_calls": [{"id": "c1", "name": "demo.now", "arguments": {"a": 1}}]},
            {"role": "tool", "content": "结果", "tool_call_id": "c1", "name": "demo.now"}]
    out = _to_llm(msgs)
    assert out[0]["tool_calls"][0]["function"]["name"] == "demo.now"
    assert out[1]["tool_call_id"] == "c1"


# ── 收回来的方向：content blocks 要抽成文字 ──────────────────────────

def test_from_resp_joins_text_blocks():
    """★ 响应是结构块列表时要**抽出文字**。

    这条现在是绿的、改之前必红：原实现对非 str 直接置空，于是"读图问一句"会静默
    拿到空串 —— 上层以为模型没给出结果，其实文字就在响应里。
    """
    got = _from_resp(_Resp([{"type": "text", "text": "前半段"},
                            {"type": "text", "text": "后半段"}]))
    assert got["content"] == "前半段后半段"


def test_from_resp_ignores_non_text_blocks():
    """只看 type=="text" 的块；别的块（图片等）忽略，不要让它们变成 repr 混进正文。"""
    got = _from_resp(_Resp([{"type": "image_url", "image_url": {"url": "x"}},
                            {"type": "text", "text": "描述"}]))
    assert got["content"] == "描述"


def test_from_resp_keeps_plain_string_without_breaking():
    """普通字符串响应照旧；奇怪类型（非 str 且非 list）也别炸，回空串。"""
    assert _from_resp(_Resp("普通回复"))["content"] == "普通回复"
    assert _from_resp(_Resp(None))["content"] == ""


def test_vision_round_trip_through_model(monkeypatch):
    """端到端（离线）：发图出去 → 从结构块响应里收回文字，两处一起验。"""
    llm = RecordingLLM(content=[{"type": "text", "text": "一位短发、戴细框眼镜的人"}])
    _patch_build(monkeypatch, llm)
    out = Model("vision", entries=[_entry()]).respond(
        [{"role": "user", "content": [
            {"type": "text", "text": "描述外貌"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}])
    assert isinstance(llm.seen[0][0]["content"], list)
    assert out["content"] == "一位短发、戴细框眼镜的人"
