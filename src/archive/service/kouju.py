"""口径文件的唯一读写器 —— 「文种 → 小类」对照表的解析、追加、摘除与保存。

新手视角（Java 朋友版）：这个模块 ≈ 一个**只认一种文件格式的小 DAO**。
  它管的那份文件是 `skills/archive/口径/文种对照.md`，格式固定成三节：

      ## 用户已确认（最高优先，与下方冲突以本节为准）   ← 机器只往这里「追加」
      ## 基础口径                                      ← 机器只从这里「摘掉冲突的那一个文种」
      ## 已知待校准                                    ← 机器不碰，纯人写

**本模块既"算"也"写"，但写只有一个出口**：
`insert_confirmed` / `drop_conflict` / `apply_amendment` 是**纯函数**（只吃字符串吐字符串，
算出"改完的完整内容"）；`write_text` 是唯一落盘的地方（原子替换 + 保持原换行风格），
而它的**调用者只有 archive 技能那个 approve 类工具 `apply_learning`**。

★ 这条路径几经反复，结论记在这里免得再翻一遍：
  · 最初本模块自己写盘 → 中途改成"技能只算、模型调全局写工具 `station.write`" →
    现在（09-13 定稿）回到"技能自己写"。**变的是前提，不是结论**：全局文件工具
    （`ls`/`read`/`grep`/`write`）09-13 整个删掉了 —— 它们属于"让 AI 看**工作站自己**"
    那一类，业务场景用不到。写口没了，技能就必须自己写。
  · 不变的是那条原则：**同一份文件只能有一条写路径**。现在这条就是 `write_text`，
    且**只有** `apply_learning` 会调它（`tests/test_learning.py` 用 AST 钉住）。
  · "必须用户确认"仍由宿主的**批准闸**保证：`apply_learning` 是 `risk="approve"`，
    用户不点"允许"就写不动 —— 所以 nonce/令牌那套依旧不需要。

**为什么"摘掉一个文种"要这么小心**：基础口径的行是**多文种捆绑**的，例如

      - 干部任免 / 任职 / 免职 / 任免审批表 / 职务变动 → **九-2**

用户只把「任免审批表」挪到别的类，整行删掉会连带毁掉仍然有效的 干部任免/任职/免职/职务变动。
所以本模块做的是**片段级摘除**：只拿走那一个词，同一行其余字节原样保留。
判不准时（命中 0 处或 ≥2 处）**一个字都不删** —— 漏删的代价是两条并存（节首那句
"与下方冲突以本节为准"兜底），错删的代价是毁掉一条仍有效的口径。**宁可漏删。**
"""
from __future__ import annotations

import os
import re
import tempfile              # 原子写：先写临时文件，再整份替换

from archive.domain import classes      # 类别码归一（normalize_category）

# ── 三个节标题 ────────────────────────────────────────────────────────
# ★ 这里存的是**短前缀**：文件里写的是完整标题（带括号说明），用 startswith 就能对上；
#   而"写法变了也还能认出来"比"逐字相等"结实得多。
SECTION_CONFIRMED = "## 用户已确认"
SECTION_BASE = "## 基础口径"
SECTION_CALIB = "## 已知待校准"

# 迁移/兜底时真正写进文件的完整标题（含那句优先级声明 —— 它是"优先"语义的落点）
CONFIRMED_HEAD = "## 用户已确认（最高优先，与下方冲突以本节为准）"

_REL = ("口径", "文种对照.md")          # 相对 skills/archive/ 的路径

# 相对**仓库根**的路径 —— 只用于**显示**（卡片上那句"将写入 …"）与文档引用。
# ★ 不交给模型当参数：写盘由 `apply_learning` 写死 `path()`，模型没有任何办法写别处。
REL_PATH = "skills/archive/口径/文种对照.md"


# ── 文件定位与读写 ────────────────────────────────────────────────────

def path() -> str:
    """口径文件的绝对路径。

    ★ 复用 `archive.skill.loader.SKILL_DIR` 的定位，**别在这里再写一份** ——
      两份定位迟早有一份忘了跟着仓库挪动而失效（loader 那份是"往上三级找到仓库根"）。
      函数内 import 是为了避开模块级循环依赖（loader 反过来不依赖本模块）。
    """
    from archive.skill import loader
    return os.path.join(loader.SKILL_DIR, *_REL)


def read_text() -> str:
    """读口径文件，**换行一律归一成 `\\n`**（内部处理只认一种换行）。

    为什么先归一：这份文件在 Windows 工作区里是 CRLF，而 Python 的 `str.split("\\n")`
    会留下行尾的 `\\r` —— 后面所有"逐行比对/摘词"都会因此对不上。归一一次，后面省心。
    """
    with open(path(), "rb") as f:
        raw = f.read()
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def write_text(text: str) -> None:
    """把文本原子地写回口径文件，**并保持该文件原有的换行风格**。

    ★ **这是全仓唯一能写这份文件的地方**，调用者只有 `apply_learning`
      （`tests/test_learning.py` 用 AST 反查钉住这条）。别在别处再开一个写口 ——
      "同一份文件两条写路径"就是这个仓库反复吃亏的那个半开的门。

    原子 = 先写同目录下的临时文件，再 `os.replace` 覆盖过去；中途异常也不会留下
    半截文件（`os.replace` 在 Windows 上同样是原子的整份替换）。
    ★ 保持原有换行风格：不做的话，一次写入就会把整个文件的换行改掉 ——
      git 里显示成"全文件重写"，diff / blame / 逐行回溯全废（这个坑仓库里踩过）。
    """
    # ★ 部署级只读开关：`STATION_FS_WRITE=0` 时这份文件也不许写。
    #   这条原来由已删的 `tools/guard.py` 管，随文件工具一起没了；但"写仓库里的文件"
    #   这件事现在又回来了（就是这里），所以开关得重新挂在**唯一的写口**上
    #   —— 公网实例照旧可以不改代码、配个环境变量就彻底关写。
    if os.environ.get("STATION_FS_WRITE", "1") == "0":
        raise PermissionError("当前部署关闭了文件写入（STATION_FS_WRITE=0）。")
    p = path()
    style = _newline_style(p)
    body = text.replace("\n", style)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p),
                               prefix=".vzhong-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body.encode("utf-8"))
        os.replace(tmp, p)
    except BaseException:                    # 出错就把临时文件收拾掉，别留垃圾
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _newline_style(p: str) -> str:
    """探出这个文件原本用哪种换行（`\\r\\n` 还是 `\\n`）；文件不存在就当 `\\n`。"""
    try:
        with open(p, "rb") as f:
            head = f.read(65536)
    except OSError:
        return "\n"
    return "\r\n" if b"\r\n" in head else "\n"


def looks_valid(text: str) -> bool:
    """粗验一份文本"像不像"本口径文件 —— 三节都在才算。

    给 `apply_learning` 当**最后一道闸**用：一个写坏的口径文件会让 `loader.hard()`
    读不出内容，而这份文件是**全站共享**的 —— 每一卷的分类都跟着坏。
    所以宁可拒写（并告诉模型重来），也不要写进去一个结构不对的东西。
    """
    lines = (text or "").split("\n")
    return all(_section_span(lines, sec)[0] >= 0
               for sec in (SECTION_CONFIRMED, SECTION_BASE, SECTION_CALIB))


# ── 结构解析（全是纯函数：只吃字符串）────────────────────────────────

def _section_span(lines: list[str], title: str) -> tuple[int, int, int]:
    """找出某个二级节的位置，返回 `(标题行号, 正文起, 正文止)`；没有这节返回 `(-1,-1,-1)`。

    - 标题行号：`## 用户已确认…` 那一行
    - 正文起  ：标题行的下一行
    - 正文止  ：**不含** —— 下一个 `## ` 标题的行号，或文件末尾

    为什么要把"正文起/止"单独给出来：三段式的正确性靠**边界**判断。
    单测里断言"改动行全部落在 [正文起, 正文止) 之内"，就等于钉住了
    "机器只动这一节"这条纪律 —— 不然哪天一个手滑改到别处，没人发现。
    """
    head = -1
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(title):
            head = i
            break
    if head < 0:
        return -1, -1, -1
    body_start = head + 1
    body_end = len(lines)
    for i in range(body_start, len(lines)):
        if lines[i].lstrip().startswith("## "):
            body_end = i
            break
    return head, body_start, body_end


_NOISE = re.compile(r"[\s《》()（）【】\[\]，。：:、·\-—_]+")


def norm(s: str) -> str:
    """文种名的归一形式：去掉空白与书名号/括号/标点，只留实义字。

    用来做"这两个写的是不是同一个文种"的判断（`《任免审批表》` vs `任免审批表` → 相等）。
    """
    return _NOISE.sub("", s or "")


def _left_right(frag: str) -> tuple[str | None, str | None]:
    """把一条片段 `- 文种A / 文种B → **九-2**` 切成 `(左边文种串, 右边类别串)`。

    没有 `→` 就不是一条对照（比如一句说明文字）→ 返回 `(None, None)`，调用方跳过。
    """
    if "→" not in frag:
        return None, None
    left, _, right = frag.partition("→")
    return left, right


def cat_of(frag: str) -> str | None:
    """读出片段右边那个类别码（规整成合法小类码）；读不出返回 None。

    ★ 不能直接把右边整串丢给 `normalize_category` —— 因为右边常带类名
      （`**六 党团**`），整串过不去。要先**抠出开头那个类号**再归一。
    """
    _, right = _left_right(frag)
    if right is None:
        return None
    right = right.replace("－", "-").replace("—", "-")
    m = re.match(r"[\s*]*([一二三四五六七八九十]+(?:-\d)?)", right)
    return classes.normalize_category(m.group(1)) if m else None


def _top_parts(s: str) -> list[tuple[int, int]]:
    """把文种串切成一个个"顶层文种"的 `(起, 止)` 下标 —— 按 `/`、`、` 切，**括号里不切**。

    ★ 为什么括号里不切：现网口径里就有 `党内表彰(优秀党员/先进支部推荐审批) → **六**`
      这样的行。括号里那个 `/` 是**组内**的分隔，在它上面切出来的"文种"是两个半截
      （`优秀党员` / `先进支部推荐审批)`），拿半截去做匹配和摘词，会把那一行改成
      残缺的（09-13 的 review 抓到的真 bug）。括号组整体算**一个**文种。
    返回的相邻两段之间，分隔符就在下一段的 `start - 1`。
    """
    spans, start, depth = [], 0, 0
    for i, ch in enumerate(s):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        elif ch in "/、" and depth == 0:
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(s)))
    return spans


def tokens_of(frag: str) -> list[str]:
    """把片段左边的文种串拆成一个个**单个文种**（原始写法，未归一）。

    ★ **必须与 `_remove_token` 共用 `_top_parts` 的切法** —— 两边切法不一致就会出现
      "匹配得到、却摘不掉"的静默不一致（review 抓到的正是这个）。
    """
    left, _ = _left_right(frag)
    if left is None:
        return []
    left = left.lstrip("-").strip()
    out = []
    for s, e in _top_parts(left):
        p = left[s:e].strip().strip("*").strip()
        if p:
            out.append(p)
    return out


def _list_lines(lines: list[str], start: int, end: int) -> list[str]:
    """取出某节里所有 `- ` 开头的条目行（缩进续行不算，它们是上一条的补充说明）。"""
    return [ln for ln in lines[start:end] if ln.startswith("- ")]


def confirmed_entries(text: str) -> list[str]:
    """「用户已确认」节里现有的全部条目行（去重时用它做双保险）。"""
    lines = text.split("\n")
    _, bs, be = _section_span(lines, SECTION_CONFIRMED)
    return _list_lines(lines, bs, be) if bs >= 0 else []


# ── 三个写操作（纯函数）──────────────────────────────────────────────

def insert_confirmed(text: str, entry: str) -> str:
    """把一条已确认条文**追加**到「用户已确认」节的末尾，返回新文本。

    插在"该节最后一个非空行的后面"，所以节末尾原本隔开下一节的那个空行保持不动。
    节是空的时候（只有标题）就直接跟在标题后面的空行位置。
    """
    lines = text.split("\n")
    head, bs, be = _section_span(lines, SECTION_CONFIRMED)
    if head < 0:                     # 没有这一节 → 先补出来，再插
        text = ensure_sections(text)
        lines = text.split("\n")
        head, bs, be = _section_span(lines, SECTION_CONFIRMED)
        if head < 0:
            return text              # 兜底都补不出来（文件结构怪）→ 原样返回，调用方会看到没写进去
    last = be - 1
    while last >= bs and not lines[last].strip():
        last -= 1
    if last < bs:                    # 空节：直接放在正文起点的位置
        lines[bs:bs] = [entry]
    else:
        lines[last + 1:last + 1] = [entry]
    return "\n".join(lines)


def _remove_token(frag: str, token: str) -> str:
    """从一条片段里摘掉指定的那个文种，**其余字节原样保留**。

    做法：只切"箭头左边"那一串，按它原本用的分隔符（`/` 或 `、`）拆开，
    滤掉命中的那一个，再用**同一个分隔符**拼回去 —— 所以没被摘掉的部分连空格都不变。
    找不到那个文种（比如文件已被手工改过）→ 原样返回，调用方据此判定"没删成"。
    """
    i = frag.find("→")
    if i < 0:
        return frag
    left, right = frag[:i], frag[i:]
    want = norm(token)
    spans = _top_parts(left)
    for k, (s, e) in enumerate(spans):
        raw = left[s:e]
        if norm(raw.strip().lstrip("- ").strip()) != want:
            continue
        # ★ 括号组是**复合文种**（`党内表彰(优秀党员/先进支部…)`），摘掉它会连累括号里
        #   别的文种 —— 宁可漏删，一个字都不动，让调用方如实说"没摘掉"。
        if any(c in raw for c in "()（）"):
            return frag
        if len(spans) == 1:                  # 整条就这一个文种 → 片段整个作废
            return ""
        if k > 0:                            # 连同**前面**那个分隔符一起摘（★ 别忘了 right）
            return left[:s - 1] + left[e:] + right
        # 第一段：摘掉它 + **后面**那个分隔符。★ 行首的 `- ` 必须保住 ——
        # 丢了这行就不以 `- ` 开头，`_list_lines` 不再把它当条目，格式也垮了。
        return "- " + left[e + 1:].lstrip() + right
    return frag                              # 没找到 → 原样返回（调用方据此判"没删成"）


def drop_conflict(text: str, drop: dict | None) -> tuple[str, bool]:
    """按提案里存的 `drop` 描述，从「基础口径」摘掉那一个文种。

    返回 `(新文本, 是否真摘掉了)`。**任何一处对不上（原行没了、片段变了、文种找不到）
    都什么都不删、返回 False** —— 调用方据此降级成"只追加"，并在卡片上如实说明。
    `drop` 的形状：`{"line": 原行全文, "frag": 原片段, "token": 要摘的文种, "old": 原类别}`。
    """
    if not drop:
        return text, False
    want_line = (drop.get("line") or "").strip()
    want_frag = (drop.get("frag") or "").strip()
    token = drop.get("token") or ""
    if not want_line or not token:
        return text, False
    lines = text.split("\n")
    _, bs, be = _section_span(lines, SECTION_BASE)
    if bs < 0:
        return text, False
    for i in range(bs, be):
        if lines[i].strip() != want_line:
            continue
        parts = re.split(r"[；;]", lines[i])
        kept = []
        changed = False
        for p in parts:
            if p.strip() == want_frag:
                p2 = _remove_token(p, token)
                if p2 == p:                  # ★ 没真摘掉（文种不在 / 是括号组）→ 不算数
                    kept.append(p)
                    continue
                changed = True
                if not tokens_of(p2):        # 这个片段被摘空了 → 整段丢掉
                    continue
                p = p2
            kept.append(p)
        if not changed:
            # 找到了行、却没改动任何字节 → 老老实实说"没删成"，让调用方降级成"只追加"，
            # 而不是回一个 True 让上层告诉用户"已摘掉"（review 抓到的假报告）。
            return text, False
        kept = [k for k in kept if k.strip().lstrip("-").strip()]
        if not kept:                         # 整行都空了 → 连行一起删
            del lines[i]
        else:
            lines[i] = "；".join(kept)
        return "\n".join(lines), True
    return text, False


def apply_amendment(text: str, entry: str, drop: dict | None = None
                    ) -> tuple[str, bool]:
    """一次"确认口径"的全部落地：先从基础口径摘掉冲突文种，再往已确认节追加。

    顺序很重要 —— **先摘后加**。反过来的话，"追加"一步会改变行号，
    后面按行定位的摘除就得重新算位置（纯函数里最烦的一类错）。
    返回 `(新文本, 是否真的把冲突摘掉了)`。
    """
    text, dropped = drop_conflict(text, drop)
    return insert_confirmed(text, entry), dropped


# ── 冲突侦测（提案时用；纯函数）──────────────────────────────────────

def find_conflict(text: str, title: str, new_cat: str) -> dict | None:
    """在新结论「《title》→ new_cat」与「基础口径」之间找冲突，返回摘除方案或 None。

    判定分两档（**从紧到松**，这是刻意的）：
      ① **精确档**：基础口径里某个文种与 title 归一后**完全相等**；
      ② **包含档**：一方是另一方的子串，且**短的那方 ≥ 4 字**。
         加长度门槛是为了挡掉"工资""体检"这种两字词满天飞地误命中。
      两档都优先看精确档；只有精确档一条都没有时才退到包含档。

    为什么"命中 ≥2 处就放弃"：基础口径里两处都提到这个文种、却给了不同类，
    说明这份口径本身有歧义 —— 这时候机器自作主张删哪一处都是错的，交给人。
    """
    t = norm(title)
    if not t:
        return None
    lines = text.split("\n")
    _, bs, be = _section_span(lines, SECTION_BASE)
    if bs < 0:
        return None

    exact: list[tuple[str, str, str]] = []      # (整行, 片段, 文种)
    fuzzy: list[tuple[str, str, str]] = []
    for ln in _list_lines(lines, bs, be):
        for frag in re.split(r"[；;]", ln):
            for tok in tokens_of(frag):
                # 括号组是复合文种，`_remove_token` 摘不掉它（摘了会连累组里别的文种）
                # —— 那就**别报成冲突**，否则预览卡会承诺一个做不到的"同时摘掉"。
                if any(c in tok for c in "()（）"):
                    continue
                n = norm(tok)
                if not n:
                    continue
                if n == t:
                    exact.append((ln, frag.strip(), tok))
                elif len(n) >= 4 and (n in t or t in n):
                    fuzzy.append((ln, frag.strip(), tok))
    if not exact and not fuzzy:
        return None
    # 精确档优先，但**不是**"有精确档就无视别的" —— 同一片段里那些"包含关系"的命中
    # （干部任免 之于 干部任免审批表）只是同一条目的组成部分，不算第二处；
    # 落在**别的片段/别的行**上的命中则是真的第二处，那才叫歧义 → 交给人。
    exact_spots = {(h[0], h[1]) for h in exact}
    hits = exact + [h for h in fuzzy if (h[0], h[1]) not in exact_spots]
    spots = {(h[0], h[1]) for h in hits}
    if len(spots) != 1:                  # 命中 ≥2 处 → 宁可不删
        return None
    ln, frag = next(iter(spots))
    # 同一片段里可能同时含"干部任免"和"任免审批表"两个候选 → 取**最长**的那个，
    # 它才是用户真正改掉的那件事（短的那个是它的一部分，通常还要保留）。
    token = max((h[2] for h in hits if (h[0], h[1]) == (ln, frag)),
                key=lambda x: len(norm(x)))
    old = cat_of(frag)
    if old is None or old == new_cat:    # 本来就归这一类 → 不叫冲突
        return None
    return {"line": ln, "frag": frag, "token": token, "old": old}


# ── 条文渲染与结构兜底 ───────────────────────────────────────────────

def render_entry(title: str, category: str, stamp: str, count: int = 1) -> str:
    """把一条学习结论渲染成写进口径文件的条文行（**格式与基础口径那批一致**）。

    形如：`- 《任免审批表》→ **十**（2026-09-13 用户确认，3 次修正）`
    用粗体包类号、箭头分隔 —— 与文件里原有条目同款，模型读起来才不割裂。
    """
    extra = f"，{count} 次修正" if count and count > 1 else ""
    return f"- 《{title}》→ **{category}**（{stamp} 用户确认{extra}）"


def ensure_sections(text: str) -> str:
    """确保「用户已确认」「基础口径」两节都在（**幂等**：结构已对就原样返回）。

    给两种情况兜底：① 老格式的文件（就是本模块上线前那份扁平的）；
    ② 有人手工把节标题删了。做法是把缺失的节标题插在**第一条 `- ` 列表行之前** ——
    那条列表行本来就属于"基础口径"，插在它前面正好把老内容整段圈进去。
    """
    lines = text.split("\n")
    has_conf = _section_span(lines, SECTION_CONFIRMED)[0] >= 0
    has_base = _section_span(lines, SECTION_BASE)[0] >= 0
    if has_conf and has_base:
        return text
    anchor = next((i for i, ln in enumerate(lines) if ln.startswith("- ")), None)
    if anchor is None:               # 连一条列表都没有 → 无处可插，别硬来
        return text
    ins: list[str] = []
    if not has_conf:
        ins += [CONFIRMED_HEAD, ""]
    if not has_base:
        ins += [SECTION_BASE, ""]
    lines[anchor:anchor] = ins
    return "\n".join(lines)
