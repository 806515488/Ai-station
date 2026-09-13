"""残页裁决节点（graph.node_resolve）的离线单测 —— 桩文本模型，不联网。

这一段的全部价值在于**"拿不准时一个字都不改"**：归组是确定性的，认不出身份的页
宁可留着当残页，也不能猜着并 —— 猜错会把两本册子并成一本，比拆散更难发现。
所以下面一半用例在测"失败路径"（模型回 null / 回垃圾 / 直接抛异常）。
"""
from __future__ import annotations

import json

from archive.engine import graph, seg


def rec(seq: int, form=None, pk=None, t=None, y=None, s=None) -> dict:
    """造一条 record（形状与 graph._parse_mark 产出的包裹卡一致）。"""
    date = {"y": y, "m": None, "d": None} if y else None
    return {"seq": seq, "md5": f"md5-{seq}", "path": f"{seq}.jpg",
            "ocr": {"mark": {"t": t if t is not None else pk, "doc": None,
                             "date": date, "form": form, "pk": pk, "pg": None,
                             "s": s, "u": False},
                    "title": t, "date": date, "texts": [s] if s else [],
                    "cls": {"category": None, "doubt": False, "doc": None,
                            "evidence": s or ""}}}


class _StubText:
    """桩文本模型：按预设内容回话；可以设成"抛异常"来测失败路径。"""

    def __init__(self, payload=None, boom=False):
        self._payload = payload
        self.calls = 0
        self.prompts: list[str] = []
        self._boom = boom

    def invoke(self, msgs):
        self.calls += 1
        self.prompts.append(msgs if isinstance(msgs, str) else str(msgs))
        if self._boom:
            raise RuntimeError("模型挂了")
        text = self._payload if isinstance(self._payload, str) \
            else json.dumps(self._payload or {}, ensure_ascii=False)

        class _R:
            content = text
        return _R()


def _state(recs, stub):
    """把页记录跑一遍真实的归组，再喂给 resolve —— 与线上链路一致。"""
    cands = seg.build_candidates(recs)
    return {"records": recs, "cands": cands, "llm_text": stub}


# ── 正常：并进已有的份 ───────────────────────────────────────────

def test_orphan_attached_to_existing_bucket():
    """模型说"这页属于干部履历表" → 并进那一份，页数 +1，并标存疑 + 记一条 C2。"""
    recs = [rec(3, form="干部履历表", pk="封面"), rec(45, form="干部履历表", pk="工作经历"),
            rec(74, t="山西省运城汽车运输公司稿纸", s="手写的工作经历补充")]
    out = graph.node_resolve(_state(recs, _StubText(
        {"attach": [{"seq": 74, "to": "干部履历表"}]})))
    book = next(c for c in out["cands"] if c["form"] == "干部履历表")
    assert book["pages"] == 3 and 74 in book["seqs"]
    assert book["doubt"] is True                       # 并进来的必须标存疑
    assert any("并入" in n["message"] for n in out["orphan_notes"])


def test_orphan_left_alone_when_model_says_null():
    """模型回 null（拿不准）→ 残页保持独立，只是进清单提醒人工看。"""
    recs = [rec(3, form="干部履历表", pk="封面"),
            rec(6, t="稿纸", s="手写内容")]
    out = graph.node_resolve(_state(recs, _StubText({"attach": [{"seq": 6, "to": None}]})))
    assert any(c["orphan"] for c in out["cands"])
    assert any(n["code"] == "C2-残页待核" for n in out["orphan_notes"])


def test_never_attaches_to_a_name_the_model_invented():
    """★ 模型编了一个不存在的份名 → 不并（只允许并进已认出的份）。"""
    recs = [rec(3, form="干部履历表", pk="封面"), rec(6, t="稿纸", s="手写")]
    out = graph.node_resolve(_state(recs, _StubText(
        {"attach": [{"seq": 6, "to": "我猜是干部履历表第二册"}]})))
    assert any(c["orphan"] for c in out["cands"])
    assert all(c["pages"] == 1 for c in out["cands"])


# ── 失败路径：一个字都不改 ───────────────────────────────────────

def test_bad_json_is_noop():
    """模型回了不是 JSON 的东西 → 不崩、不改，只留一句"裁决失败"。"""
    recs = [rec(3, form="干部履历表", pk="封面"), rec(6, t="稿纸", s="手写")]
    out = graph.node_resolve(_state(recs, _StubText("这不是JSON，我只是随便说说")))
    assert any(c["orphan"] for c in out["cands"])
    assert out["orphan_notes"]


def test_call_failure_is_noop():
    """调用抛异常（断网/超时）→ 同样不崩、不改。识别链不能被这一步拖垮。"""
    recs = [rec(3, form="干部履历表", pk="封面"), rec(6, t="稿纸", s="手写")]
    out = graph.node_resolve(_state(recs, _StubText(boom=True)))
    assert any(c["orphan"] for c in out["cands"])


def test_non_orphan_pages_are_never_touched():
    """★ 只动残页：有身份的正常份，成员集合一个都不许变。"""
    recs = [rec(3, form="干部履历表", pk="封面"), rec(45, form="干部履历表", pk="工作经历"),
            rec(6, t="稿纸", s="手写")]
    state = _state(recs, _StubText({"attach": [{"seq": 6, "to": "干部履历表"}]}))
    before = {c["form"]: list(c["seqs"]) for c in state["cands"] if c["form"]}
    out = graph.node_resolve(state)
    after = {c["form"]: [s for s in c["seqs"] if s in (3, 45)] for c in out["cands"]
             if c["form"]}
    assert before == after


# ── 批量与输出规模（钉住"别再来一次 103 条超时"）──────────────────

def test_batches_are_bounded():
    """65 个残页 → 恰好分 3 批（每批 ≤30）；且提示词里每批的残页行数也 ≤30。

    这是早期踩过的坑：一次把上百份材料丢给模型让它生成，输出过长直接读超时。

    注意要配够"认得出身份"的页：覆盖率低于一半会触发回退闸门，那就不走这个节点了。
    """
    recs = [rec(3 + i, form="职工工资变动表", y=1950 + i) for i in range(65)]
    recs += [rec(100 + i, t=f"稿纸{i}", s=f"第{i}页手写") for i in range(65)]
    stub = _StubText({"attach": []})
    graph.node_resolve(_state(recs, stub))
    assert stub.calls == 3
    for p in stub.prompts:
        assert p.count("张：表头") <= 30        # 每批喂进去的残页行数
