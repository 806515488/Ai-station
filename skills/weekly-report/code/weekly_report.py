"""weekly-report 技能代码 —— agent 型：整理对话要点 → 生成周报(md/docx) 并落盘到文件区。

tools() 返回叶子 Tool（registry 会补成 weekly.*）。全部本地执行、零外部依赖，
素材只来自当前对话里用户已经说过的工作要点；不读取用户电脑/服务器目录，也不读写 Git。
演示“skill 自带代码 + 多工具串联 + 产物落盘”的宿主扩展路径。

新手视角：这是【第 3 种代码位置】的技能 —— 代码放在技能目录自己的 code/ 下，
registry 会在 import 时把这个目录临时加进 sys.path（原理见 registry.py 的 _import_entry）。
真实“写你自己的技能”最常改的就是这个文件：换工具逻辑 = 改这里的函数 + tools()。
"""
from __future__ import annotations

import time
import re

from station.core.tool import Tool     # 工具“盒子”（见 tool.py）


def _owner(ctx) -> str:
    """产物归属：对话场景用户 id 在 Thread 上；job/脚本场景 Context 直接带。"""
    t = getattr(ctx, "thread", None)
    return (getattr(t, "user_id", "") if t else "") or getattr(ctx, "user_id", "") or ""


def _week_from_md(md: str) -> str:
    """从周报模板首行《# 工作周报 · YYYY-Www》里反推周标签，兜底取本周。"""
    for ln in (md or "").splitlines():
        s = ln.strip()
        if s.startswith("# 工作周报 · "):
            return s.split("·", 1)[1].strip()
    return time.strftime("%Y-W%W")


def collect_highlights(ctx) -> str:
    """工具①：只整理当前对话里用户已提供的要点，不读取目录或 Git。"""
    thread = getattr(ctx, "thread", None)
    msgs = list(getattr(thread, "msgs", []) or [])
    # 当前会话的用户原话就是本技能唯一可承诺的素材来源；取全部而非只取最后一句，
    # 用户常在“帮我写周报”之后分几条补充完成事项、数据和计划。
    texts = [str(m.get("content") or "").strip() for m in msgs
             if isinstance(m, dict) and m.get("role") == "user"
             and str(m.get("content") or "").strip()]
    if not texts:
        return ("还没有收到可写入周报的工作要点。请用户补充：本周完成、数据/进展、"
                "遇到的问题和下周计划。")
    return "## 用户在本次对话中提供的周报要点\n" + "\n".join(
        f"- {text}" for text in texts)


def render_report(ctx, content: str = "", week: str = "") -> str:
    """工具②：把素材渲染成《工作周报》md 并落盘到文件区，返回下载信息。

    落盘用 station 的 files/store（≈上传到“文件柜”，拿到 id 前端就能下载）。
    """
    from station.files import store as fs
    week = week.strip() or time.strftime("%Y-W%W")   # 空就取“今年-第几周”
    body = (content or "").strip()
    # 让内容读起来像要点列表：如果是一整句话，就包成一条“- xxx”
    if body and not body.startswith("-") and "\n" not in body:
        body = f"- {body}"
    # 模型已经整理成完整章节时直接保存，避免再套一层模板而留下重复标题和占位文字。
    if re.search(r"^##\s+(?:本周完成|数据\s*/\s*进展|遇到的问题|下周计划)", body, re.M):
        lines = body.splitlines()
        if lines and lines[0].startswith("# "):
            body = "\n".join(lines[1:]).strip()
        md = f"# 工作周报 · {week}\n\n{body}\n"
    else:
        md = "\n".join([
        f"# 工作周报 · {week}", "",
        "## 本周完成", body or "（本周没有新增内容）", "",
        "## 数据 / 进展", "（可补充量化数据）", "",
        "## 遇到的问题", "（补充）", "",
        "## 下周计划", "- [ ] 待定", ""])
    fid = fs.save_text(md, ".md", name=f"周报-{week}.md",
                       owner=_owner(ctx), skill_id="weekly-report",
                       group_key=f"weekly:{week}",
                       group_label=f"写周报 · {week}")       # 存进文件区拿 id
    return f"周报已生成并落盘：周报-{week}.md（id={fid}）→ 下载 GET /api/files/{fid}"


def export_docx(ctx, file_id: str = "", content: str = "") -> str:
    """工具③（可选）：把 md 周报转 .docx。python-docx 没装就给提示，别崩。"""
    from station.files import store as fs
    try:
        from docx import Document      # 第三方库；装没装用 try 探一下
    except Exception:                  # noqa
        return "docx 导出不可用：python-docx 未安装（直接用 .md 产物即可）"
    # 输入二选一：给 file_id → 读那个文件；否则直接用 content
    if file_id:
        p = fs.path(file_id)
        md = p.read_text(encoding="utf-8") if p else ""
    else:
        md = content or ""
    doc = Document()                   # 建 Word 文档对象
    from docx.shared import Cm, Pt, RGBColor
    from docx.oxml.ns import qn
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(2)
        section.left_margin = section.right_margin = Cm(2.2)
    # 同时设置中西文字体；默认 Word 主题的蓝色标题不适合正式周报。
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "List Bullet"):
        style = doc.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.3
    doc.styles["Normal"].paragraph_format.space_after = Pt(5)
    doc.styles["Title"].font.size = Pt(20)
    for border in doc.styles["Title"].element.xpath(".//w:pBdr"):
        border.getparent().remove(border)
    doc.styles["Heading 2"].font.size = Pt(13)
    doc.core_properties.author = "station"
    doc.core_properties.title = "工作周报"
    for ln in md.splitlines():         # 逐行解析 markdown → Word 段落/标题/列表
        if ln.startswith("# "):        # 一级标题 → Word Heading 1
            doc.add_paragraph(ln[2:].strip().replace(" · ", " "), style="Title")
        elif ln.startswith("## "):
            doc.add_heading(ln[3:].strip(), level=2)
        elif ln.strip() == "":         # 空行忽略
            pass
        elif ln.strip().startswith("- "):        # 列表项 → Word 项目符号
            doc.add_paragraph(ln.strip()[2:], style="List Bullet")
        else:
            doc.add_paragraph(ln.strip())        # 普通行 → 正文
    import io
    buf = io.BytesIO()                 # 先在内存里存成字节（不急着写盘）
    doc.save(buf)
    week = _week_from_md(md)
    name = f"周报-{week}.docx"
    fid = fs.save_bytes(buf.getvalue(), ".docx", name=name,
                        owner=_owner(ctx), skill_id="weekly-report",
                        group_key=f"weekly:{week}",
                        group_label=f"写周报 · {week}")      # 字节存进文件区
    return f"已导出 docx：{name}（id={fid}）→ 下载 GET /api/files/{fid}"


def tools() -> list[Tool]:
    """暴露本技能的工具清单（registry 会注册成 weekly.*）。"""
    return [
        Tool(name="collect_highlights", label="整理已提供要点",
             description="整理当前对话中用户已经提供的工作要点。不读取用户电脑、服务器目录或 Git。"
                         "若用户只说“写周报”而没有具体事项，应先请他补充完成、进展、问题和计划。",
             run=collect_highlights),
        Tool(name="render_report", label="生成周报",
             description="把素材内容渲染成一份《工作周报》markdown 并落盘到文件区，返回产物下载信息。",
             run=render_report,
             args=[{"name": "content", "type": "str", "desc": "周报正文要点", "required": True},
                   {"name": "week", "type": "str", "desc": "周标签(可省，默认本周)", "required": False}]),
        Tool(name="export_docx", label="导出 Word",
             description="把已生成的 md 周报(file_id)转成 .docx 产物（需 python-docx，可选）。",
             run=export_docx,
             args=[{"name": "file_id", "type": "str", "desc": "md 产物 id", "required": False},
                   {"name": "content", "type": "str", "desc": "或直接给正文", "required": False}]),
    ]
