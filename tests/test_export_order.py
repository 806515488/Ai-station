"""导出顺序的离线单测（PDF 页序 / Excel 目录序）—— 不生成真 PDF，只测纯函数与表格。

用户拍板：**导出的 PDF 必须按 一→十 的顺序**（rules §5.3 早就这么写，代码一直没做到）。
而且照片顺序随机，所以"页序"这回事必须完全由 materials 推出来：
大类固定序 → 类内按成文时间 → 材料内按装订序 → 没归入任何材料的散图垫最后。
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

from archive.domain import classes
from archive.export import excel, original_pdf


def rec(seq: int) -> dict:
    return {"seq": seq, "md5": f"m{seq}", "path": f"{seq}.jpg", "ocr": {}}


def mat(uid, cat, members, date=None, raw=None, **kw):
    d = {"uid": uid, "category": cat, "title": f"材料{uid}", "members": list(members),
         "date": {"y": date, "m": None, "d": None} if date else None,
         "raw_seqs": list(raw or members), "pages": len(members), "copies": 1}
    d.update(kw)
    return d


# ── PDF 页序 ────────────────────────────────────────────────────

def test_page_sequence_follows_catalog_order():
    """★ 页序 = 目录序：一→十，类内按成文时间，材料内按装订序。"""
    records = [rec(i) for i in [1, 2, 3, 5, 7, 9]]
    mats = [mat("m3", "十", [7]),                      # 十（最后）
            mat("m1", "一", [3, 1, 2]),                # 一（最前，装订序 3,1,2）
            mat("m2", "九-1", [9], date=2014),
            mat("m4", "九-1", [5], date=1990)]         # 同小类：1990 在 2014 前
    assert original_pdf.page_sequence(records, mats) == [3, 1, 2, 5, 9, 7]


def test_unassigned_pages_go_last_and_in_order():
    """没归进任何材料的散图排最后（并进待核对清单），升序。"""
    records = [rec(i) for i in range(1, 6)]
    mats = [mat("m1", "一", [4])]
    assert original_pdf.page_sequence(records, mats) == [4, 1, 2, 3, 5]


def test_duplicate_copies_are_excluded_by_default():
    """一式N份的复本**不入册**（rules §4：PDF 只收第一份，份数只在目录体现）。"""
    records = [rec(i) for i in range(1, 5)]
    mats = [mat("m1", "六", [1, 2], copies=2, dup_pages=[3, 4])]
    assert original_pdf.page_sequence(records, mats) == [1, 2]
    assert original_pdf.page_sequence(records, mats, include_dups=True) == [1, 2, 3, 4]


def test_category_segments_are_monotonic():
    """随便造一堆乱序材料，页序里的大类段必须单调不减（不会"十类"夹在"一类"中间）。"""
    records = [rec(i) for i in range(1, 8)]
    mats = [mat(f"m{i}", cat, [i]) for i, cat in
            enumerate(["十", "六", "一", "九-2", "二", "四-1", "七"], start=1)]
    seq = original_pdf.page_sequence(records, mats)
    ranks = []
    for s in seq:
        cat = next(m["category"] for m in mats if s in m["members"])
        r = classes.CAT_RANK[cat]
        if not ranks or r != ranks[-1]:
            ranks.append(r)
    assert ranks == sorted(ranks) and len(ranks) == len(set(ranks))


# ── 页面统一尺寸（用户要求"生成的 pdf 要大小一致"）────────────────

def test_all_pages_land_on_the_same_canvas():
    """★ 不管原图是横是竖、是大是小，摆到画布上之后**像素尺寸完全一致**。

    为什么必须有这条：PDF 的页面尺寸是 img2pdf 按"图片像素 ÷ DPI"算出来的，而翻拍照片
    尺寸并不一致（实测同卷里既有 1702×1276 也有 4032×3024）→ 出出来 216×288mm 和
    512×683mm 混着，翻页时忽大忽小。摆到统一画布上，页面尺寸就绝对一致。
    """
    from PIL import Image
    for size in ((1702, 1276), (4032, 3024), (1276, 1702), (800, 800)):
        img = Image.new("RGB", size, (200, 180, 160))
        page = original_pdf.fit_canvas(img)
        assert page.size == original_pdf.CANVAS_PX
        assert page.height > page.width                 # A4 竖版
        img.close(); page.close()


def test_fit_canvas_keeps_aspect_and_centers():
    """等比缩放（不变形）+ 居中：内容仍落在画布内，且四边留白基本对称。"""
    from PIL import Image
    img = Image.new("RGB", (2000, 1000), (255, 255, 255))
    img.paste(Image.new("RGB", (2000, 500), (0, 0, 0)), (0, 250))   # 中间一条黑带
    page = original_pdf.fit_canvas(img)
    w, h = original_pdf.CANVAS_PX
    # 黑带等比缩到 w 宽、h/4 高，居中 → 上下留白相等
    ys = [y for y in range(h) if page.getpixel((w // 2, y))[0] < 128]
    assert ys and abs((ys[0]) - (h - 1 - ys[-1])) <= 2
    img.close(); page.close()


# ── 两条组装路径的页面尺寸必须一致（09-13 实测踩到的坑）────────────

def _photo(path: Path, size=(1600, 1200)) -> str:
    """造一张真 JPEG（横的，逼 fit_canvas 真的缩放），返回路径字符串。"""
    from PIL import Image
    Image.new("RGB", size, (200, 180, 160)).save(str(path), format="JPEG", quality=85)
    return str(path)


def _page_mm(pdf_path: str) -> tuple[int, int]:
    """读 PDF 第一页的**物理**尺寸（毫米）—— pt ÷ 72 × 25.4。"""
    import pypdf
    box = pypdf.PdfReader(pdf_path).pages[0].mediabox
    return (round(float(box.width) / 72 * 25.4), round(float(box.height) / 72 * 25.4))


def test_pdf_page_size_is_a4_on_both_assembly_paths(tmp_path, monkeypatch):
    """★ 装不装 img2pdf，出出来的页面**都必须是 A4**（210×297mm），一字不差。

    这是 09-13 抓到的真坑：`img2pdf` 是照 **JPEG 头里写的 DPI** 排页面的，而存 JPEG 时
    没写 dpi → 它按 96dpi 算，每页变成 328×464mm；PIL 兜底那条路按 resolution=150 出
    的却是 A4。于是"环境里装没装 img2pdf"这一个差异就会**悄悄**把成品尺寸换掉 ——
    不报错、不崩，只是印出来不对、跟用户要的"大小一致"（是 A4）不符。
    修法是存 JPEG 时写 dpi=(CANVAS_DPI, CANVAS_DPI)（见 original_pdf.CANVAS_DPI）。

    这里两条路都真跑一遍再比：只测一条路的话，另一条哪天漂了没人知道。
    """
    src = _photo(tmp_path / "a.jpg")
    records = [{"seq": i, "path": src, "rotate": 0} for i in (1, 2)]
    mats = [mat("m1", "一", [1, 2])]

    # ① 有 img2pdf（正常装好依赖时的主路径）
    pytest.importorskip("img2pdf", reason="没装 img2pdf —— 见下面 test_img2pdf_is_declared_dependency")
    a = original_pdf.export_pdf(records, mats, "有", str(tmp_path / "a"))
    # ② 没有 img2pdf（退化到 PIL 那条路）
    #    sys.modules 里塞 None → `import img2pdf` 抛 ImportError（Python 的既有语义）
    monkeypatch.setitem(sys.modules, "img2pdf", None)
    b = original_pdf.export_pdf(records, mats, "无", str(tmp_path / "b"))

    assert _page_mm(a) == (210, 297), "有 img2pdf 时不是 A4：JPEG 头里的 dpi 没写对"
    assert _page_mm(b) == (210, 297), "PIL 兜底路径不是 A4"
    assert _page_mm(a) == _page_mm(b), "两条组装路径出的页面尺寸不一致"


def test_img2pdf_is_a_declared_dependency():
    """★ img2pdf 是**主路径不是可选加速** —— 别顺手从依赖里删掉。

    缺了它就只能走 PIL 兜底，而兜底那次把全卷一次性解码成 RGB：实测 120 页手机翻拍
    峰值 1411MB（有 img2pdf 时 552MB）。1.9G 的小机器上这就是"出得来"和"被 OOM
    杀进程"的差别（用户看到的是服务突然没了，没有任何报错）。
    """
    root = Path(__file__).resolve().parents[1]
    txt = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "img2pdf" in txt


# ── Excel 目录 ──────────────────────────────────────────────────

def test_excel_rows_follow_skeleton_and_date_order(tmp_path):
    """Excel 里：大类按骨架 一→十 排列；类内按成文时间升序、无日期垫后。"""
    mats = [mat("m1", "九-1", [5], date=2014),
            mat("m2", "一", [1], date=2005),
            mat("m3", "九-1", [6], date=1990),
            mat("m4", "十", [9])]                        # 无日期
    path = excel.export(mats, "测试", str(tmp_path))
    ws = openpyxl.load_workbook(path).active
    rows = [(ws.cell(r, 1).value, ws.cell(r, 2).value)
            for r in range(1, ws.max_row + 1)]
    names = [n for _, n in rows if n in ("材料m1", "材料m2", "材料m3", "材料m4")]
    assert names == ["材料m2", "材料m3", "材料m1", "材料m4"]   # 一 → 九-1(1990,2014) → 十


def test_excel_tie_break_is_stable(tmp_path):
    """同一天的两份材料：按最小照片号兜底 —— 两次导出必须一样（以前按模型给的顺序，会变）。"""
    a = [mat("m1", "六", [30], date=1999), mat("m2", "六", [10], date=1999)]
    p1 = excel.export(list(a), "测试", str(tmp_path / "1"))
    p2 = excel.export(list(reversed(a)), "测试", str(tmp_path / "2"))
    def names(p):
        ws = openpyxl.load_workbook(p).active
        return [ws.cell(r, 2).value for r in range(1, ws.max_row + 1)
                if ws.cell(r, 2).value in ("材料m1", "材料m2")]
    assert names(p1) == names(p2) == ["材料m2", "材料m1"]     # 照片号 10 在前
