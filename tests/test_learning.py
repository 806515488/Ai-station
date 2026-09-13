"""口径学习的离线单测（不烧 key、不碰真 data/、**更不碰真的口径文件**）。

新手视角（Java 朋友版）：这一组测试守的是**一条会改变全站识别行为的链路** ——
"把学到的口径写进文件"。所以重点不是"功能好不好用"，而是"**它会不会改坏东西**"、
以及"**写口是不是只有一个**"：
  · 算出来的内容写下去，是不是就是想要的文件？（`test_proposal_content_round_trip`）
  · 摘文种的时候，同一行里的兄弟文种保住了吗？（`test_drop_keeps_sibling_tokens`）
  · 认不准的时候，是不是老老实实一个字都不删？（`test_ambiguous_conflict_deletes_nothing`）
  · **本仓是不是只有一条写这份文件的路径**？（`test_nothing_in_repo_writes_the_kouju_file`）
  · kouju 是不是真的"只算不写"？（`test_kouju_has_no_write_api`）

★ 关键夹具 `kouju_tmp`：把 `loader.SKILL_DIR` 指到一个临时目录，里面放**真口径文件的副本**。
  没有它，测试会写到仓库里那份真文件（那是产品资产）。
★ 落盘走 `learn.apply()`（它调 `kouju.write_text`）—— 这是全仓唯一的写口，
  被 archive 的 approve 类工具 `apply_learning` 调用，见下面那两条 AST 护栏。
"""
from __future__ import annotations

import ast
import difflib
import json
import shutil
from pathlib import Path

import pytest

from station import config

from archive.service import kouju, learn

_REPO = Path(__file__).resolve().parents[1]
_REAL_KOUJU = _REPO / "skills" / "archive" / "口径" / "文种对照.md"
_SKILL_TOOLS = _REPO / "skills" / "archive" / "code" / "archive_tools.py"

# 一段"长得像真文件"的小样本，用来测那些不该依赖真文件内容的行为
SAMPLE = """# 文种 → 小类 对照（口径 v1 · 2026-09-13）

> 说明。

## 用户已确认（最高优先，与下方冲突以本节为准）

## 基础口径

- 干部任免 / 任职 / 免职 / 任免审批表 / 职务变动 → **九-2**；出国(境) → **九-3**
- 体检 / 档案管理 / 人事争议 / 其他杂项 → **十**

## 已知待校准
- 人写的备注。
"""


@pytest.fixture
def kouju_tmp(tmp_path, monkeypatch):
    """把口径文件换到临时目录（内容复制自真文件），返回那份临时文件的路径。

    用法：测试里正常调 `kouju.*` / `learn.*`，读写落在 tmp 上，跑完连 tmp 一起消失。
    "写入"由测试自己调 `kouju_tmp.write_text(...)` 模拟（产品代码不写盘）。
    """
    from archive.skill import loader
    skill_dir = tmp_path / "skills" / "archive"
    (skill_dir / "口径").mkdir(parents=True)
    shutil.copy2(_REAL_KOUJU, skill_dir / "口径" / "文种对照.md")
    monkeypatch.setattr(loader, "SKILL_DIR", str(skill_dir))
    return skill_dir / "口径" / "文种对照.md"


def _section(text: str, title: str) -> str:
    """取出某一节的正文（测试里判断"某节变没变"用）。

    ★ 刻意**不**用 `text.split("## 基础口径")` 这种土办法 —— 文件开头的说明块里
      也提到了这两个节名，naive split 会切错地方（第一版就这么写错的）。
    """
    lines = text.split("\n")
    _h, bs, be = kouju._section_span(lines, title)
    return "\n".join(lines[bs:be]) if bs >= 0 else ""


def _changed_lines(old: str, new: str) -> list[int]:
    """逐行比对，返回**新文本里被改动/新增的行的下标**（0 基）。"""
    sm = difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"))
    out = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            out.extend(range(j1, j2))
    return out


def _ast(path):
    return ast.parse(Path(path).read_text(encoding="utf-8"))


# ── 真文件：结构对不对（迁移有没有做对）────────────────────────────

def test_real_file_has_three_sections():
    """真口径文件必须已有三节，且 `loader.hard()` 读到的内容包含新节与老条目。"""
    from archive.skill import loader
    lines = kouju.read_text().split("\n")
    for sec in (kouju.SECTION_CONFIRMED, kouju.SECTION_BASE, kouju.SECTION_CALIB):
        assert kouju._section_span(lines, sec)[0] >= 0, f"缺节：{sec}"
    hard = loader.hard()
    assert kouju.SECTION_CONFIRMED in hard and "九-2" in hard


def test_base_section_kept_all_original_entries():
    """迁移**不许丢条目**：基础口径里的条目数应与老版本的 12 条一致。"""
    lines = kouju.read_text().split("\n")
    _h, bs, be = kouju._section_span(lines, kouju.SECTION_BASE)
    entries = kouju._list_lines(lines, bs, be)
    assert len(entries) == 12
    assert any("入党申请书" in e for e in entries)
    assert any("其他杂项" in e for e in entries)


# ── ★ 写口唯一性（09-13 用户拍板的核心约束）────────────────────────

def test_kouju_write_is_called_only_by_learn_apply():
    """★ 写口唯一：调 `kouju.write_text` 的地方**只能**是 `learn.py` 的 `apply()`。

    「同一份文件只能有一条写路径」是这个功能反复吃过亏总结出来的 ——
    两条路径就会有一条不过闸，而那一条迟早被人走到。
    现在这条路径是：`archive.apply_learning`（approve 类，宿主批准闸）
    → `learn.apply` → `kouju.write_text`。**别在别处再开一个写口。**
    """
    callers = []
    for p in (_REPO / "src").rglob("*.py"):
        for fn in [n for n in ast.walk(_ast(p))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                f = getattr(node, "func", None)
                if (isinstance(f, ast.Attribute) and f.attr == "write_text"
                        and isinstance(f.value, ast.Name) and f.value.id == "kouju"):
                    callers.append((p.name, fn.name))
                    break
    assert callers == [("learn.py", "apply")], f"写口径文件的调用方不止 learn.apply：{callers}"


def test_kouju_really_has_the_write_api():
    """反过来确认一下：护栏断言的那个函数确实在（防"因为函数改名而永远绿灯"）。"""
    assert callable(getattr(kouju, "write_text", None))


def test_learning_tools_have_the_right_risk():
    """★ `apply_learning` **必须是 approve 类** —— 它是全仓唯一能写口径文件的工具。

    少了这个 risk，它就变成"模型可以自己调、不用问用户"，整个
    "必须用户确认"的设计当场失效。**这条只能靠看注册表抓** —— 单测直接调工具函数
    是绕过 risk 字段的（09-13 真浏览器实测踩到：漏写 risk 后点确认直接落盘、
    批准弹窗根本不出现，而单测全绿）。
    """
    from station.skills.registry import get_registry
    ts = {t.leaf(): t for t in get_registry().get("archive").tools}
    assert ts["apply_learning"].risk == "approve"
    # 顺带确认另一个危险工具没被改坏（出件也要批准）
    assert ts["export"].risk == "approve"
    # 而"改类"这类日常动作**不该**弹确认（弹了就没人愿意用了）
    for leaf in ("set_category", "merge", "split", "list_learning", "reconcile"):
        assert ts[leaf].risk == "auto", f"{leaf} 不该是 approve 类"


def test_skill_tools_do_not_write_files_directly():
    """技能工具不许**自己**写文件（`open` / `write_text`），只能通过 `learn.apply`。"""
    tree = _ast(_SKILL_TOOLS)
    for node in ast.walk(tree):
        f = getattr(node, "func", None)
        if isinstance(f, ast.Attribute) and f.attr in ("write_text", "open"):
            pytest.fail(f"技能工具里出现了直接写文件的调用：{f.attr}")


# ── ensure_sections / insert_confirmed ──────────────────────────────

def test_ensure_sections_is_idempotent():
    """结构已就位 → 再跑一次必须**逐字节相同**（幂等）。"""
    assert kouju.ensure_sections(SAMPLE) == SAMPLE
    assert kouju.ensure_sections(kouju.ensure_sections(SAMPLE)) == SAMPLE


def test_ensure_sections_migrates_flat_file():
    """老格式（没有两节、直接一串条目）→ 补出两节，原条目全落进「基础口径」。"""
    flat = "# 标题\n\n> 说明。\n\n- 甲 → **一**\n- 乙 → **二**\n"
    got = kouju.ensure_sections(flat)
    lines = got.split("\n")
    assert kouju._section_span(lines, kouju.SECTION_CONFIRMED)[0] >= 0
    _h, bs, be = kouju._section_span(lines, kouju.SECTION_BASE)
    assert kouju._list_lines(lines, bs, be) == ["- 甲 → **一**", "- 乙 → **二**"]


def test_insert_confirmed_only_touches_its_section():
    """★ 核心纪律：追加条目时，改动行**全部落在「用户已确认」节内**。"""
    new = kouju.insert_confirmed(SAMPLE, "- 《考核表》→ **三**（2026-09-13 用户确认）")
    lines = new.split("\n")
    _h, bs, be = kouju._section_span(lines, kouju.SECTION_CONFIRMED)
    changed = _changed_lines(SAMPLE, new)
    assert changed and all(bs <= i < be for i in changed), f"改到了本节之外：{changed}"
    assert any("《考核表》" in e for e in kouju.confirmed_entries(new))


# ── 冲突侦测 ────────────────────────────────────────────────────────

def test_find_conflict_picks_the_longest_matching_token():
    """`干部任免审批表` 同时含 `干部任免`(4字) 与 `任免审批表`(5字) → 取**最长**那个。"""
    c = kouju.find_conflict(SAMPLE, "干部任免审批表", "十")
    assert c is not None and c["token"] == "任免审批表" and c["old"] == "九-2"


def test_find_conflict_none_when_same_category():
    """本来就在这一类 → 不叫冲突（不能把一条已经正确的口径删掉）。"""
    assert kouju.find_conflict(SAMPLE, "任免审批表", "九-2") is None


def test_find_conflict_none_for_unrelated_title():
    assert kouju.find_conflict(SAMPLE, "优秀党员推荐审批表", "六") is None


def test_ambiguous_conflict_deletes_nothing():
    """同一文种在两处出现且类别不同 = 口径本身有歧义 → **交给人，一个字都不删**。"""
    two = SAMPLE.replace("- 体检 / 档案管理",
                         "- 任免审批表已废止 → **十**\n- 体检 / 档案管理")
    assert kouju.find_conflict(two, "任免审批表", "七") is None


def test_short_tokens_do_not_falsely_match():
    """两字词（工资/体检）不许靠"包含"误命中 —— 长度门槛挡的就是它。"""
    assert kouju.find_conflict(SAMPLE, "工资变动审批表", "九-1") is None


# ── 片段级摘除 ──────────────────────────────────────────────────────

def test_drop_keeps_sibling_tokens():
    """★ 核心纪律：摘掉一个文种，同一行剩下的文种**逐字保住**（含空格分隔符）。"""
    c = kouju.find_conflict(SAMPLE, "干部任免审批表", "十")
    got, ok = kouju.drop_conflict(SAMPLE, c)
    assert ok
    assert "- 干部任免 / 任职 / 免职 / 职务变动 → **九-2**；出国(境) → **九-3**" in got
    assert "任免审批表" not in _section(got, kouju.SECTION_BASE)


def test_drop_aborts_when_line_changed():
    """文件被手工改过、提案里记的那一行找不到了 → **不删**，返回 False 让调用方降级。"""
    c = kouju.find_conflict(SAMPLE, "干部任免审批表", "十")
    edited = SAMPLE.replace("任免审批表 / 职务变动", "职务变动")
    got, ok = kouju.drop_conflict(edited, c)
    assert got == edited and ok is False


def test_render_entry_format():
    assert kouju.render_entry("X", "十", "2026-09-13") == "- 《X》→ **十**（2026-09-13 用户确认）"
    assert "2 次修正" in kouju.render_entry("X", "十", "2026-09-13", count=2)


# ── ★ 提案：算出来的内容能不能直接用 ───────────────────────────────

def test_proposal_content_round_trip(kouju_tmp):
    """★ 最重要的一条：提案给的 `content` **写下去就是想要的文件**。

    这条钉住"模型只需要原样转发"这个前提 —— 内容里必须已经包含新条文、
    并且冲突文种已经被摘掉、兄弟文种还在。模型自己不需要做任何加工。
    """
    prop = learn.propose([{"title": "干部任免审批表", "category": "十", "old": "九-2"}],
                         SAMPLE)
    assert prop["file"] == kouju.REL_PATH
    got = prop["content"]
    assert any("干部任免审批表" in e for e in kouju.confirmed_entries(got))
    base = _section(got, kouju.SECTION_BASE)
    assert "任免审批表" not in base and "干部任免 / 任职" in base
    assert prop["items"][0]["dropped"] is True
    assert "摘掉" not in got          # 别把"说明"混进要写的内容里


def test_proposal_without_conflict_only_appends(kouju_tmp):
    """没有冲突就只追加，基础口径**一个字节都不动**。"""
    prop = learn.propose([{"title": "优秀党员推荐审批表", "category": "六", "old": "七"}],
                         SAMPLE)
    assert _section(prop["content"], kouju.SECTION_BASE) == _section(SAMPLE, kouju.SECTION_BASE)
    assert prop["items"][0]["drop"] is None


def test_proposal_multiple_changes_apply_in_order(kouju_tmp):
    """多条改动 → **一份**内容（后一条在前一条改完的基础上改）。"""
    prop = learn.propose([
        {"title": "干部任免审批表", "category": "十", "old": "九-2"},
        {"title": "体检表", "category": "五", "old": "十"},
    ], SAMPLE)
    got = prop["content"]
    assert len(prop["items"]) == 2
    assert "- 《体检表》→ **五**" in got and "- 《干部任免审批表》→ **十**" in got


def test_apply_writes_and_classify_sees_it(kouju_tmp):
    """★ `learn.apply` 真写盘 → 文件就是算出来的那份，且 `loader.hard()` 读得到新条文。"""
    from archive.skill import loader
    res = learn.apply([{"title": "干部任免审批表", "category": "十", "old": "九-2"}])
    assert res["written"] and res["dropped"] == ["任免审批表"]
    assert "《干部任免审批表》" in kouju.read_text()
    assert "《干部任免审批表》" in loader.hard()      # 注入口径 → 下次 classify 自动生效


def test_apply_refuses_broken_content(kouju_tmp, monkeypatch):
    """★ 算出来的内容结构不对（缺节）→ **拒写**，原文件一个字节不动。

    口径文件是**全站共享**的：写坏了 `loader.hard()` 就读不出内容，**每一卷的分类
    都跟着坏**。所以宁可报错让模型重来，也不要写进去一个结构不对的东西。
    """
    before = kouju_tmp.read_bytes()
    monkeypatch.setattr(kouju, "looks_valid", lambda t: False)
    with pytest.raises(ValueError):
        learn.apply([{"title": "干部任免审批表", "category": "十"}])
    assert kouju_tmp.read_bytes() == before


def test_apply_with_nothing_to_do_is_a_noop(kouju_tmp):
    """""空改动"不该碰文件（免得白白改一次换行/时间戳）。"""
    before = kouju_tmp.read_bytes()
    assert learn.apply([]) == {"written": [], "dropped": []}
    assert kouju_tmp.read_bytes() == before


# ── from_correction：什么时候该静默 ────────────────────────────────

def _correct(before_cat="九-2", after_cat="十", title="干部任免审批表"):
    return {"title": title, "category": before_cat}, {"category": after_cat}


def test_from_correction_proposes(kouju_tmp):
    before, after = _correct()
    prop = learn.from_correction(before, after)
    assert prop and prop["items"][0]["category"] == "十"
    assert prop["items"][0]["drop"]["token"] == "任免审批表"


def test_same_category_correction_is_silent(kouju_tmp):
    before, after = _correct("九-2", "九-2")
    assert learn.from_correction(before, after) is None


def test_no_title_or_no_category_is_ignored(kouju_tmp):
    assert learn.from_correction({"title": ""}, {"category": "十"}) is None
    assert learn.from_correction({"title": "甲"}, {"category": ""}) is None


def test_already_learned_is_silent(kouju_tmp):
    """★ 学过的口径**不再问** —— 判断依据是**文件本身**（「用户已确认」节里有没有）。"""
    before, after = _correct()
    prop = learn.from_correction(before, after)
    kouju_tmp.write_text(prop["content"], encoding="utf-8")
    assert learn.from_correction(before, after) is None


def test_user_handwritten_entry_also_silences(kouju_tmp):
    """用户**手工**把一条口径写进文件后，系统也不该再问一遍（去重不依赖任何库）。"""
    text = kouju.insert_confirmed(kouju.read_text(),
                                  kouju.render_entry("干部任免审批表", "十", "2026-09-13"))
    kouju_tmp.write_text(text, encoding="utf-8")
    before, after = _correct()
    assert learn.from_correction(before, after) is None


# ── pending_from_ledger：无状态的"还有什么没学" ────────────────────

def _mats():
    """两份材料：考核表（三，第1张）、调资表（九-1，第2张）。"""
    def m(uid, cat, title, seq):
        return {"uid": uid, "seq": seq, "category": cat, "main": cat.split("-")[0],
                "title": title, "title_src": "engine", "date": None, "copies": 1,
                "pages": 1, "members": [seq], "dup_pages": [], "assigned_pages": [seq],
                "evidence": "", "doubt": False, "verdict": "ok"}
    return [m("m1", "三", "考核表", 1), m("m2", "九-1", "调资表", 2)]


def _proj(tmp_path):
    """手搓一个小卷（3 页 / 2 份材料）并把"手头这卷"登记好，返回 (pj, ctx, mats)。"""
    from station.core.context import Context
    from archive.service import interactive as it
    root = tmp_path / "卷"
    root.mkdir()
    pj = root / "project.json"
    pj.write_text(json.dumps({"name": "卷", "records_file": "photos.json"},
                             ensure_ascii=False), encoding="utf-8")
    (root / "photos.json").write_text(json.dumps(
        [{"seq": i, "path": f"x{i}.jpg", "md5": f"m{i}", "ocr": {}} for i in (1, 2, 3)],
        ensure_ascii=False), encoding="utf-8")
    mats = _mats()
    it.save_state(str(pj), mats, [], "测试")
    ctx = Context(skill_id="archive", data_dir=tmp_path / "skilldata", user_id="u1")
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    (ctx.data_dir / "current.json").write_text(
        json.dumps({"project": str(pj), "person": "测试"}), encoding="utf-8")
    return str(pj), ctx, mats


def test_pending_from_ledger_derives_from_existing_data(tmp_path, kouju_tmp):
    """★ 不需要任何新表：从**账本**（已有的 corrections）+ **口径文件**现算出待学项。"""
    from archive.service import interactive as it
    pj, _ctx, _m = _proj(tmp_path)
    it.op_set_category(pj, "m1", "九-2")          # 考核表 三 → 九-2（记进账本）
    prop = learn.pending_from_ledger(pj)
    assert [i["category"] for i in prop["items"]] == ["九-2"]
    assert "考核表" in prop["items"][0]["title"]


def test_pending_from_ledger_empty_when_already_learned(tmp_path, kouju_tmp):
    """已经学进去的，不该再出现在"待学"里。"""
    from archive.service import interactive as it
    pj, _ctx, _m = _proj(tmp_path)
    it.op_set_category(pj, "m1", "九-2")
    prop = learn.pending_from_ledger(pj)
    kouju_tmp.write_text(prop["content"], encoding="utf-8")
    assert learn.pending_from_ledger(pj) == {}


# ── 渲染契约：一个工具可以同时发多张卡 ─────────────────────────────

def _run_tool(out):
    from station.core.agent import run_tool
    from station.core.context import Context
    from station.core.tool import Tool
    ctx = Context()
    ret = run_tool(ctx, Tool(name="demo.x", description="d", run=lambda c: out), {})
    return ret, [e.data["card"] for e in ctx.events if e.type == "render"]


def test_run_tool_list_render_emits_every_card():
    """list render → 每张卡各发一个事件，顺序不变；模型仍只拿到 text。"""
    ret, cards = _run_tool({"text": "给模型看",
                            "render": [{"type": "overview"}, {"type": "learn-card"}]})
    assert ret == "给模型看"
    assert [c["type"] for c in cards] == ["overview", "learn-card"]


def test_run_tool_list_render_skips_junk():
    """list 里混进非 dict 的垃圾 → 跳过它，别把整轮工具结果搞崩。"""
    ret, cards = _run_tool({"text": "t", "render": [{"type": "overview"}, "垃圾", None]})
    assert ret == "t" and [c["type"] for c in cards] == ["overview"]


def test_run_tool_single_dict_render_unchanged():
    """回归：单个 dict 的 render 行为**一个字都没变**（老卡片全走这条路）。"""
    ret, cards = _run_tool({"text": "t", "render": {"type": "page-card"}})
    assert ret == "t" and [c["type"] for c in cards] == ["page-card"]


def test_run_tool_plain_values_unchanged():
    """回归：不带 render 的字典 / 裸字符串，仍旧原样 `str(out)` 给模型。"""
    ret, cards = _run_tool({"a": 1})
    assert cards == [] and ret == "{'a': 1}"
    ret, cards = _run_tool("就是一句话")
    assert cards == [] and ret == "就是一句话"


# ── 端到端（工具层）：改一次类，屏幕上多一张卡、给模型的内容可直写 ──

def _archive_tool(leaf):
    from station.skills.registry import get_registry
    return next(t for t in get_registry().get("archive").tools if t.leaf() == leaf)


def test_set_category_emits_overview_then_learn_card(tmp_path, kouju_tmp):
    """★ 一次改类 → 工具返回**两张卡**（全景第一 + 学习卡），且文字里带着可直写的内容。"""
    _pj, ctx, _m = _proj(tmp_path)
    out = _archive_tool("set_category").run(ctx, target="m1", category="九-2")
    assert isinstance(out["render"], list)
    assert out["render"][0]["type"] == "overview"        # 09-12 那条修复不许丢
    assert out["render"][1]["type"] == "learn-card"
    assert out["render"][1]["items"][0]["category"] == "九-2"
    # ★ 给模型的文字里要**说清怎么落盘**（调 apply_learning），但**不带文件内容** ——
    #   条文由技能自己算，模型不参与生成内容，也就写不歪一行。
    assert "apply_learning" in out["text"]
    assert "《考核表》→ **九-2**" in out["text"]
    assert "content" not in out["render"][1]        # 卡片/工具结果都不该带整份文件


def test_set_category_twice_second_time_is_quiet(tmp_path, kouju_tmp):
    """第二次改同一个：先把第一条写进文件（模拟用户确认），再改 → 不再出学习卡。"""
    _pj, ctx, _m = _proj(tmp_path)
    tool = _archive_tool("set_category")
    out = tool.run(ctx, target="m1", category="九-2")
    learn.apply([{"title": "考核表", "category": "九-2", "old": "三"}])   # 模拟用户确认写入
    out2 = tool.run(ctx, target="m1", category="九-2")
    assert out2["render"]["type"] == "overview"           # 退回单张卡的形状


def test_learning_failure_does_not_break_the_correction(tmp_path, monkeypatch):
    """★ 学习是加分项：它坏掉**绝不能**影响"改类"这件正事。"""
    _pj, ctx, _m = _proj(tmp_path)
    from archive.service import learn as _learn
    monkeypatch.setattr(_learn, "from_correction",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _archive_tool("set_category").run(ctx, target="m1", category="九-2")
    assert "已改" in out["text"]
    assert out["render"]["type"] == "overview"


def test_list_learning_tool(tmp_path, kouju_tmp):
    """用户问"学什么了" → 卡片 + 可直接写的内容。"""
    from archive.service import interactive as it
    pj, ctx, _m = _proj(tmp_path)
    it.op_set_category(pj, "m1", "九-2")
    out = _archive_tool("list_learning").run(ctx)
    assert out["render"]["items"][0]["category"] == "九-2"
    assert "apply_learning" in out["text"]


def test_list_learning_empty(tmp_path, kouju_tmp):
    _pj, ctx, _m = _proj(tmp_path)
    out = _archive_tool("list_learning").run(ctx)
    assert out["render"]["items"] == []


# ── 入口 B：终版 Excel 对账 ─────────────────────────────────────────

def _export(mats, tmp_path, person="测试"):
    from archive.export import excel
    return excel.export(mats, person, str(tmp_path / "out"))


def _export_uploaded(mats, person="测试"):
    """导到「上传终版目录」那个目录里 —— 对账只认那里的文件（见 `_safe_upload_path`）。"""
    from archive.export import excel
    d = config.sub("reconcile")
    d.mkdir(parents=True, exist_ok=True)
    return excel.export(mats, person, str(d))


def test_read_xlsx_matches_export(tmp_path):
    """★ 读回器与导出器必须**严丝合缝**：导出再读回，类别/名称/页号集合完全一致。

    这条钉的是 `learn.read_xlsx_materials` 与 `export/excel.py` 列结构之间的耦合 ——
    哪天 excel.py 的骨架/单元格布局改了，这里立刻红。
    """
    mats = _mats()
    rows = learn.read_xlsx_materials(_export(mats, tmp_path))
    assert len(rows) == len(mats)
    assert {(r["cat"], r["title"]) for r in rows} == {(m["category"], m["title"]) for m in mats}
    assert {frozenset(r["seqs"]) for r in rows} == {frozenset(m["members"]) for m in mats}


def test_read_xlsx_without_trace_still_reads(tmp_path):
    """用户把隐藏的 `_trace` 表删了也要能读（退到"按名字认"那条路）。"""
    from openpyxl import load_workbook
    xlsx = _export(_mats(), tmp_path)
    wb = load_workbook(xlsx)
    del wb["_trace"]
    wb.save(xlsx)
    rows = learn.read_xlsx_materials(xlsx)
    assert [r["title"] for r in rows] and all(r["seqs"] is None for r in rows)


def test_diff_reports_category_change(tmp_path):
    """用户把某一行的类别挪到别的类 → 报一条 cat_changed（其余照旧匹配上）。"""
    user = _mats()
    user[0]["category"] = "九-2"
    rows = learn.read_xlsx_materials(_export(user, tmp_path))
    d = learn.diff_materials(_mats(), rows)
    assert d["cat_changed"] == [{"title": "考核表", "from": "三", "to": "九-2"}]
    assert d["missing"] == [] and d["added"] == []


def test_diff_reports_missing_and_added(tmp_path):
    """用户这版少一份/多一份 → 如实报出来，**不硬凑**。"""
    user = [dict(_mats()[1])]
    extra = dict(_mats()[0]); extra["uid"] = "m9"; extra["title"] = "体检表"
    extra["members"] = [3]
    user.append(extra)
    rows = learn.read_xlsx_materials(_export(user, tmp_path))
    d = learn.diff_materials(_mats(), rows)
    assert [m["title"] for m in d["missing"]] == ["考核表"]
    assert [a["title"] for a in d["added"]] == ["体检表"]


def test_diff_reports_rename(tmp_path):
    """名称改了（页号没变 → 靠页号仍能配上）→ 报一条 renamed，不进提案。"""
    user = _mats()
    user[0]["title"] = "年度考核表"
    rows = learn.read_xlsx_materials(_export(user, tmp_path))
    d = learn.diff_materials(_mats(), rows)
    assert d["renamed"] == [{"from": "考核表", "to": "年度考核表"}]
    assert d["cat_changed"] == []


def test_reconcile_records_ledger_and_proposes(tmp_path, kouju_tmp):
    """走完整对账：类别差异既进账本（标 src=excel）又算成提案。"""
    pj, _ctx, _m = _proj(tmp_path)
    user = _mats()
    user[0]["category"] = "九-2"
    res = learn.reconcile(pj, _export_uploaded(user), "u1")

    assert len(res["diffs"]["cat_changed"]) == 1
    assert res["proposal"]["items"][0]["category"] == "九-2"
    assert "终版" in res["proposal"]["items"][0]["why"]     # 来源说法是"你改后的终版目录里"
    assert kouju.REL_PATH in res["proposal"]["file"]

    from archive.service import interactive as it
    led = it.read_corrections(pj)
    assert led[-1]["kind"] == "set_category" and led[-1]["src"] == "excel"


def test_reconcile_refuses_paths_outside_the_upload_dir(tmp_path, kouju_tmp):
    """★ 对账只认「上传终版目录」里的文件 —— 模型给的 path 不能指到别处去。

    服务器上的文件读取原来有一道路径闸（`tools/guard.py`），它随文件工具一起删了；
    而这个工具的参数是**模型给的自由文本**。不收紧就等于：一句话就能把服务器上
    任意 xlsx 的内容读进对话（09-13 review 抓到的口子）。
    """
    pj, _ctx, _m = _proj(tmp_path)
    outside = _export(_mats(), tmp_path)              # 导到 out/ 而不是上传目录
    with pytest.raises(ValueError, match="上传"):
        learn.reconcile(pj, outside, "u1")
    with pytest.raises(ValueError, match="上传"):
        learn.reconcile(pj, str(tmp_path / ".." / "别处.xlsx"), "u1")
    # 同前缀的兄弟目录也不能放行（`/a/bc` 不是 `/a/b` 的子路径 —— guard.py 记过这个坑）
    sib = str(config.sub("reconcile")) + "2"
    with pytest.raises(ValueError, match="上传"):
        learn.reconcile(pj, sib + "/x.xlsx", "u1")


# ── 09-13 review 抓到的那批：每条都是"单测看不见"的缺陷 ─────────────

def test_parenthesised_group_is_never_chopped(kouju_tmp):
    """★ 括号组是一个整体，**摘不掉** —— 那就一个字不动（宁可漏删也别把行改残）。

    现网口径里就有 `党内表彰(优秀党员/先进支部推荐审批) → **六**` 这种行。按括号里的
    `/` 去切、再按半截去摘，会把那一行改成 `党内表彰(优秀党员 → **六**`（括号不配对、
    不再是一条合法条目）—— 而这份文件决定每一卷的分类。
    """
    frag = "- 党内表彰(优秀党员/先进支部推荐审批) → **六**"
    text = SAMPLE.replace("- 体检 / 档案管理 / 人事争议 / 其他杂项 → **十**", frag)
    # ① 不报成冲突：预览卡就不会承诺一个做不到的"同时摘掉"
    assert kouju.find_conflict(text, "先进支部推荐审批", "七") is None
    # ② 就算硬喂一个 drop 进来，也一个字节都不动
    got, ok = kouju.drop_conflict(text, {"line": frag, "frag": frag,
                                         "token": "先进支部推荐审批", "old": "六"})
    assert ok is False
    assert "优秀党员" in got and "先进支部推荐审批)" in got


def test_drop_conflict_reports_false_when_nothing_changed(kouju_tmp):
    """★ 「找到了行、却一个字节没改」必须返回 False。

    否则上层会告诉用户「已摘掉 X」，而旧规则还在文件里 —— 下一卷照样按旧的判（review
    抓到的假报告）。返回 False 会让调用方降级成"只追加"并如实说明。
    """
    line = "- 体检 / 档案管理 / 人事争议 / 其他杂项 → **十**"
    got, ok = kouju.drop_conflict(SAMPLE, {"line": line, "frag": line,
                                           "token": "根本不存在的文种", "old": "十"})
    assert ok is False and got == SAMPLE


def test_ledger_keeps_only_the_latest_conclusion_per_title(tmp_path, kouju_tmp):
    """★ 同一个文种改过好几次时，**只有最后一个结论**算数。

    按 (文种, 类别) 去重会让**被推翻的早先结论**也存活：用户把考核表 三→九-1 又反悔
    改成 九-1→九-2（中间没确认过），两条键不同、于是会一起写进那个自称"冲突以本节为准"
    的节里 —— 本该裁决冲突的地方自己先自相矛盾（review 抓到）。
    """
    from archive.service import interactive as it
    pj, _ctx, _m = _proj(tmp_path)
    it.op_set_category(pj, "m1", "九-1")      # 考核表 三 → 九-1
    it.op_set_category(pj, "m1", "九-2")      # 反悔 → 九-2
    prop = learn.pending_from_ledger(pj)
    assert [i["category"] for i in prop["items"]] == ["九-2"]


def test_category_is_normalised_before_writing(kouju_tmp):
    """★ 类别码必须**归一**再写。模型给 `9-2` 这种写法时，写进去以后 `cat_of()` 再也
    读不回来 → `already_confirmed` 恒为假 → 同一条提案反复弹、每次确认再堆一条重复行。"""
    prop = learn.propose([{"title": "任免审批表", "category": "9-2", "old": "三"}])
    assert prop["items"][0]["category"] == "九-2"
    assert "**九-2**" in prop["items"][0]["entry"]


def test_bogus_category_is_refused(kouju_tmp):
    """归不了的类别码整条拒掉（跟 `t_set_category` 一个口径），别把垃圾写进共享文件。"""
    with pytest.raises(ValueError):
        learn.propose([{"title": "任免审批表", "category": "乱七八糟", "old": "三"}])


def test_title_cannot_inject_lines(kouju_tmp):
    """★ 文种名里的换行能往共享文件里**注入新行**（甚至插一个 `## 基础口径` 标题），
    而写前只检查三个节标题在不在 —— 照样通过。所以名字必须先清干净。"""
    prop = learn.propose([{"title": "甲》→ **一**\n\n## 基础口径", "category": "十",
                           "old": "三"}])
    entry = prop["items"][0]["entry"]
    assert "\n" not in entry and entry.count("《") == 1 and entry.count("》") == 1


def test_apply_backs_up_before_overwriting(kouju_tmp):
    """★ 写前把旧内容存一份进文件区 —— 这次改的是**全站共享**的口径文件，
    判错了得有后悔药（原来管这件事的全局写工具 09-13 删了，这条不变量得重建）。"""
    from station.files import store as fsstore
    learn.apply([{"title": "干部任免审批表", "category": "十", "old": "九-2"}])
    assert "文种对照-改写前.md" in [d.get("name") for d in fsstore.iter_files("")]


def test_write_switch_still_guards_the_only_writer(kouju_tmp, monkeypatch):
    """★ `STATION_FS_WRITE=0` 必须**仍然能关写** —— 写口换到技能里了，开关得跟着挂过去。

    不然运维照旧配这个变量以为实例只读，实际上口径文件照样能被改写（review 抓到）。"""
    monkeypatch.setenv("STATION_FS_WRITE", "0")
    before = kouju_tmp.read_bytes()
    with pytest.raises(PermissionError):
        learn.apply([{"title": "干部任免审批表", "category": "十", "old": "九-2"}])
    assert kouju_tmp.read_bytes() == before


def test_diff_reports_duplicate_named_rows(tmp_path):
    """★ 用户那版里两份**同名**材料，不能都配给同一行 AI 材料。

    否则多出来的那份既不进 `added` 也不进 `missing`，从差异里凭空消失，
    `diff_note` 还会说"完全一致"（review 抓到）。"""
    user = [dict(_mats()[0]), dict(_mats()[0]), dict(_mats()[1])]   # 考核表 ×2
    for i, r in enumerate(user):
        r["members"] = [90 + i]          # 页号全变 → 第一轮配不上，只能按名字配
    rows = learn.read_xlsx_materials(_export(user, tmp_path))
    d = learn.diff_materials(_mats(), rows)
    assert len(d["added"]) == 1, "用户那版多出来的重复行必须如实报出来"


def test_already_confirmed_is_not_written_again(tmp_path, kouju_tmp):
    """★ 已经学过的**不再写** —— 三条入口（改类/账本/显式传参）都得盖住。

    这条原来只在账本那条路上做，显式传 title+category 的那条漏了，于是重复确认会往
    「用户已确认」节里堆重复行、甚至堆出自相矛盾的条文（review 抓到）。"""
    prop = learn.propose([{"title": "干部任免审批表", "category": "十", "old": "九-2"}])
    kouju_tmp.write_text(prop["content"], encoding="utf-8")
    again = learn.propose([{"title": "干部任免审批表", "category": "十", "old": "九-2"}])
    assert again["items"] == []                      # 没什么可写的了
    assert learn.apply([{"title": "干部任免审批表", "category": "十", "old": "九-2"}]) \
        == {"written": [], "dropped": []}


def test_apply_learning_rejects_half_arguments(tmp_path, kouju_tmp):
    """★ 只给 title 不给 category = 这次调用本身就不明确。

    **不能**退化成"把这卷所有没学的都写进去" —— 那等于把一次被批准的小改动偷偷放大
    成一批（review 抓到）。"""
    from archive.service import interactive as it
    pj, ctx, _m = _proj(tmp_path)
    it.op_set_category(pj, "m1", "九-2")             # 账本里攒了一条待学
    tool = _archive_tool("apply_learning")
    with pytest.raises(ValueError, match="要么都给"):
        tool.run(ctx, title="考核表")                 # 只有 title


def test_reconcile_dir_is_outside_the_photo_upload_tree():
    """★ 对账上传目录**不能**放在 `uploads/` 底下。

    `POST /api/photos` 会调 `purge_old` 把 `uploads/` 的每个子目录 rmtree 掉（重传照片
    是常规流程）—— 塞在里面的话，用户一重传照片，刚传上来的终版目录就被删了，
    而 agent 手上还攥着那个路径（review 抓到的真 bug）。"""
    from station import config
    up = str(config.sub("uploads"))
    rec = str(config.sub("reconcile"))
    assert not rec.startswith(up), "对账目录落在了会被 purge_old 清掉的树里"


def test_reconcile_without_state_is_refused(tmp_path, kouju_tmp):
    """没识别过的卷不让对账（先给一句人话，别抛栈）。"""
    root = tmp_path / "空卷"; root.mkdir()
    pj = root / "project.json"
    pj.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        learn.reconcile(str(pj), _export(_mats(), tmp_path), "u1")


def test_reconcile_endpoint_requires_ownership():
    """上传端点：没登录 401；不是自己的卷 404（防探测，不用 403）。"""
    from fastapi.testclient import TestClient
    from station.app.server import app
    c = TestClient(app)
    r = c.post("/api/archive/reconcile",
               files={"file": ("a.xlsx", b"xx", "application/vnd.ms-excel")},
               data={"project": "/别人的卷"})
    assert r.status_code == 401

    c.post("/api/auth/register", json={"name": "站长", "password": "pw"})
    r = c.post("/api/archive/reconcile",
               files={"file": ("a.xlsx", b"xx", "application/vnd.ms-excel")},
               data={"project": "/不属于我的卷/project.json"})
    assert r.status_code == 404


def test_confirm_endpoint_is_gone():
    """★ 09-13 起不再有 /api/learning/confirm —— 写口收敛到全局写工具那一条路径。

    直接查路由表（不发请求）：POST 打不存在的路径会落到静态目录挂载上、拿到 405，
    那不是"端点还在"的证据，拿它断言反而看不住这件事。
    """
    from station.app.server import app
    assert "/api/learning/confirm" not in {r.path for r in app.routes}
