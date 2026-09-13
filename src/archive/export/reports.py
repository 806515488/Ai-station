"""⑥ 报告导出：待核对清单 + 自动判定报告，输出 PDF（中文排版）。

策略：优先 reportlab（精确中文排版）；未安装/无中文字体时退化生成 HTML，
用户可用浏览器/Word 打开另存为 PDF。

新手视角（Java 朋友版）：两份“PDF 报告”（待核对清单 + 自动判定报告）≈ 给非技术
用户看的“审核结果单”。输入是 issues(问题) 和 materials(材料) 的统计，
内容由一个个“章节(标题, 段落[])”拼成 → reportlab 排版成 PDF；中文字体找不到就
降级成 HTML（也能看）——体现了“重要功能要有 fallback”的工程习惯。
"""
from __future__ import annotations

import os
from datetime import datetime


# --------------------------------------------------------------- 中文PDF ----

def _pdf_from_sections(sections: list[tuple[str, list[str]]],
                       out_path: str, title: str) -> str | None:
    """把“章节清单”渲染成一个中文 PDF。成功返回路径；失败（缺库/无字体）返回 None。

    sections 长这样：[(一级标题, [段落…]), …]。reportlab ≈ Java 的 PDFBox/iText。
    里面对“中文字体”做了三层尝试，全失败就返回 None 让调用方走 HTML 兜底。
    """
    # ① 先 import reportlab 系列（它是可选依赖；没装 → ImportError → return None）
    try:
        from reportlab.lib.pagesizes import A4                 # A4 纸尺寸
        from reportlab.lib.units import mm                     # 毫米单位（方便算边距）
        from reportlab.pdfbase import pdfmetrics               # 注册字体用
        from reportlab.pdfbase.ttfonts import TTFont           # 加载系统字体文件
        from reportlab.lib.styles import ParagraphStyle        # 段落样式
        from reportlab.platypus import (SimpleDocTemplate,     # “流式排版”画布
                                        Paragraph, Spacer)
    except ImportError:
        return None

    # ② 注册一个中文字体：优先内置 CID 字体 STSong-Light；不行再找 Windows 字体文件
    font_registered = False
    font_name = "STSong-Light"
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # 内置简体中文 CID 字体
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_registered = True
    except Exception:                           # noqa  # 有些环境没这个
        pass
    if not font_registered:
        # 逐个试 Windows 自带的宋体/黑体/雅黑文件（这些 .ttf/.ttc 一般都在）
        for name, path in (("MSYaHei", "C:/Windows/Fonts/msyh.ttc"),
                           ("SimHei", "C:/Windows/Fonts/simhei.ttf"),
                           ("SimSun", "C:/Windows/Fonts/simsun.ttc")):
            if os.path.isfile(path):
                try:
                    pdfmetrics.registerFont(TTFont(name, path))
                    font_name = name
                    font_registered = True
                    break
                except Exception:               # noqa  # 单个字体文件坏了就试下一个
                    continue
    if not font_registered:                     # 全失败 → 无法保证中文正常显示 → 交 HTML 兜底
        return None

    # ③ 定义几档“段落样式”（标题/一级/正文/页脚），类似 CSS 里的一套 class
    st_title = ParagraphStyle("t", fontName=font_name, fontSize=18,
                              leading=26, spaceAfter=6)
    st_h1 = ParagraphStyle("h1", fontName=font_name, fontSize=14,
                           leading=20, spaceBefore=10, spaceAfter=4)
    st_body = ParagraphStyle("b", fontName=font_name, fontSize=11,
                             leading=17, spaceAfter=3)

    # ④ 页脚回调：每页底部画一行小字（reportlab 在画每页时调它）
    def _footer(canvas, doc_):
        canvas.saveState()                      # 存状态（画完要恢复）
        canvas.setFont(font_name, 9)
        canvas.setFillColor("#888888")
        canvas.drawCentredString(A4[0] / 2, 12 * mm,
                                 "© station")                      # 画在页面底部中央
        canvas.restoreState()                   # 恢复，不影响正文

    # ⑤ 建“文档模板”，把章节拼成 story（一个接一个的流式元素），最后 build 出 PDF
    doc = SimpleDocTemplate(out_path, pagesize=A4,               # 输出到 out_path
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    story = [Paragraph(title, st_title)]        # 先放总标题
    for h1, paras in sections:                  # 再逐章节放 标题+段落
        story.append(Paragraph(h1, st_h1))
        for p in paras:
            story.append(Paragraph(p, st_body))
        story.append(Spacer(1, 4))              # 章节间加一点空隙
    try:
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        return out_path
    except Exception:                           # noqa  # 排版失败也别崩，走 HTML
        return None


def _esc(t: str) -> str:
    """HTML 转义：把 < > & 换成 &lt; &gt; &amp;。

    因为我们要把文本塞进 HTML（报告里允许 HTML 标签），必须转义用户/AI 输入里的
    尖括号，否则会当作标签解析（既可能显示错、也防注入）。
    """
    return (t.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _html_fallback(sections, out_base: str, title: str, now: str) -> str:
    """兜底：reportlab 不可用/无中文字体时，把同样内容渲染成一个 HTML 文件。

    浏览器打开可看，需要 PDF 时选“打印→另存为 PDF”。返回生成的 .html 路径。
    """
    parts = [f"<html><head><meta charset='utf-8'><title>{title}</title>",
             "<style>body{font-family:'Microsoft YaHei';margin:40px;"
             "line-height:1.7} h1{font-size:22px} h2{font-size:16px;"
             "margin-top:24px}</style></head><body>",        # 内嵌一点简单 CSS
             f"<h1>{title}</h1><p>生成时间：{now}</p>"]
    for h1, paras in sections:                  # 和 PDF 同一份 sections，只是套 HTML
        parts.append(f"<h2>{h1}</h2>")
        for p in paras:
            parts.append(f"<p>{p}</p>")          # paras 里已是 _esc 过的安全文本
    parts.append("</body></html>")
    path = out_base + ".html"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return path


# --------------------------------------------------------------- 主入口 ----

def export_reports(records: list[dict], materials: list[dict],
                   issues: list[dict], notes: list[str],
                   out_dir: str, person: str) -> tuple[str, str]:
    """生成《待核对清单》和《自动判定报告》两份 PDF（或 HTML 兜底）。

    返回 (待核对清单路径, 自动判定报告路径)。
    输入：records=整卷页、materials=材料行、issues=问题清单、notes=去重/一式N份说明。
    """
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")     # 生成时间（进报告页脚）

    # 按严重程度把问题分成三桶：必须人工核对(error) / 推断(infer) / 警告(warn)
    review = [i for i in issues if i["level"] == "error"]
    infers = [i for i in issues if i["level"] == "infer"]
    warns = [i for i in issues if i["level"] == "warn"]

    def _fmt(it: dict) -> str:
        """把一条问题格式化成一句给人看的话（含它涉及哪些照片页）。"""
        seq = it.get("seq") or []
        seq_s = "、".join(f"第{s}张" for s in seq) if seq else "见说明"
        return (f"【{it['code']}】{_esc(it['message'])}"
                f"<br/>涉及照片：{seq_s}")

    # ---------------- 待核对清单 ----------------
    p1_base = os.path.join(out_dir, f"{person}-待核对清单")
    sections1 = [
        (f"一、必须人工核对（共 {len(review)} 项）",
         # 没有必核项就给一句“没有”并告诉人怎么核（给审核员的操作指引）
         ["核对方法：打开整理台左侧照片（按张号定位），重点看角部的类号与页码，"
          "在右侧表格中修改后点「重新导出」。"] if review else
         ["本次整理未发现必须人工核对的问题。"]) ,
    ]
    for n, it in enumerate(review, 1):          # 把每个必核问题列成小节
        sections1.append((f"问题 {n}", [_fmt(it)]))
    if warns:                                   # 有警告才加一节（没有就不占纸）
        sections1.append(("二、警告项（建议浏览，不强制处理）",
                          [_fmt(it) for it in warns]))

    # ---------------- 自动判定报告 ----------------
    doubt = sum(1 for m in materials if m.get("verdict") == "doubt")   # 存疑材料数
    sections2 = [("一、推断项（系统自主判断，均已写入目录但请复核）",
                  [_fmt(i) for i in infers] or ["本次无推断项。"])]
    if notes:                                   # 去重/一式N份 的依据（notes 有就写）
        sections2.append(("二、去重与一式多份判定依据", [_esc(n) for n in notes]))
    sections2.append(("三、统计", [             # 关键数字一眼看全卷规模
        f"照片总数：{len(records)}",
        f"材料行数：{len(materials)}",
        f"总份数：{sum(m['copies'] for m in materials)}",
        f"总页数：{sum(m['pages'] for m in materials)}",
        f"存疑材料行：{doubt}（已在待核对清单列出）",
    ]))

    # 两份都先试 PDF，失败则同内容出 HTML
    t1 = f"待核对清单 — {person}"
    t2 = f"自动判定报告 — {person}"
    out1 = _pdf_from_sections(sections1, p1_base + ".pdf", t1)
    if out1 is None:
        out1 = _html_fallback(sections1, p1_base, t1, now)
    out2 = _pdf_from_sections(sections2, p1_base.replace("待核对清单",
                                                         "自动判定报告") + ".pdf",
                              t2)
    if out2 is None:
        out2 = _html_fallback(sections2,
                              p1_base.replace("待核对清单", "自动判定报告"),
                              t2, now)
    return out1, out2
