"""翻拍件方向探测（engine/orient.py）的离线单测 —— 全部用桩模型，不联网不烧 key。

新手视角（Java 朋友版）：这个模块干的活是"猜整卷该往哪转"，猜错的代价是用户看到
倒着的字、还得再来一次整卷操作。所以用例重点不在"猜得准不准"（那要靠真卷实测，
见模块 docstring），而在**猜不准的时候必须一个字都不写** —— 下面几条把这条底线钉死：
  · 样本之间打架 → 不写
  · 模型自己说没把握 → 不写
  · 没有可判的页 → 不写
  · 人工已经定过方向的页 → 永远不覆盖
"""
from __future__ import annotations

import json

from PIL import Image

from archive.engine import orient


# ── 造图与桩模型 ──────────────────────────────────────────────────

def _img(tmp_path, name: str, w: int, h: int) -> str:
    """造一张 w×h 的真 JPEG，返回路径（is_landscape 要用 PIL 真的去读）。"""
    p = tmp_path / name
    Image.new("RGB", (w, h), (200, 180, 160)).save(p, "JPEG")
    return str(p)


def _rec(tmp_path, seq: int, w: int, h: int, rotate=None, src=None) -> dict:
    r = {"seq": seq, "path": _img(tmp_path, f"{seq}.jpg", w, h)}
    if rotate is not None:
        r["rotate"] = rotate
    if src is not None:
        r["rotate_src"] = src            # "manual" = 人工定过；"auto" = 上一轮自动探测写的
    return r


class _StubVision:
    """桩视觉模型：按预设的 pick 序列回答（用完了就重复最后一个）。"""

    def __init__(self, picks):
        self._picks = list(picks)
        self.calls = 0

    def invoke(self, msgs):
        self.calls += 1
        pick = self._picks[min(self.calls - 1, len(self._picks) - 1)]
        class _R:
            content = json.dumps({"pick": pick[0], "conf": pick[1], "reason": "桩"})
        return _R()


# ── ① 横躺判据 ────────────────────────────────────────────────────

def test_is_landscape_reads_real_images(tmp_path):
    """宽>高 = 横躺（该转 ±90）；竖版 = 不动；读不动 = 说不确定（None，不当横躺）。"""
    assert orient.is_landscape(_img(tmp_path, "wide.jpg", 400, 300)) is True
    assert orient.is_landscape(_img(tmp_path, "tall.jpg", 300, 400)) is False
    assert orient.is_landscape(str(tmp_path / "不存在.jpg")) is None


def test_undecided_skips_pages_with_manual_rotation(tmp_path):
    """已经有人工定过方向的页不参与探测 —— 人比模型的二选一可靠，重跑识别不能覆盖。"""
    recs = [_rec(tmp_path, 1, 400, 300),                             # 横躺、没定过 → 参与
            _rec(tmp_path, 2, 400, 300, rotate=90, src="manual"),    # 人工定过 → 不参与
            _rec(tmp_path, 3, 300, 400)]                             # 竖版 → 不参与
    assert [r["seq"] for r in orient.undecided_landscape(recs)] == [1]


def test_auto_rotation_is_re_probed(tmp_path):
    """★ 上一轮**自动**探测写下的角度必须能重新探 —— 否则提示词改好了也白改：
    错误答案会永久卡在 photos.json 里（这是 09-12 修方向准确率时踩到的）。"""
    recs = [_rec(tmp_path, 1, 400, 300, rotate=90, src="auto"),      # 自动写的 → 参与
            _rec(tmp_path, 2, 400, 300, rotate=90)]                  # 老卷没标来源 → 参与
    assert [r["seq"] for r in orient.undecided_landscape(recs)] == [1, 2]
    hit, _, _ = orient.auto_rotate(recs, _StubVision([("B", "high")]))
    assert hit == 2 and all(r["rotate"] == 270 and r["rotate_src"] == "auto" for r in recs)


# ── ② 逐页探测：各判各的 ──────────────────────────────────────────

def test_each_page_gets_its_own_angle(tmp_path):
    """★ 逐页判、各写各的 —— 整卷可能方向不一致（实测李明卷第 13 页要顺时针、
    50/87 页要逆时针）。谁把它改回"探一张、整卷沿用"，这条就红。"""
    recs = [_rec(tmp_path, 1, 400, 300), _rec(tmp_path, 2, 400, 300)]
    # 第一页答 B（顺时针90=270），第二页答 A（逆时针90）—— 两页角度不同。
    # workers=1：桩模型是"按调用顺序"发答案的，而线上是 6 路并发、顺序不定
    # （顺序在这里不重要，重要的是**每页各写各的**）。
    hit, miss, msg = orient.auto_rotate(recs, _StubVision([("B", "high"), ("A", "high")]),
                                        workers=1)
    assert hit == 2 and miss == 0
    assert [r["rotate"] for r in recs] == [270, 90]
    assert "逐页判的方向" in msg


def test_pick_a_maps_to_counterclockwise_90(tmp_path):
    """A = 逆时针 90°（PIL 语义：正值=逆时针）。符号别搞反 —— 反了就是整卷倒着。"""
    recs = [_rec(tmp_path, 1, 400, 300)]
    hit, _, _ = orient.auto_rotate(recs, _StubVision([("A", "high")]))
    assert hit == 1 and recs[0]["rotate"] == 90


def test_manual_rotation_is_never_touched(tmp_path):
    """人工定过方向的页不参与探测、也不被改写（人比模型可靠）。"""
    recs = [_rec(tmp_path, 1, 400, 300, rotate=90, src="manual"),
            _rec(tmp_path, 2, 300, 400)]
    hit, miss, msg = orient.auto_rotate(recs, _StubVision([("B", "high")]))
    assert hit == 0 and miss == 0 and msg == ""
    assert recs[0]["rotate"] == 90 and "rotate" not in recs[1]


# ── ③ 单页判不准：只跳过那一页，别的照写 ──────────────────────────

def test_unconfident_page_is_skipped_but_others_written(tmp_path):
    """判不准的那页一个字都不写，其余页照常转正，并在说明里报数让用户补。"""
    class _PerPage:
        """第一页没把握，第二页明确答 B。"""
        def __init__(self):
            self.n = 0
        def invoke(self, msgs):
            self.n += 1
            pick = ("uncertain", "high") if self.n == 1 else ("B", "high")
            class _R:
                content = json.dumps({"pick": pick[0], "conf": pick[1]})
            return _R()

    # ★ workers=1：桩是"按调用顺序"发答案的，而线上默认 6 路并发 —— 不串行化的话
    #   两个页谁先被问到是不确定的，这条会**偶发失败**（写测试时踩过：第一页有时拿到
    #   "B"、于是被写上了 rotate）。顺序不是本用例要验的东西，"判不准只跳过那一页"才是。
    recs = [_rec(tmp_path, 1, 400, 300), _rec(tmp_path, 2, 400, 300)]
    hit, miss, msg = orient.auto_rotate(recs, _PerPage(), workers=1)
    assert hit == 1 and miss == 1
    assert "rotate" not in recs[0] and recs[1].get("rotate") in (90, 270)
    assert "1 页判不准" in msg                      # 报数，别让用户以为全好了


def test_backs_off_when_model_not_confident(tmp_path):
    """全部判不准 → 一页都不写，改成提醒用户自己说一句。"""
    recs = [_rec(tmp_path, 1, 400, 300)]
    for pick in (("B", "low"), ("uncertain", "high")):
        hit, miss, msg = orient.auto_rotate(recs, _StubVision([pick]))
        assert hit == 0 and miss == 1 and "rotate" not in recs[0]
        assert "没把握" in msg and "1 页是横躺的" in msg


def test_call_failure_does_not_break_recognition(tmp_path):
    """探测调用抛异常（断网/超时）→ 不写、不抛出去，别拖垮整卷识别。"""
    class _Boom:
        def invoke(self, msgs):
            raise RuntimeError("网络挂了")

    recs = [_rec(tmp_path, 1, 400, 300)]
    hit, miss, msg = orient.auto_rotate(recs, _Boom())
    assert hit == 0 and miss == 1 and "rotate" not in recs[0]
    assert "没把握" in msg


def test_no_landscape_pages_means_no_op(tmp_path):
    """全是竖版 → 不调模型、不写任何东西、不打扰用户。"""
    recs = [_rec(tmp_path, 1, 300, 400)]
    llm = _StubVision([("B", "high")])
    hit, miss, msg = orient.auto_rotate(recs, llm)
    assert hit == 0 and miss == 0 and msg == "" and llm.calls == 0


def test_probe_result_is_cached(tmp_path, monkeypatch):
    """探测结果进 OCR 缓存：同一张图第二次探测**一次模型都不调**（重跑免费）。"""
    import archive.storage.ocr_cache as oc
    monkeypatch.setattr(oc, "CACHE_DIR", str(tmp_path / "c"))

    recs = [_rec(tmp_path, 1, 400, 300)]
    recs[0]["md5"] = "md5-orient"
    llm = _StubVision([("B", "high")])
    hit, _, _ = orient.auto_rotate(recs, llm, channel="ch")
    assert hit == 1 and llm.calls == 1

    recs[0].pop("rotate")                       # 模拟重跑识别时的"未定方向"状态
    hit2, _, msg2 = orient.auto_rotate(recs, llm, channel="ch")
    assert hit2 == 1 and llm.calls == 1         # ★ 没有第二次调用（命中缓存）
    assert "命中缓存" in msg2 and recs[0]["rotate"] == 270
