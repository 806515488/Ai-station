"""口径/提示词加载器：从仓库根 skills/archive（= archive skill 目录）读模板与文种口径。

engine 与后续实现一律经此加载；提示词/口径不写死在代码里。
skills/archive 同时是 station 的开放 skill（manifest.json, type=agent，对话入口），
prompts/口径 是它的识别资产——识别引擎与对话工具都从这里拿口径，随真实卷在此更新。

新手视角（Java 朋友版）：把“识别规则”当成【外置配置文件】来读，是这套架构最重要的一招。
  - 传统做法：把判定规则写死在代码里 → 每次调规则都要改代码、重新部署、还容易和真实口径脱节。
  - 这里：规则 = 一堆 .md（human-readable），engine 运行时才来读。于是“改识别 = 改 md”，
    不用碰 engine 代码 —— 等价于“把策略/提示词放在配置/资源目录，代码只做渲染和调用”。
  - _render 是极简模板引擎：把提示词里的 {占位符} 换成运行时内容（类似 String.format / 模板注入）。
"""
from __future__ import annotations

import os
import re

_PKG = os.path.dirname(os.path.abspath(__file__))              # .../src/archive/skill
_REPO = os.path.abspath(os.path.join(_PKG, "..", "..", ".."))  # 仓库根（skills 在此）
SKILL_DIR = os.path.join(_REPO, "skills", "archive")           # 技能目录（读 md 都在这）

# 注意：这里 repo 定位向上 3 级（skill 目录 → archive → src → 仓库根）。
# 别写成固定绝对路径 —— 仓库挪位置也能跑。


def _read(rel: str) -> str:
    """读技能目录下的一个 md 并去首尾空白。

    rel 形如 "prompts/mark.md" 或 "口径/文种对照.md"；
    rel.split("/") 拆成片段再用 join 拼回（避免不同系统分隔符问题）。
    """
    p = os.path.join(SKILL_DIR, *rel.split("/"))
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


def _render(tpl: str, **kw) -> str:
    """极简模板替换：把模板里所有 {名字} 换成传进来的值。

    kw 是关键字参数 dict：调用 _render(t, A=1) 收到 kw={"A":1}。
    .replace("{A}", str(1)) 就是最朴素的模板注入（不是真模板引擎，够用）。
    """
    for k, v in kw.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


# ── 表册对照：同一份 md 的两种读法 ─────────────────────────────────
# 《口径/表册对照.md》既是"给人/模型看的说明"（注入建档提示词），
# 又是"给代码看的结构化表"（seg 按身份归组时要用册名/大类/版序）。
# 两者共用一份文件 = 口径改了代码立刻跟着改，不会一边新一边旧。
_PARSE_ERROR = ""          # 最近一次解析失败的原因（测试与排障用；正常时为空串）


def formbooks() -> dict:
    """把《口径/表册对照.md》解析成结构化表；解析不出来返回 {}（调用方据此回退）。

    返回形如：
      {"干部履历表": {"cat": "一", "whole": True, "title": "干部履历表",
                     "aliases": ["履历表", …],
                     "order": {"封面": 1, "入党志愿": 2, …, "其他需要说明的情况": 99}},
       …}
    whole=True 表示"整册一份"（多页同一本册子）；False 表示单页件（同表名会有很多份）。
    order 既用来做"栏目名 → 是哪本册"的反查，也用来排册内页序。
    """
    global _PARSE_ERROR
    try:
        books = _parse_formbooks(_read("口径/表册对照.md"))
    except Exception as e:                     # 文件缺失/语法坏 → 一律当"没有对照表"
        _PARSE_ERROR = f"{type(e).__name__}: {e}"
        return {}
    if not books:
        _PARSE_ERROR = "对照表里没解析到任何册子"
    else:
        _PARSE_ERROR = ""
    return books


def _parse_formbooks(text: str) -> dict:
    """解析《表册对照.md》。规则见该文件开头的"格式约定"，别改一边忘一边。

    解析要点（为什么这么写）：
      - 只把**含 `- 大类：` 的 `## 段`**当册子 —— 文件里还有别的 ## 小标题（如"单页件"），
        靠"有没有大类"这一条就能把它们跳过，不用给标题打特殊记号。
      - 键值行的冒号全角半角都认（写文档的人容易混用）。
      - "版序："之后的 `序号 栏目名` 行收进 order；遇到下一条 `- 键：` 或新的 `## ` 就结束。
    """
    books: dict = {}
    cur: dict | None = None
    in_order = False                            # 是否正处在"版序"列表里

    def flush():
        """把攒好的一段收进结果 —— 没有大类的不算册子（例如"单页件"那种说明性小标题）。"""
        if cur and cur.get("cat"):
            books[cur["name"]] = {
                "cat": cur["cat"],
                "whole": cur.get("whole", False),
                "title": cur.get("title") or cur["name"],
                "aliases": cur.get("aliases", []),
                "clue": cur.get("clue", ""),
                "order": cur.get("order", {}),
            }

    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("## "):                 # 新的一段开始
            flush()
            cur = {"name": s[3:].strip(), "aliases": [], "order": {}}
            in_order = False
            continue
        if cur is None:
            continue
        if s.startswith("- "):                  # 一条属性
            in_order = False
            body = s[2:]
            key, _, val = body.partition("：") if "：" in body else body.partition(":")
            key, val = key.strip(), val.strip()
            if key == "版序":
                in_order = True
                continue
            if key == "大类":
                cur["cat"] = val
            elif key == "整册一份":
                cur["whole"] = val.startswith("是")
            elif key == "规范材料名":
                cur["title"] = val
            elif key == "别名":
                cur["aliases"] = [x.strip() for x in re.split(r"[/、]", val) if x.strip()]
            elif key == "线索":
                cur["clue"] = val
            continue
        if in_order:                            # 版序列表行： "  3 个人基本情况"
            m = re.match(r"^(\d+)\s+(\S.*)$", s)
            if m:
                cur["order"][m.group(2).strip()] = int(m.group(1))
    flush()
    return books


# ── 每个环节的“读提示词”入口（engine/graph.py 的节点调这些）──────────
def mark() -> str:
    """建档提示词（每页视觉读内容用）。

    ★ FORMBOOK 里是整份《表册对照》，放进提示词让模型知道"这页可能是哪本册子的栏目页、
    栏目名长什么样" —— 这是模型能不能填对 form 字段的关键。改这份对照 =
    改提示词 = OCR 缓存键（sha256 提示词[:12]）随之改变，旧缓存失效，这是预期行为。
    """
    return _render(_read("prompts/mark.md"),
                   FORMBOOK=_read("口径/表册对照.md"))


def hard() -> str:
    """硬口径：文种→小类对照（随真实卷校准更新，最重要）。"""
    return _read("口径/文种对照.md")


def classify(cards: str, subs: str) -> str:
    """定类提示词：把一批“份的首页内容卡”+ 类表 渲染进 classify.md 模板。

    SUBS = “类名表”，HARD = 文种口径，CARDS = 每份首页卡 —— 全在 md 里拼给模型。
    """
    return _render(_read("prompts/classify.md"),
                   SUBS=subs, HARD=hard(), CARDS=cards)


def resolve(orphans: str, buckets: str) -> str:
    """残页裁决提示词：判断"认不出属于哪本册子的页"该并进哪一份，还是自成一 份。

    为什么要有这一步：按身份归组（seg）是确定性的 —— 判不出 `form` 的页它一律不敢并
    （猜错会把两本册子并成一本，比拆散更难发现）。剩下这十几页交给文本模型看一眼
    "它像下面哪一份"，比硬猜稳，而且**输出很短**（只回答归属，不复述内容），
    不会重蹈早期"一次生成 103 条材料导致读超时"的覆辙。
    """
    return _render(_read("prompts/resolve.md"),
                   ORPHANS=orphans, BUCKETS=buckets)


def orient() -> str:
    """方向探测提示词：把同一页的两个候选方向并排给模型，问哪张文字是正立的。

    为什么是"二选一"而不是问"这张图正不正"：后者（绝对判断）实测不可靠 —— 会把
    明显横躺的图判成"正立"（见 docs/conventions.md 坑区）。两个候选摆一起比，
    判断题变成选择题，可用性完全不同。
    """
    return _read("prompts/orient.md")
