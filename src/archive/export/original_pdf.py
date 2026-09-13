"""⑦ 档案PDF导出：照片按【目录顺序】合成PDF + 材料书签导航。

★ 顺序口径（rules §5.3，09-12 才真正落地）：**PDF 页面顺序 = 目录顺序，不是照片顺序**。
  照片序号是随机的（用户拍照上传的顺序与实物无关），而且一份材料内部的装订序也
  不是照片顺序 —— 所以页面顺序要从 materials 推：大类固定序（一→十）→ 类内按成文
  时间 → 材料内按装订序（members 已经是装订序）。没归进任何材料的散图排最后。

书签 = 每个材料行一行，层级：大类 → 材料行（点书签跳到该材料首页 = rep 页）。

★ 页面尺寸统一（09-12 用户要求"生成的 pdf 要大小一致"）：每页都摆到**同一张 A4 竖版画布**
  上（等比缩放 + 居中 + 白底）。为什么必须显式做：img2pdf 是"按图片像素÷DPI"出页的，
  而翻拍照片的像素尺寸并不一致（实测同卷里既有 1702×1276 也有 4032×3024）→
  出出来的 PDF 页面 216×288mm 和 512×683mm 混着，翻页时忽大忽小。人眼一看就是"没整理"。

新手视角（Java 朋友版）：原件 PDF ≈ 用 iText 之类的库把“照片们”拼成一个 PDF。
  组件分工：img2pdf 直接嵌 JPEG（快、零重压缩）→ 再用 pypdf 给 PDF 加书签大纲；
  两个都不行就退回 PIL 一页页拼 —— 每层都留降级，环境缺哪个库都能出结果。

★ 内存：**这个降级不是"以防万一"，它俩差着 2.5 倍**（09-13 实测，120 页手机翻拍）：
    img2pdf 那条路峰值 552MB，PIL 那条路 1411MB（≈11MB/页，它把全卷一次性解码成 RGB）。
  所以 img2pdf 已经写进 pyproject 的 dependencies —— 它不是装饰品，**别顺手删掉**。
  1.9G 的小机器上跑的是哪条路，就是"出得来"和"被内核杀掉"的区别。
  但两条路出页面的**物理尺寸**必须一样：见 CANVAS_DPI 那段。
"""
from __future__ import annotations

import io          # 内存字节流：把图片/PDF 先放内存里倒腾，不用反复写临时文件
import os

from archive.domain import classes                      # 目录序（dir_sort_key）
from archive.domain.classes import DEFAULT_SUB_TITLES   # 小类标题（书签里显示“九-1 工资类材料”）


# A4 竖版 @150dpi 的像素尺寸（1240×1754）。选 150 而不是 72/300：
#   · 72 太糊（手写字看不清）；300 让文件翻倍且源图本来也没那么高的有效分辨率。
# 所有页都摆到这块画布上 → PDF 里每页尺寸绝对一致（用户要的"大小一致"）。
CANVAS_PX = (1240, 1754)

# 画布对应的分辨率。★ 这个数必须**同时**喂给下面两条组装路径，别各写各的字面量：
#   · ① 存 JPEG 时写进文件头（dpi=）—— img2pdf 是照 JPEG 头里的 DPI 排页面的；
#   · ② PIL 兜底那条路的 save(resolution=)。
# 09-13 实测踩到的坑：JPEG 头里不写 DPI 时 img2pdf 按 96dpi 算 → 每页 328×464mm，
# 而 PIL 那条路按 150 出的是 210×297mm（A4）。于是"装没装 img2pdf"这一个环境差异
# 就会悄悄把成品尺寸从 A4 换成一张巨页 —— 不报错、不崩，只是印出来不对。
CANVAS_DPI = 150


def fit_canvas(img, canvas=CANVAS_PX):
    """把一页图**等比缩放 + 居中**摆到统一画布上（白底）。返回新图，不改原图。

    为什么用白底而不是透明：PDF 里透明会被某些阅读器渲染成黑块；档案扫描件本来就
    是白纸黑字，白底最接近原件观感。
    """
    from PIL import Image
    page = Image.new("RGB", canvas, "white")
    im = img.copy()
    im.thumbnail(canvas, Image.LANCZOS)         # 等比缩到装得下（长边贴边）
    page.paste(im, ((canvas[0] - im.width) // 2, (canvas[1] - im.height) // 2))
    im.close()
    return page


def page_sequence(records: list[dict], materials: list[dict],
                  include_dups: bool = False) -> list[int]:
    """算出 PDF 里每一页该按什么顺序摆 —— 返回 seq 列表（纯函数，好离线单测）。

    顺序 = 目录顺序：
      ① 材料按目录序排（classes.dir_sort_key：大类一→十 → 类内成文时间升序）
      ② 每份材料内部按它的 members（**已经是装订序**，不是照片号大小）
      ③ 没归进任何材料的散图，按照片号升序排在最后（并进《待核对清单》）
    一式N份的复本默认**不入册**（rules §4：PDF 只收第一份，份数只在目录体现）；
    include_dups=True 时把它们紧跟在本体之后（需要"原件全集"时用）。
    """
    ordered = sorted(materials, key=classes.dir_sort_key)
    seqs: list[int] = []
    for m in ordered:
        seqs.extend(m.get("members") or [])                  # members 就是装订序，别再排
        if include_dups:
            seqs.extend(sorted(m.get("dup_pages") or []))
    known = {r["seq"] for r in records}
    # "有着落"的页 = 已经排进去的 + 一式N份的复本。★ 复本必须算进去：它是"有归属但按
    # 规则不印"，如果当成散图，就会被当成"没归类的页"又补到 PDF 末尾去（等于没排除）。
    used = {s for s in seqs if s in known}
    used |= {s for m in ordered for s in (m.get("dup_pages") or []) if s in known}
    left = sorted(s for s in known if s not in used)          # 未归入任何材料的散图
    return seqs + left


def export_pdf(records: list[dict], materials: list[dict], person: str,
               out_dir: str) -> str:
    """主入口：把某干部卷的照片按【目录顺序】合成《<person>-档案原件.pdf》并加材料书签。

    流程：
      ① 按目录序排出页面顺序（page_sequence），把每张照片摆正、转成 JPEG 字节
      ② 根据 materials 算出“书签大纲”（大类 → 材料行→首页页码）
      ③ 用 img2pdf 把 JPEG 直接嵌成 PDF（快/不重压）；不行退回 PIL
      ④ 用 pypdf 给 PDF 加书签大纲 + 元数据，最后存盘
    """
    os.makedirs(out_dir, exist_ok=True)                      # 输出目录就绪
    pdf_path = os.path.join(out_dir, f"{person}-档案原件.pdf")
    by_seq = {r["seq"]: r for r in records}                   # seq → 记录（原始记录不动）
    records = [by_seq[s] for s in page_sequence(records, materials) if s in by_seq]
    seq_to_index = {r["seq"]: i for i, r in enumerate(records)}  # seq → 在 PDF 里第几页(0起)

    # ---- ① 页数据：直立 RGB-JPEG 字节流（img2pdf 直接嵌入，不重编码）----
    from archive.imaging import apply_rotate, load_upright   # 函数内 import：用到才加载
    jpeg_pages: list[bytes] = []
    for r in records:
        img = load_upright(r["path"])             # 按 EXIF 摆正成直立 RGB
        img = apply_rotate(img, r.get("rotate") or 0)   # 再按确认/探测出的方向转正
        img = fit_canvas(img)                     # ★ 统一摆到同一张 A4 竖版画布
        buf = io.BytesIO()
        # quality=88 是归档质量；dpi 见 CANVAS_DPI 那段注释（少写它 = img2pdf 那条路的
        # 页面尺寸会变大，两条路出来的 PDF 不一样）。
        img.save(buf, format="JPEG", quality=88, dpi=(CANVAS_DPI, CANVAS_DPI))
        jpeg_pages.append(buf.getvalue())         # 取出字节存进列表
        img.close()                               # 释放图片内存（大卷尤其重要）

    outlines = _build_outlines(materials, seq_to_index)   # ② 算书签大纲

    # ---- ③ 组装 PDF（优先 img2pdf，缺库退化 PIL）----
    pdf_bytes = _assemble_with_img2pdf(jpeg_pages)
    if pdf_bytes is None:
        pdf_bytes = _assemble_with_pil(jpeg_pages)

    # ---- ④ 书签：pypdf 可用则写大纲 ----
    try:
        import pypdf                        # 能加书签就加（可选库，没装就跳过）
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        writer = pypdf.PdfWriter()
        writer.append(reader)               # 把原 PDF 内容拷进 writer
        writer.add_metadata({               # 文件属性：标题/作者（浏览器里能看）
            "/Title": f"{person}-档案原件",
            "/Author": "lisen",
            "/Creator": "干部人事档案数字化整理台 · © lisen",
        })
        for main_title, children in outlines:      # 逐大类加书签
            if not children:
                continue
            # add_outline_item(标题, 页码)：先加大类，再以它为父级加材料子项
            parent = writer.add_outline_item(main_title, children[0][1])
            for title, page_idx in children:
                writer.add_outline_item(title, page_idx, parent=parent)
        with open(pdf_path, "wb") as f:
            writer.write(f)
    except ImportError:                     # 没装 pypdf → 直接存不含书签的 PDF
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

    return pdf_path


def _build_outlines(materials: list[dict],
                    seq_to_index: dict) -> list[tuple[str, list[tuple[str, int]]]]:
    """把材料清单转成“书签大纲”：[(大类标题, [(书签名, 页index), ...]), ...]

    每个材料行变成一个书签项，书签文字 = “类号+类名｜材料名”，定位到它首页。
    """
    order: list[str] = []                   # 大类出现的先后顺序（保证 PDF 书签不乱序）
    tree: dict[str, list[tuple[str, int]]] = {}   # 大类 → 它下面的书签项列表
    for m in materials:
        cat = m.get("category") or "?"      # 小类号（如 九-1）
        main = cat.split("-")[0] if "-" in cat else cat   # 由小类推大类（九-1→九）
        sub_title = DEFAULT_SUB_TITLES.get(cat, cat)      # 小类的正式名
        label = f"{cat} {sub_title}｜{m.get('title') or '【待确认】'}"   # 书签显示文字
        # 这本书签定位到该材料**装订后的第一页**：members[0]（归组时已按装订序排好）。
        # 回退到"任意一个在册的页"，防某些页不在 records 里（残缺数据）导致整个书签丢失。
        first_page = (seq_to_index.get(m.get("rep"))
                      if m.get("rep") in seq_to_index else None)
        if first_page is None:
            first_page = next((seq_to_index[s] for s in m.get("members", [])
                               if s in seq_to_index), None)
        if first_page is None:              # 找不到（页不在记录里）就跳过这个书签
            continue
        if main not in tree:                # 第一次遇到这个大类 → 记录顺序并开一个组
            tree[main] = []
            order.append(main)
        tree[main].append((label, first_page))
    return [(main, tree[main]) for main in order]


def _assemble_with_img2pdf(jpeg_pages: list[bytes]) -> bytes | None:
    """方案 A：用 img2pdf 把 JPEG 列表直接合成 PDF（原样嵌入、零重压缩、省内存）。

    返回 PDF 字节；没装 img2pdf 或没有图片 → 返回 None（让调用方走 B 方案）。
    """
    try:
        import img2pdf
    except ImportError:                     # 没装 → 返回 None 走退化
        return None
    if not jpeg_pages:
        return None
    return img2pdf.convert(jpeg_pages)


def _assemble_with_pil(jpeg_pages: list[bytes]) -> bytes:
    """兜底方案：PIL 逐页转存（内存占用较高，页数多时慢；保证“缺库也能出”）。

    入参就是 ① 里已经摆正、已经统一画布的 JPEG 字节 —— 别在这里重读原图重算一遍，
    否则"统一尺寸"这件事会在这条退化路径上漏掉（两条路出来的 PDF 就不一样了）。
    """
    from PIL import Image                              # 函数内 import：只有退化路径才用
    images = [Image.open(io.BytesIO(b)).convert("RGB") for b in jpeg_pages]
    buf = io.BytesIO()
    first, rest = images[0], images[1:]     # 首图 + 其余
    # PIL 支持“把多张图存成一个 PDF”：首图为主，其余 append_images
    first.save(buf, format="PDF", save_all=True, append_images=rest,
               resolution=CANVAS_DPI)      # 必须与 JPEG 头里写的 dpi 同源，见 CANVAS_DPI
    for im in images:
        im.close()                          # 释放图片内存
    return buf.getvalue()
