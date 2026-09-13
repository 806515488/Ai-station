"""archive 交互式整理离线单测：OCR 缓存 / 交互操作 / 修正账本 / 口径提案。

新手视角（Java 朋友版）：这是上面 station 测试的姊妹篇——把档案侧的几条
"行为契约"锁住，全部离线（不调真模型）。看懂这些用例 = 知道交互式整理
每一层保证什么：
  1) OCR 缓存：同图+同通道+同提示词必命中；换通道/换提示词自然失效（互不覆盖）
  2) 识别执行器：跑完现场落 DB、任务 done（确认门已删，对话式改造 09-07）
  3) 交互操作：改类合法/非法、并份页并集、拆份标存疑、采用OCR刷新标题
  4) 账本：每次操作记一条，聚合器能把"多次同类改判"聚成口径提案
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import archive.storage.ocr_cache as oc
from archive.service import interactive as it


# ── ① OCR 缓存 ───────────────────────────────────────────────────

@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """把缓存目录指到临时目录（不污染真 data/）。"""
    d = tmp_path / "ocr_cache"
    monkeypatch.setattr(oc, "CACHE_DIR", str(d))
    return d


def test_ocr_cache_hit_and_isolation(cache_dir):
    """同键命中；换通道/换提示词不命中且互不覆盖。"""
    assert oc.lookup("md5a", "qwen", "P1") is None          # 先查：没有
    oc.store("md5a", "qwen", "P1", {"mark": {"t": "履历表"}}, model="m1")
    assert oc.lookup("md5a", "qwen", "P1")["mark"]["t"] == "履历表"
    oc.store("md5a", "glm", "P1", {"mark": {"t": "GLM版"}}, model="m2")
    assert oc.lookup("md5a", "qwen", "P1")["mark"]["t"] == "履历表"   # 不互踩
    assert oc.lookup("md5a", "glm", "P1")["mark"]["t"] == "GLM版"
    assert oc.lookup("md5a", "qwen", "P2") is None           # 提示词变 → 失效
    assert oc.clear() == 2                                   # 清空


def test_mark_cache_hit_keeps_full_card(cache_dir, monkeypatch):
    """建档命中缓存后，record.ocr 必须仍是“完整包裹卡”（mark 键在）。

    回归背景（09-09 真卷发现）：node_mark 缓存命中路径曾返回 hit["mark"]
    （内层裸卡），比真调模型路径（返回 _parse_mark 的完整包裹
    {mark,title,date,texts,cls}）少一层 → 切份 seg 读 ocr.mark 全是空 →
    整卷被误判成“一式N份复本”→ 材料 0 行。此用例锁住两条路径同结构。
    """
    from archive.engine import graph, seg
    from archive.skill import loader

    prompt = loader.mark()                       # 与 node_mark 同一份建档提示词（缓存键之一）
    # 预先 store 两张“不同内容”的完整包裹卡（模拟此前真调模型时写进缓存的形态）
    for md5, t, s in (("md5-1", "入党申请书", "申请入党意愿"),
                      ("md5-2", "主要简历", "个人主要经历")):
        oc.store(md5, "qwen", prompt,
                 {"mark": {"t": t, "doc": None, "is_first": True, "is_cont": None,
                           "date": None, "kind": "表格式", "s": s, "u": False},
                  "title": t, "date": None, "texts": [s],
                  "cls": {"category": None, "doubt": False, "doc": None,
                          "evidence": s}},
                 model="m1")

    # 模型对象照常创建（node_mark 查缓存前就会 make_model，这是原设计），
    # 但把 invoke 换成“一调就炸”：真调模型（烧钱点）只发生在缓存未命中时
    class _NoInvoke:
        def invoke(self, *a, **k):
            raise AssertionError("缓存已命中却调用了视觉模型")

    monkeypatch.setattr(graph.providers, "make_model", lambda *a, **k: _NoInvoke())
    monkeypatch.setattr(graph.providers, "_pick", lambda kind: "qwen")  # 缓存键要用的通道名

    recs = [{"seq": 1, "path": "x1.jpg", "md5": "md5-1", "ocr": {}, "flags": []},
            {"seq": 2, "path": "x2.jpg", "md5": "md5-2", "ocr": {}, "flags": []}]
    out = graph.node_mark({"records": recs})
    assert out["ocr_cached"] == 2                       # 两页都走了缓存
    for r in out["records"]:
        # ★ 核心断言：外层包裹没丢（seg 读的 ocr.mark 能拿到标题）
        assert r["ocr"].get("mark", {}).get("t")

    # 结构完整 → 切份能看到 is_first/标题信号：切成 2 份独立材料，
    # 而不是“1 份 + 一式两份复本”（结构丢失时 _same 全空必误判重复）
    cands = seg.build_candidates(out["records"])
    assert len(cands) == 2 and all(c["copies"] == 1 for c in cands)


def _seed_mark_cache(cache_dir, records, channel, cards):
    """把"建档结果"预置进 OCR 缓存 —— 让 node_mark 命中缓存、**一次模型都不调**。

    这是让识别链能离线跑的关键：建档是唯一每个页面都要调视觉模型的环节，缓存命中
    即可零成本走完。cards 形如 [(表头, 要点), …]，按 records 顺序配。
    """
    from archive.skill import loader            # 本文件其它用例也是函数内 import
    prompt = loader.mark()
    for r, (t, s) in zip(records, cards):
        oc.store(r["md5"], channel, prompt,
                 {"mark": {"t": t, "doc": None, "is_first": True, "is_cont": None,
                           "date": {"y": 2021, "m": 4, "d": 6}, "kind": "表格式",
                           "s": s, "u": False},
                  "title": t, "date": {"y": 2021, "m": 4, "d": 6}, "texts": [s],
                  "cls": {"category": None, "doubt": False, "doc": None,
                          "evidence": s}},
                 model="stub")


class _StubText:
    """桩文本模型：定类/合并阶段用。返回合法 JSON，不联网、不烧 key。"""

    def __init__(self, items=None):
        self._items = items if items is not None else []

    def invoke(self, msgs):
        payload = {"items": self._items}
        class _R:
            content = json.dumps(payload, ensure_ascii=False)
        return _R()


def test_node_mark_emits_one_page_event_per_page(tmp_path, monkeypatch):
    """每建完一页就推一条 page 事件（识别进度面板靠它逐张刷新）。

    直接调 node_mark（不在图里跑）→ 用打桩替换 writer 收事件。这条钉的是"发不发、
    字段齐不齐、缓存命中的页也发"；"事件真能从 LangGraph 的 custom 流里出来"由
    下一条用例钉（那需要真图跑一遍）。
    """
    from archive.engine import graph
    d = tmp_path / "c"; monkeypatch.setattr(oc, "CACHE_DIR", str(d))

    got: list = []
    monkeypatch.setattr(graph, "_page_writer", lambda: got.append)

    recs = [{"seq": i, "path": f"x{i}.jpg", "md5": f"md5-{i}", "ocr": {}, "flags": []}
            for i in (1, 2, 3)]
    _seed_mark_cache(d, recs, "ch", [("入党申请书", "申请入党意愿"),
                                     ("干部履历表", "本人经历"),
                                     ("年度考核表", "考核结果")])
    # 把通道名和视觉模型都**直接放进 state**：这样连 providers 都不用碰
    # （state 里给了 llm_vision，node_mark 就不会去 make_model；给了 vision_channel
    #   就不会去 _pick）—— 用例完全不依赖 .env 和本机装了哪几家 provider。
    out = graph.node_mark({"records": recs, "vision_channel": "ch",
                           "llm_vision": object()})

    assert out["ocr_cached"] == 3                       # 三页全走缓存（没调模型）
    assert [e["seq"] for e in got] == [1, 2, 3] or sorted(e["seq"] for e in got) == [1, 2, 3]
    assert len(got) == 3
    e = next(x for x in got if x["seq"] == 2)
    assert e["type"] == "page" and e["total"] == 3      # 前端要拼"第 N / 总数 张"
    assert e["title"] == "干部履历表" and e["summary"] == "本人经历"
    assert e["date"] == "2021-4-6" and e["cached"] is True
    assert "img" not in e                               # ★ 不带图/不带 URL：URL 是上站层补的


def test_iter_flow_relays_page_events_from_custom_stream(tmp_path, monkeypatch):
    """整条识别链离线跑通：page 事件能从 LangGraph 的 custom 流里透出来。

    ★ 这条钉的是 `stream_mode` 必须含 "custom" —— 漏了的话 get_stream_writer() 推的
      事件会被**静默丢弃**（不报错、不小崩），界面上就是"进度面板一直空着"。所以
      必须有一条**真的跑一遍图**的用例，而不是只测到 node_mark 内部。
    """
    import io
    from PIL import Image
    from archive.storage.project import create_project, load_project
    from archive.engine import providers, graph
    from archive import station_adapter as ad

    d = tmp_path / "c"; monkeypatch.setattr(oc, "CACHE_DIR", str(d))
    monkeypatch.setattr(providers, "make_model", lambda *a, **k: None)  # 视觉模型不该被用到

    src = tmp_path / "in"; src.mkdir()
    base = Image.new("RGB", (8, 8), (200, 180, 160))
    for i in (1, 2):                                    # 两张**内容不同**的图（不同 md5）
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200 - i * 10, 180, 160)).save(buf, "JPEG")
        (src / f"{i}.jpg").write_bytes(buf.getvalue())
    pj = create_project("测试卷", str(src), str(tmp_path / "proj"))
    _, records = load_project(pj)
    _seed_mark_cache(d, records, "ch", [("入党申请书", "申请入党意愿"),
                                        ("年度考核表", "考核结果")])

    stub_text = _StubText([
        {"id": 1, "category": "六", "title": "入党申请书", "date": None,
         "doubt": False, "reason": "桩"},
        {"id": 2, "category": "三", "title": "考核表", "date": None,
         "doubt": False, "reason": "桩"}])
    sink: dict = {}
    # 视觉模型传桩对象（全命中缓存 → 一次都不会 invoke）
    events = list(ad._iter_flow(records, stub_text, object(), "ch", sink))

    pages = [e for e in events if e["type"] == "page"]
    assert len(pages) == len(records)                   # 每页一条
    assert all(p["cached"] and p["total"] == len(records) for p in pages)
    assert any(e["type"] == "progress" for e in events)  # 节点进度还在
    assert sink.get("materials")                        # 最终状态经 sink 交回（不是 return）


def test_build_runner_attaches_image_url_to_page_events(tmp_path, monkeypatch):
    """页级事件由 build_runner 补上图片 URL —— 且**缩略图**、project 已 quote 编码。

    （URL 只能在这一层拼：engine/graph.py 不该知道 web 那层怎么取图；而 project 是含
    中文和反斜杠的路径，不编码会在浏览器里表现为破图 —— 09-10 踩过。）
    """
    import archive.station_adapter as ad
    from station.core.context import Context

    pj = tmp_path / "卷" / "project.json"
    pj.parent.mkdir()
    pj.write_text(json.dumps({"name": "卷", "records_file": "photos.json"},
                             ensure_ascii=False), encoding="utf-8")
    (pj.parent / "photos.json").write_text("[]", encoding="utf-8")

    def _fake_flow(recs, t, v, ch="", sink=None, refresh=False):
        if sink is not None:
            sink.update({"materials": [], "issues": [], "records": []})
        return iter([{"type": "page", "seq": 7, "total": 9, "title": "履历表",
                      "doc": "", "date": "", "summary": "", "cached": False}])

    monkeypatch.setattr(ad, "_iter_flow", _fake_flow)
    ctx = Context(data_dir=tmp_path / "skilldata")
    events = list(ad.build_runner(ctx, project=str(pj)))

    page = next(e for e in events if e["type"] == "page")
    assert "/api/archive/page/7?" in page["img"]
    assert "thumb=1" in page["img"]                     # 必须缩略图（上百张，原图会拖死）
    assert " " not in page["img"]                       # 路径已 quote（空格/中文/反斜杠都不裸奔）


def test_thumb_bytes_scales_down(tmp_path):
    """缩略图：大图能压成指定宽度的小 JPEG（全景卡一列上百张，必须够小够快）。"""
    import io
    from PIL import Image
    from archive import imaging
    src = tmp_path / "big.jpg"
    Image.new("RGB", (2000, 3000), (200, 180, 160)).save(src, "JPEG")
    data = imaging.thumb_bytes(str(src), 160)
    im = Image.open(io.BytesIO(data))
    assert im.size[0] == 160 and im.format == "JPEG"     # 等比缩到宽 160
    assert len(data) < 50_000                            # 文件很小（实测 ~0.5KB）


def test_thumb_rotate_changes_orientation(tmp_path):
    """旋转：给 thumb_bytes 传 rotate，输出方向跟着换（横躺照片转正就靠它）。

    真卷背景（09-10）：翻拍设备不写 EXIF 方向，113/126 页像素横躺且程序无从判断，
    只能由人确认后记进 photos.json 的 rotate，展示与导出都照它转。
    """
    import io
    from PIL import Image
    from archive import imaging
    src = tmp_path / "p.jpg"
    Image.new("RGB", (400, 300), (200, 180, 160)).save(src, "JPEG")    # 横版
    assert Image.open(io.BytesIO(imaging.thumb_bytes(str(src), 100, 0))).size == (100, 75)
    assert Image.open(io.BytesIO(imaging.thumb_bytes(str(src), 100, 90))).size == (100, 133)


def test_show_overview_lists_all_categories(monkeypatch):
    """全景卡：没材料的类别也要列出来（用户才知道"这一类是空的"），顺序固定。

    同时锁住：每条材料带**首页缩略图** URL —— 用户手上是一叠纸质照片，
    只给"第 41 张"他没法对应到哪一张，给小图才认得出。
    """
    import sys
    from archive.domain.classes import VALID_SUBS
    code = Path(__file__).resolve().parents[1] / "skills" / "archive" / "code"
    if str(code) not in sys.path:
        sys.path.insert(0, str(code))
    import archive_tools
    monkeypatch.setattr(archive_tools, "_state", lambda ctx: ("/tmp/卷.json", {
        "materials": [{"uid": "m1", "title": "入党志愿书", "category": "六",
                       "members": [41, 42], "copies": 1, "doubt": False}],
        "issues": [], "person": "测试"}))
    cats = archive_tools.t_show_overview(None)["render"]["cats"]
    assert [c["category"] for c in cats] == list(VALID_SUBS)   # 16 类全列、顺序固定
    assert sum(c["n"] for c in cats) == 1                      # 只有“六”有材料
    assert all(c["items"] == [] for c in cats if c["n"] == 0)
    liu = cats[[c["category"] for c in cats].index("六")]["items"][0]
    from urllib.parse import quote
    q = quote("/tmp/卷.json", safe="")                # project 必须被 quote 编码进 URL
    assert liu["thumbs"] == [f"/api/archive/page/{s}?project={q}&thumb=1"
                             for s in (41, 42)]       # 这份材料两页 → 两张缩略图


# ── ② 识别执行器（station_adapter：跑完现场落 DB，无确认门）────────

def test_adapter_recognize_persists_state(tmp_path, monkeypatch):
    """识别 job 跑完：archive_states 里有现场、job 状态 done（不再停在确认门）。

    真识别链要调模型 → monkeypatch 掉 _iter_flow，直接给"识别结果"。
    """
    from archive.storage.project import create_project
    import archive.station_adapter as ad
    from station.core.context import Context

    # 造一个 2 页假项目（用 1x1 真 JPEG——create_project 会用 PIL 读图算 phash）
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (1, 1)).save(buf, format="JPEG")
    one = buf.getvalue()
    src = tmp_path / "in"; src.mkdir()
    for i in (1, 2):
        (src / f"{i}.jpg").write_bytes(one)
    pj = create_project("测试卷", str(src), str(tmp_path))
    person = "张三"

    # 签名要与 _iter_flow(records, llm_text, llm_vision, vision_channel, sink) 对齐：
    # 产出**事件 dict**，最终状态**回填进 sink**（生成器的 return 值会被 for 丢掉，
    # 所以结果走这个可变 dict 交回调用方 —— 见 _iter_flow 的 docstring）。
    def _fake_flow(recs, t, v, ch="", sink=None, refresh=False):
        if sink is not None:
            sink.update({"materials": [_mat("m1", "一", "履历表", [1]),
                                       _mat("m2", "九-2", "任免表", [2])],
                         "issues": [], "ocr_cached": 0})
        return iter([{"type": "progress", "percent": 50, "message": "half"}])

    monkeypatch.setattr(ad, "_iter_flow", _fake_flow)
    monkeypatch.setattr(ad, "load_project", None, raising=False)  # noqa：真函数仍被 import 用

    ctx = Context(data_dir=tmp_path / "skilldata")
    events = list(ad.build_runner(ctx, project=pj, person=person))
    assert events[-1]["percent"] == 100                # 走到了终点
    st = it.load_state(pj)                             # 现场已落 DB
    assert st is not None and len(st["materials"]) == 2
    assert st["person"] == "张三"


# ── ③④ 交互操作 + 账本 ──────────────────────────────────────────

@pytest.fixture()
def proj(tmp_path):
    """手搓一个小项目（3 页记录）+ 一份现场（3 份材料），返回 project.json 路径。"""
    root = tmp_path / "卷"
    root.mkdir()
    pj = root / "project.json"
    pj.write_text(json.dumps({"name": "卷", "records_file": "photos.json"},
                             ensure_ascii=False), encoding="utf-8")
    (root / "photos.json").write_text(json.dumps(
        [{"seq": i, "path": f"x{i}.jpg", "md5": f"m{i}", "ocr": {}} for i in (1, 2, 3)],
        ensure_ascii=False), encoding="utf-8")
    mats = [_mat("m1", "一", "履历表", [1]),
            _mat("m2", "三", "考核表", [2]),
            _mat("m3", "九-1", "调资表", [3])]
    it.save_state(str(pj), mats, [], "测试")
    return str(pj)


def _mat(uid, cat, title, members):
    """造一条最小可用的材料行（字段契约见 domain/models.py）。"""
    return {"uid": uid, "seq": 1, "category": cat,
            "main": cat.split("-")[0], "title": title, "title_src": "engine",
            "date": None, "copies": 1, "pages": len(members),
            "members": members, "dup_pages": [], "assigned_pages": list(members),
            "evidence": "", "doubt": False, "verdict": "ok"}


def test_set_category_and_ledger(proj):
    """改类生效、非法类被拦、账本记了一条。"""
    st = it.op_set_category(proj, "m2", "九-2", "任免表")
    m = it.mat_by_uid(st["materials"], "m2")
    assert m["category"] == "九-2" and m["doubt"] is False and m["title"] == "任免表"
    with pytest.raises(ValueError):
        it.op_set_category(proj, "m2", "九")             # 四/九必须落小类
    led = it.read_corrections(proj)
    assert led[-1]["kind"] == "set_category"
    assert led[-1]["before"]["category"] == "三"


def test_merge_and_split(proj):
    """并份页取并集且少一行；拆份成新行且两行存疑。"""
    st = it.op_merge(proj, "m1", "m2")
    assert len(st["materials"]) == 2
    kept = it.mat_by_uid(st["materials"], "m1")
    assert kept["members"] == [1, 2]
    st = it.op_split(proj, "m1", [2])
    assert len(st["materials"]) == 3
    new_uid = next(m["uid"] for m in st["materials"] if m["members"] == [2])
    assert next(m for m in st["materials"] if m["uid"] == new_uid)["doubt"] is True


def test_apply_reocr_updates_row(proj):
    """采用新卡：photos.json 回写 + 首页对应材料行标题刷新。"""
    st = it.op_apply_reocr(proj, 2, {"mark": {"t": "年度考核登记表", "s": "新"},
                                     "title": "年度考核登记表",
                                     "date": None, "texts": ["新"]})
    row = next(m for m in st["materials"] if m["members"] == [2])
    assert row["title"] == "年度考核登记表"
    recs = json.loads((proj.parent if isinstance(proj, Path) else Path(proj).parent
                       / "photos.json").read_text(encoding="utf-8"))
    assert recs[1]["ocr"]["mark"]["t"] == "年度考核登记表"


def test_checkpoint_list_then_restore(proj):
    """★ 检查点必须"存得下、列得出、退得回" —— 三段缺一就是死链。

    回退要 ck_id，而它是 DB 全局自增号（不是 1..n），没人能猜；所以 list 和 restore
    必须成对存在（09-11 排查到过：只有 restore 没有 list，每步都在写却列不出来）。
    """
    it.op_set_category(proj, "m1", "九-2")                # 改类前自动存下"一"
    it.op_set_category(proj, "m1", "十")                  # 再存下"九-2"，现场变成"十"
    assert it.mat_by_uid(it.load_state(proj)["materials"], "m1")["category"] == "十"

    cks = it.list_checkpoints(proj)                       # 新→旧
    assert len(cks) == 2, "两次改动应该攒下两个检查点"
    assert cks[0]["id"] and "改类前" in (cks[0]["label"] or "")   # 编号拿得到、说明是人话

    it.restore_checkpoint(proj, cks[0]["id"])             # 最近那个 = 第二次改之前 = 九-2
    assert it.mat_by_uid(it.load_state(proj)["materials"], "m1")["category"] == "九-2"
    # 恢复前会自动再存一个"反悔点"，所以检查点只增不减
    assert len(it.list_checkpoints(proj)) == 3


# test_aggregate_and_draft 已删（09-13）：它测的 aggregate_corrections/draft_proposal
# 被"口径学习"取代（每次改类即时开提案、重复改累加次数）。等价断言搬去了
# tests/test_learning.py：test_from_correction_bumps_existing_pending（重复改 → 一条、次数在涨）。
