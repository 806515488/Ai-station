"""材料装配（graph.node_build_mats）的离线单测 —— 桩数据，不联网。

这一段决定"目录上那一行长什么样"：叫什么名、算哪天、几页、按什么顺序排。
照片顺序随机之后，最容易犯的错是**把册内栏目名当材料名**（于是出现一堆叫
"工作经历"的材料）和**把 members 又按照片号重排一遍**（装订序就没了）。
"""
from __future__ import annotations

from archive.engine import graph, seg


def rec(seq: int, form=None, pk=None, pg=None, t=None, y=None, s=None) -> dict:
    date = {"y": y, "m": None, "d": None} if y else None
    return {"seq": seq, "md5": f"md5-{seq}", "path": f"{seq}.jpg",
            "ocr": {"mark": {"t": t if t is not None else pk, "doc": None,
                             "date": date, "form": form, "pk": pk, "pg": pg,
                             "s": s, "u": False},
                    "title": t, "date": date, "texts": [s] if s else [],
                    "cls": {"category": None, "doubt": False, "doc": None,
                            "evidence": s or ""}}}


def build(recs, mat):
    """跑归组 + 装配（跳过要模型的定类：直接把定类结果当参数传进来）。"""
    return graph.node_build_mats({"records": recs, "cands": seg.build_candidates(recs),
                                  "mat": mat})


def test_title_comes_from_the_book_not_the_column():
    """★ 代表页的表头是栏目名（"工作经历"）→ 材料名必须取规范册名"干部履历表"。

    不修这条，目录里会出现一堆叫"工作经历""学习简历"的材料 —— 这正是改造前的样子。
    """
    recs = [rec(45, form="干部履历表", pk="工作经历"), rec(3, form="干部履历表", pk="封面"),
            rec(74, form="干部履历表", pk="其他需要说明的情况")]
    out = build(recs, {1: {"category": "一", "reason": "履历"}})
    assert out["materials"][0]["title"] == "干部履历表"


def test_book_title_page_wins_when_present():
    """如果某页的表头本身就是册名（封面页），就用它原文（更忠实于实物）。"""
    recs = [rec(45, form="干部履历表", pk="工作经历"),
            rec(88, form="干部履历表", pk="干部履历表", t="干部履历表")]
    out = build(recs, {1: {"category": "一"}})
    m = out["materials"][0]
    assert out["materials"][0]["rep"] == 88 and m["title"] == "干部履历表"


def test_members_keep_binder_order_and_raw_seqs_keep_photo_order():
    """members 是**装订序**（导出 PDF 吃它），raw_seqs 是**照片号升序**（人找实物用）。"""
    recs = [rec(1, form="干部履历表", pk="工作经历", pg=3),
            rec(2, form="干部履历表", pk="封面", pg=1),
            rec(3, form="干部履历表", pk="说明", pg=2)]
    m = build(recs, {1: {"category": "一"}})["materials"][0]
    assert m["members"] == [2, 3, 1]            # 按印刷页码 1,2,3
    assert m["raw_seqs"] == [1, 2, 3]           # 照片号升序


def test_copies_are_counted_but_pages_is_one_copy():
    """一式N份：copies 记份数，pages 只算本体那一份的页数（PDF 只收第一份）。"""
    recs = [rec(3, form="优秀共产党员推荐审批表", pk="主要事迹", s="事迹"),
            rec(4, form="优秀共产党员推荐审批表", pk="公司党委及集团党委意见", s="同意"),
            rec(5, form="优秀共产党员推荐审批表", pk="主要事迹", s="事迹"),
            rec(6, form="优秀共产党员推荐审批表", pk="公司党委及集团党委意见", s="同意")]
    m = build(recs, {1: {"category": "六"}})["materials"][0]
    assert m["copies"] == 2 and m["pages"] == 2
    assert m["dup_pages"] == [5, 6]
    assert m["assigned_pages"] == m["members"] + [5, 6]


def test_materials_are_sorted_in_catalog_order():
    """★ 材料行按目录序排（一→十）—— 交互层的类内编号与 Excel/PDF 都吃这个顺序。"""
    recs = [rec(1, form="体检表", pk="体检表"),                    # 十
            rec(2, form="职工工资变动表", pk="职工工资变动表", y=2014),  # 九-1
            rec(3, form="干部履历表", pk="封面")]                    # 一
    out = build(recs, {1: {"category": "十"}, 2: {"category": "九-1"},
                       3: {"category": "一"}})
    assert [m["category"] for m in out["materials"]] == ["一", "九-1", "十"]


def test_undecided_bucket_becomes_C0_and_pages_are_not_dropped():
    """没定出类的份 → 不生成材料行，但要进 C0 清单（防止"悄悄少了一页"）。"""
    recs = [rec(3, form="干部履历表", pk="封面")]
    out = build(recs, {})                       # 没有定类结果
    assert out["materials"] == []
    assert any(i["code"] == "C0-未归类" for i in out["issues"])


def test_doubtful_bucket_raises_C3():
    """归组存疑（同类册桶/残页并入）→ 材料行标 doubt 并进 C3 清单，逼人工看一眼。"""
    recs = [rec(3, pk="工作经历"), rec(45, pk="学习简历")]
    out = build(recs, {1: {"category": "一"}})
    m = out["materials"][0]
    assert m["doubt"] is True
    assert any(i["code"] == "C3-归组存疑" for i in out["issues"])
