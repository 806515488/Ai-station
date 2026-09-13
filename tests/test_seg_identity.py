"""归组（engine/seg.py）的离线单测 —— 全部用合成"内容卡"，不联网、不烧 key。

新手视角（Java 朋友版）：为什么这组用例特别重要？因为 2026-09-12 用户确认了一个
推翻性事实：**照片的上传顺序是随机的**（第 1 张和第 50 张可能本来挨着）。旧引擎
"顺着照片顺序、比较相邻两页像不像同一份"的做法因此整体失效。新做法是"按身份归组"：
同一本册子（form）+ 同一天 → 同一份。下面的用例就是把这套判据钉死，尤其是
**"相隔很远的页也能归到一份"**、**"同名栏目但不同册子绝不合并"** 这两条 ——
它们是这次改造的立身之本，谁改回去谁就会看到这些用例变红。
"""
from __future__ import annotations

from archive.engine import seg


# ── 造一页"内容卡" ────────────────────────────────────────────────
def rec(seq: int, form=None, pk=None, pg=None, t=None, y=None, m=None, d=None,
        s=None, md5=None) -> dict:
    """造一条 record（形状与 graph._parse_mark 产出的包裹卡一致）。

    只填 seg 会读的字段：ocr.mark 里的 form/pk/pg/t/date/s，外加 seq/md5/path。
    """
    date = {"y": y, "m": m, "d": d} if y else None
    return {"seq": seq, "md5": md5 or f"md5-{seq}", "path": f"{seq}.jpg",
            "ocr": {"mark": {"t": t if t is not None else pk, "doc": None,
                             "date": date, "form": form, "pk": pk, "pg": pg,
                             "s": s, "u": False},
                    "title": t, "date": date, "texts": [s] if s else [],
                    "cls": {"category": None, "doubt": False, "doc": None,
                            "evidence": s or ""}}}


def forms(cands) -> list:
    """把结果压成"每份：(册名, 页数, 成员照片号)"，方便断言。"""
    return [(c["form"], c["pages"], c["seqs"]) for c in cands]


# ── ① 身份键：同 form 同日期必并、异日期必分 ──────────────────────

def test_same_form_same_date_merges():
    """同一个表名、同一天的两页 = 同一份（哪怕照片号隔着十万八千里）。"""
    cands = seg.build_candidates([rec(7, form="职工工资变动表", y=2014, m=8, d=1),
                                  rec(60, form="职工工资变动表", y=2014, m=8, d=1)])
    assert len(cands) == 1 and cands[0]["pages"] == 2
    assert sorted(cands[0]["seqs"]) == [7, 60]


def test_same_form_diff_date_splits():
    """同一种表、8 个不同日期 = 8 份独立材料（真实卷的工资变动类就是这么一批）。"""
    recs = [rec(i, form="山西省企业职工岗位技能工资变动审批表", y=1990 + i * 2,
                pk="山西省企业职工岗位技能工资变动审批表") for i in range(8)]
    cands = seg.build_candidates(recs)
    assert len(cands) == 8
    assert all(c["pages"] == 1 for c in cands)
    assert all(c["copies"] == 1 for c in cands)


def test_no_date_single_pages_do_not_merge():
    """没日期、又是"非册子"的表单 → 不并（宁可拆散，也不能把两张不同的任免表并成一份）。"""
    cands = seg.build_candidates([
        rec(3, form="干部任免呈报表", pk="干部任免呈报表", s="拟任运城公司副经理"),
        rec(9, form="干部任免呈报表", pk="干部任免呈报表", s="拟任太原公司经理")])
    assert len(cands) == 2


# ── ② 册子白名单：整册一份（忽略日期）───────────────────────────

def test_book_whitelist_ignores_date():
    """册子型材料（干部履历表）不管各页日期怎么填，整本一份。"""
    recs = [rec(3, form="干部履历表", pk="本人经历（包括学历）", y=1999),
            rec(45, form="干部履历表", pk="工作经历", y=2005),
            rec(74, form="干部履历表", pk="其他需要说明的情况", y=2001)]
    cands = seg.build_candidates(recs)
    assert len(cands) == 1 and cands[0]["pages"] == 3


def test_form_alias_maps_to_same_book():
    """别名（"履历表"）要归一到规范册名，否则同一本册子会被拆成两本。"""
    cands = seg.build_candidates([rec(3, form="履历表", pk="工作经历"),
                                  rec(45, form="干部履历表", pk="学习简历")])
    assert len(cands) == 1 and cands[0]["form"] == "干部履历表"


# ── ③ 跨册不混：同名栏目也必须按册分开 ──────────────────────────

def test_same_column_diff_book_never_merges():
    """★ "工作经历"两本册子都有 —— 册名不同就必须是两份，绝不能并成一份。"""
    cands = seg.build_candidates([
        rec(3, form="干部履历表", pk="工作经历"),
        rec(45, form="干部履历表", pk="本人经历（包括学历）"),
        rec(99, form="工人登记表", pk="工作经历"),
        rec(110, form="工人登记表", pk="主要简历")])
    assert len(cands) == 2
    by = {c["form"]: c for c in cands}
    assert by["干部履历表"]["pages"] == 2 and by["工人登记表"]["pages"] == 2


def test_form_contradicting_pk_goes_to_doubtful_bucket():
    """册名与栏目打架（模型说"工人登记表"但栏目是只有志愿书才有的"誓词"）→ 不信任何一边，
    并进"同类册桶"并标存疑，等人工看一眼；绝不静默按册名并进工人登记表。"""
    cands = seg.build_candidates([rec(1, form="工人登记表", pk="誓词")])
    assert len(cands) == 1 and cands[0]["doubt"] is True


# ── ④ 非相邻页也归一份（这条钉住"不再依赖照片顺序"）──────────────

def test_non_adjacent_pages_merge_into_one():
    """★ 履历表的页散在 seq 3/45/74/110，中间夹着 20 页无关材料 → 仍归成一份。"""
    recs = [rec(3, form="干部履历表", pk="封面"),
            rec(45, form="干部履历表", pk="工作经历")]
    recs += [rec(50 + i, form="山西省企业职工岗位技能工资变动审批表", y=1990 + i)
             for i in range(20)]
    recs += [rec(74, form="干部履历表", pk="家庭成员及主要社会关系情况"),
             rec(110, form="干部履历表", pk="本人经历（包括学历）")]
    cands = seg.build_candidates(recs)
    book = next(c for c in cands if c["form"] == "干部履历表")
    assert book["pages"] == 4 and sorted(book["seqs"]) == [3, 45, 74, 110]
    assert len(cands) == 21                    # 1 本册子 + 20 张单页工资表


# ── ⑤ 栏目名定位（form 判不出时的确定性兜底）────────────────────

def test_unique_column_locates_book_without_model():
    """★ 栏目名唯一（"誓词"只可能属于入党志愿书）→ 不用 form 也能归册，且**不调模型**。"""
    cands = seg.build_candidates([rec(55, pk="入党志愿"), rec(16, pk="总支部审查（审批）意见"),
                                  rec(64, pk="誓词")])
    assert len(cands) == 1
    assert cands[0]["form"] == "中国共产党入党志愿书" and cands[0]["pages"] == 3


def test_ambiguous_column_same_cat_becomes_one_doubtful_bucket():
    """栏目名几本册子都有、又没册名，但它们同属一个大类 → 并成一个"同类册桶"并标存疑。

    为什么不各自独立：并成 1 条 + 存疑，用户一句话"拆成两本"就对了；
    各自独立就是最差的"20 页拆成 14 条"，还得人工一条条并。
    """
    recs = [rec(3, pk="工作经历"), rec(45, pk="学习简历"),
            rec(74, pk="工作经历")]
    cands = seg.build_candidates(recs)
    assert len(cands) == 1
    assert cands[0]["pages"] == 3 and cands[0]["doubt"] is True


def test_column_shared_across_categories_stays_orphan():
    """栏目名横跨不同大类（"家庭成员及社会关系"在履历表、工人登记表**和入党志愿书**里都有）
    → 不敢并进任何一类，留成残页交给 resolve 问模型。"""
    cands = seg.build_candidates([rec(74, pk="家庭成员及社会关系情况"),
                                  rec(96, pk="家庭成员及社会关系情况")])
    assert all(c["orphan"] is True for c in cands)


def test_unlocatable_page_stays_orphan():
    """既没有册名、栏目名也不认识 → 残页，等 resolve 节点问模型（这里只保证不瞎并）。

    注意要先有几页能认出来的，否则整体覆盖率过低会触发回退闸门（那是另一条用例）。
    """
    recs = [rec(6, t="山西省运城汽车运输公司稿纸", s="申请入党"),
            rec(72, t="山西省运城汽车运输公司稿纸", s="思想汇报"),
            rec(3, form="干部履历表", pk="封面"),
            rec(45, form="干部履历表", pk="工作经历")]
    cands = seg.build_candidates(recs)
    orphan = [c for c in cands if c["orphan"]]
    assert len(orphan) == 2 and all(c["pages"] == 1 for c in orphan)


# ── ⑥ 一式 N 份（复本）──────────────────────────────────────────

def test_copies_within_one_bucket():
    """同一份 2 页拍了两遍（身份键相同 → 落进同一个桶）→ 桶内拆出复本。"""
    two = [rec(10, form="优秀共产党员推荐审批表", pk="主要事迹", s="事迹"),
           rec(11, form="优秀共产党员推荐审批表", pk="公司党委及集团党委意见", s="同意")]
    cands = seg.build_candidates(two + [rec(12, form="优秀共产党员推荐审批表",
                                            pk="主要事迹", s="事迹"),
                                        rec(13, form="优秀共产党员推荐审批表",
                                            pk="公司党委及集团党委意见", s="同意")])
    assert len(cands) == 1
    c = cands[0]
    assert c["pages"] == 2 and c["copies"] == 2
    assert c["seqs"] == [10, 11] and c["dup_groups"] == [[12, 13]]


def test_copies_detected_beyond_the_old_window():
    """★ 复本相隔超过 20 份也要认出来 —— 旧代码"只往后看 20 份"的窗口就是漏检源头。

    这里用两张**无日期**的同类单页件（各自成桶）造出这个场景：它们在列表里隔了 24 份。
    """
    recs = [rec(1, form="社保参保证明", pk="社保参保证明", s="参保缴费证明内容")]
    recs += [rec(10 + i, form="职工工资变动表", pk="职工工资变动表", y=1980 + i)
             for i in range(24)]
    recs += [rec(99, form="社保参保证明", pk="社保参保证明", s="参保缴费证明内容")]
    cands = seg.build_candidates(recs)
    copies = [c for c in cands if c["copies"] > 1]
    assert len(copies) == 1 and copies[0]["copies"] == 2
    assert len(cands) == 25                     # 24 张工资表 + 1 条（一式2份）


# ── ⑦ 册内页序与代表页 ──────────────────────────────────────────

def test_printed_page_number_orders_members():
    """整份都印了页码、且不重复 → 按印刷页码排（作者自己编的号最可靠），不看照片号。"""
    # 乱序给：照片号 1 的其实是第 3 页，照片号 3 的是第 1 页
    recs = [rec(1, form="干部履历表", pk="工作经历", pg=3),
            rec(2, form="干部履历表", pk="说明", pg=2),
            rec(3, form="干部履历表", pk="封面", pg=1)]
    c = seg.build_candidates(recs)[0]
    assert c["seqs"] == [3, 2, 1]               # 按 pg 1,2,3 → 对应照片 3,2,1


def test_page_number_needs_full_coverage_and_no_duplicates():
    """★ 用户 2026-09-12 提醒：一卷里不同表册各编各的页码，**印刷页码不是全局坐标**。

    所以只有当这一份里"每一页都有号、且号互不重复"时才敢信它；缺号的、重号的
    （说明混进了另一套编号）一律退回按栏目版序排。
    """
    # 缺号：只有两页有页码 → 不信 pg，按版序（本人经历=4 在工作经历=5 之前）
    recs = [rec(1, form="干部履历表", pk="工作经历", pg=3),
            rec(2, form="干部履历表", pk="本人经历（包括学历）"),
            rec(3, form="干部履历表", pk="封面", pg=1)]
    assert seg.build_candidates(recs)[0]["seqs"] == [3, 2, 1]   # 封面1 → 本人经历4 → 工作经历5

    # 重号：两页都印着"3" → 不是同一套编号 → 同样不信 pg
    recs = [rec(1, form="干部履历表", pk="工作经历", pg=3),
            rec(2, form="干部履历表", pk="学习简历", pg=3)]
    got = seg.build_candidates(recs)[0]["seqs"]
    assert got == [1, 2]                        # 版序 工作经历5 < 学习简历6


def test_book_order_table_orders_members():
    """没有页码 → 按册子的"版序"表排（志愿书：入党志愿 2 < 总支部审查 8 < 誓词 99）。"""
    c = seg.build_candidates([rec(64, pk="誓词"), rec(55, pk="入党志愿"),
                              rec(16, pk="总支部审查（审批）意见")])[0]
    assert c["seqs"] == [55, 16, 64]


def test_column_name_in_form_field_still_finds_the_book():
    """★ 模型有时把**栏目名**填进 form（实测："主要简历""本人经历"）—— 那也得认回册子，
    否则一本好好的册子会被拆成"册 + 一堆栏目份"。"""
    cands = seg.build_candidates([
        rec(3, form="主要简历", pk="主要简历"),          # 只有工人登记表有这个栏目
        rec(110, form="工人登记表", pk="封面"),
        rec(40, form="当选人民代表大会、政治协商会议、中国共产党及民主党派、群众团体代表大会代表、委员等情况",
            pk="技术等级(专业技术职务)变动情况和任职资格情况")])   # 只有干部履历表有
    by = {c["form"]: c for c in cands}
    assert set(by) == {"工人登记表", "干部履历表"}
    assert by["工人登记表"]["pages"] == 2 and by["干部履历表"]["pages"] == 1


def test_column_shared_by_two_books_is_not_forced_into_one():
    """"本人经历"在干部履历表和入党志愿书里都有 → 不能硬认成某一本（宁可留成独立页）。"""
    cands = seg.build_candidates([rec(39, form="本人经历", pk="本人经历")])
    assert len(cands) == 1 and cands[0]["form"] == "本人经历"   # 保持原样，不塞进任何册子


def test_letterhead_is_not_a_title():
    """信纸抬头（"XX公司稿纸"）是印在纸上的字，不能当材料名/册名。"""
    assert seg.is_letterhead("山西省运城汽车运输公司稿纸")
    assert seg.is_letterhead("山西省运城汽车运输公司公用笺")
    assert not seg.is_letterhead("干部履历表")


def test_rep_prefers_the_book_title_page():
    """代表页要挑"标题就是册名"的那页（封面），而不是照片号最小的那页。

    为什么重要：材料名/日期都从代表页取，定类也把它给模型看 —— 挑错页就会得到
    一条名叫"工作经历"的材料。
    """
    recs = [rec(3, form="干部履历表", pk="工作经历"),
            rec(88, form="干部履历表", pk="干部履历表", t="干部履历表"),
            rec(74, form="干部履历表", pk="其他需要说明的情况")]
    c = seg.build_candidates(recs)[0]
    assert c["rep"] == 88


# ── ⑧ 输出顺序与安全闸门 ────────────────────────────────────────

def test_output_order_is_stable_regardless_of_input_order():
    """份序号要**确定**：同一批页换个输入顺序，出来的份顺序应当一样。

    这一段（seg）还没定类，排不了"一→十"的目录序 —— 真正的目录序要到 build_mats
    （那里才有 category）才排。这里只钉住"有日期在前、无日期垫后、并列按照片号"这个
    确定的次序，免得下游两次跑出两种顺序（更正/导出会因此对不上）。
    """
    a = [rec(1, form="体检表", pk="体检表"),
         rec(2, form="职工工资变动表", pk="职工工资变动表", y=2014),
         rec(3, form="干部履历表", pk="封面")]
    c1 = seg.build_candidates(a)
    c2 = seg.build_candidates(list(reversed(a)))     # 换输入顺序
    assert [c["raw_seqs"][0] for c in c1] == [2, 1, 3]      # 有日期的在前，无日期按照片号
    assert [c["raw_seqs"][0] for c in c1] == [c["raw_seqs"][0] for c in c2]


def test_low_form_coverage_falls_back_to_legacy():
    """★ 安全闸门：模型整体没读出 form（覆盖率过低）→ 退回旧的顺序切份，并留一句说明。

    宁可退回旧行为，也不能让整卷塌成 126 条单页材料。
    """
    recs = [rec(1, t="入党申请书"), rec(2, t="入党申请书"),
            rec(3, t="体检表"), rec(4, t="体检表")]
    cands = seg.build_candidates(recs)
    assert len(cands) == 2                      # 老规则：相邻同标题并成一份
    assert "退回" in seg.LAST_NOTE and "覆盖率" in seg.LAST_NOTE


def test_missing_formbook_table_falls_back():
    """表册对照读不出来（文件丢了/语法坏了）→ 同样回退，不崩。"""
    import archive.skill.loader as loader
    old = loader.formbooks
    loader.formbooks = lambda: {}
    try:
        cands = seg.build_candidates([rec(1, t="入党申请书"), rec(2, t="入学登记表")])
    finally:
        loader.formbooks = old
    assert len(cands) == 2 and "退回" in seg.LAST_NOTE


def test_no_note_when_identity_path_works():
    """正常走身份归组时不该留回退说明（别让"降级提示"变成常态噪音）。"""
    seg.build_candidates([rec(1, form="干部履历表", pk="封面")])
    assert seg.LAST_NOTE == ""
