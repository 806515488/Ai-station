"""全局工具 · 档二：长期记忆（remember / recall）。

先说清楚它**不是**什么：`core/compact.py` 的"压缩"是"这次聊太长了，把前面折成
一句摘要" —— 那是**同一次会话内**的上下文管理，聊完就没了。
这里管的是另一件事：**上次说过的话，下次还得记得**。比如
  · "导出 PDF 一律 A4 竖版"（口径）
  · "用户是初学者，代码注释要详细"（偏好）
  · "无犯罪记录证明归十类"（口径）
这些今天只能靠往 CLAUDE.md 里硬塞，模型自己写不了。有了这两个工具，它就能自己攒。

存法故意选**最土的**：一篇 markdown 存一条事实 + 一个 `MEMORY.md` 索引。
为什么不拿现成的开源方案（选型结论，别再翻一遍）：
  · `mem0`（约 5.4 万 star）要 Qdrant + Neo4j，而且**每次写入都调一次 LLM** 做抽取
    —— 对"一个人的工作站"是杀鸡用牛刀，还每次都烧 token。
  · `basic-memory`（约 3 千 star）思路最对（markdown 即事实源），但它是 **AGPL-3.0**
    的完整 MCP 服务，还带 SQLite 索引 + FastEmbed。本仓 P4 要开源，AGPL 是雷。
  · 所以自己写 —— 加上注释也就百来行，零新依赖，且**完全确定性**（没有 embedding
    就没有"为什么这条没搜出来"的玄学）。

检索用关键词过滤（分词 = 空格切），不上向量库。单用户、几百条以内完全够用；
真到几千条再说 —— 到那天的升级路径是把 `recall` 换成读全文，索引文件本身没变。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from station import config
from station.core.tool import Tool

_INDEX = "MEMORY.md"          # 索引文件名（跟 Claude Code 的记忆机制同构，用户熟）
_KINDS = ("偏好", "口径", "事实", "项目")     # kind 的取值 —— 只影响给人看的标签
_RECALL_MAX = 5               # 一次最多回几条（防一句模糊的话把上下文塞满）
_BODY_MAX = 1200              # 单条正文回给模型时的截断长度


def _dir() -> Path:
    """记忆目录 data/station/memory/（不存在会自动建，见 config.sub）。"""
    return config.sub("memory")


def _slug(title: str, text: str) -> str:
    """给一条记忆起文件名。

    中文标题没法直接当文件名（跨平台/编码都麻烦），所以用
    `时间戳-内容哈希前6位` —— 时间戳让人一眼看出先后，哈希保证同一秒内写两条
    也不会互相覆盖（`remember` 可能被连着调两次）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"{stamp}-{digest}"


def _split_front(text: str) -> tuple[dict, str]:
    """把 `---` 包起来的头部（frontmatter）和正文拆开。

    返回 (字段 dict, 正文)。没有头部就返回 ({}, 原文)。
    头部格式故意做得极简 —— 只有 `键: 值` 一行一条，不引 yaml 库。
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta: dict = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, body.lstrip("\n")


def _read_all() -> list[dict]:
    """读全部记忆条目，按修改时间倒序（新的在前）。

    `MEMORY.md` 自己跳过 —— 它是索引，不是一条记忆。
    """
    out = []
    for p in _dir().glob("*.md"):
        if p.name == _INDEX:
            continue
        try:
            meta, body = _split_front(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        out.append({"file": p.name, "title": meta.get("title", p.stem),
                    "kind": meta.get("kind", ""), "created": meta.get("created", ""),
                    "body": body.strip(), "mtime": p.stat().st_mtime})
    return sorted(out, key=lambda d: d["mtime"], reverse=True)


def t_remember(ctx, text: str, title: str = "", kind: str = "事实") -> str:
    """记下一条跨会话的事实/偏好/口径。"""
    text = (text or "").strip()
    if not text:
        return "要记的内容是空的。"
    if kind not in _KINDS:                 # 不认识的 kind 不报错，归到"事实"（别为标签挡路）
        kind = "事实"
    # 去重：内容一模一样就不再写一条（模型容易把同一件事说两遍）
    for m in _read_all():
        if m["body"] == text:
            return f"这条已经记过了（{m['file']}）。"

    name = _slug(title or text, text)
    path = _dir() / f"{name}.md"
    # 标题：没给就用正文头一行前 24 字（当目录里的一行摘要）
    head = (title or text.splitlines()[0]).strip()[:24]
    path.write_text(
        f"---\ntitle: {head}\nkind: {kind}\ncreated: {time.strftime('%Y-%m-%d %H:%M')}\n---\n\n{text}\n",
        encoding="utf-8")
    _reindex()
    return f"已记住（{kind}）：{head}"


def t_recall(ctx, query: str = "") -> str:
    """回忆：不给 query就回全部索引；给了就按关键词找。"""
    items = _read_all()
    if not items:
        return "还没有任何长期记忆。"
    if not query.strip():
        lines = [f"- [{m['title']}]({m['file']}) ({m['kind']})" for m in items]
        return f"共 {len(items)} 条长期记忆：\n" + "\n".join(lines)

    # 分词 = 按空格切；中文没有空格时就整句当一个词。多个词之间是**与**的关系
    # （都出现才算命中）—— 这比"或"更接近"我想找的就是这条"。
    terms = [t for t in re.split(r"\s+", query.strip().lower()) if t]
    hits = [m for m in items
            if all(t in (m["title"] + m["body"]).lower() for t in terms)]
    if not hits:
        return f"没找到和 `{query}` 相关的记忆。当前共 {len(items)} 条，可以不带 query 看索引。"

    parts = []
    for m in hits[:_RECALL_MAX]:
        body = m["body"]
        if len(body) > _BODY_MAX:
            body = body[:_BODY_MAX] + "…（已截断）"
        parts.append(f"【{m['title']}】（{m['kind']}，{m['created']}）\n{body}")
    more = f"\n\n（还有 {len(hits) - _RECALL_MAX} 条没显示）" if len(hits) > _RECALL_MAX else ""
    return f"找到 {len(hits)} 条：\n\n" + "\n\n".join(parts) + more


def _reindex() -> None:
    """重建索引文件 MEMORY.md —— 每次写完重排一遍（几十条的量，重排比增量维护简单且不会错）。"""
    items = _read_all()
    lines = ["# 长期记忆索引", "",
             "> 一条一行，正文在各自的 .md 里。由 station.remember 自动维护，别手改。", ""]
    lines += [f"- [{m['title']}]({m['file']}) — {m['kind']}" for m in items]
    (_dir() / _INDEX).write_text("\n".join(lines) + "\n", encoding="utf-8")


def tools() -> list[Tool]:
    """本组工具清单。"""
    return [
        Tool(name="remember", label="记住这件事",
             description="记下一条**用户明确要求长期记住**的约定（跨会话有效）。"
                         "★ 只在用户说了「记一下」「以后都这样」「别忘了」这类话时用。"
                         "★★ 它记的是**用户的**长期约定，**不是你的草稿纸** ——"
                         "你的推理过程、刚弄清的工具用法、这一轮的教训与待办，"
                         "**一律不要往里写**（那些是你当下该做的事，不是要跨会话记住的用户约定）。"
                         "也不许「先记一笔再动手」—— 先把用户要的事做完。",
             run=t_remember,
             args=[{"name": "text", "type": "str", "desc": "要记住的完整内容（一句话说清楚，别写'同上'）"},
                   {"name": "title", "type": "str", "desc": "短标题（十几字以内），不给就从正文取头一行", "required": False},
                   {"name": "kind", "type": "str", "desc": "偏好 / 口径 / 事实 / 项目，默认 事实", "required": False}]),

        Tool(name="recall", label="回忆一下",
             description="查长期记忆里有没有**用户以前定下的约定**。"
                         "★ 只在两种时候用：用户问起以往约定（「我以前说过吗」「按我的习惯来」），"
                         "或你正要做的选择可能和以前的约定冲突。"
                         "★★ **不要每轮都调**：回答普通问题、动手做任务之前**都不需要**先查一遍 ——"
                         "用户刚说过的话就在上面的对话里。查过没找到就直接做事，**别反复查**。"
                         "不给 query 就列出全部记忆的索引。",
             run=t_recall,
             args=[{"name": "query", "type": "str", "desc": "关键词，多个词用空格分开（都要命中）；留空则列出全部", "required": False}]),
    ]
