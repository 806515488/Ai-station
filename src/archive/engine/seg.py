"""归组：把一叠照片按【身份】分成若干份材料（不再依赖照片顺序）。

新手视角（Java 朋友版）：这是识别链的【第二站】——建档(mark)给每页打了"内容卡"，
seg 负责把"哪些页属于同一份材料"算出来。

═══ 为什么不能用"顺序"了（2026-09-12 用户确认，必读）═══
最早的做法是：顺着照片顺序走，比较"上一页和下一页像不像同一份"，像就并、不像就切。
前提是**照片顺序 = 档案实物页序**。这个前提是错的 —— 用户上传的照片顺序是随机的
（"第 1 张和第 2 张本来应该连续，但我传给你的可能是第 1 张和第 50 张"）。
顺序一乱，"相邻两页像不像"就没有任何意义：同一本册子的 13 页散落在全卷，
每一页都会被判成"新的一份"。实测：干部履历表被拆成 5~8 条、入党志愿书的内页
被拆成"入党介绍人的意见""誓词""总支部审查意见"等一堆单页材料。

═══ 现在的判据：身份相同 ═══
一份材料的页共享**身份特征**：同一本册子（`form`）、同一个日期。于是：
    身份键 = (form, 日期)；form 是"整册一份"的册子 → 忽略日期，整本并成一份
例如：
    · 干部履历表 13 页 → form 都是"干部履历表" → 一份 ✓
    · 山西省企业职工岗位技能工资变动审批表 8 张（日期各异）→ 8 份 ✓
这与它们在**第几张照片**上完全无关。

═══ 判不出 form 怎么办：五层兜底，逐层降低把握，最后一层宁可"不并" ═══
    L0 页面印刷的册名（模型给的 form）
    L1 栏目名→册 的**唯一**映射（"誓词"只可能在入党志愿书里）—— 纯规则，不调模型
    L2 栏目名歧义（"工作经历"两本册子都有）→ 用 form 仲裁，但**必须与栏目相符**
    L3.5 仍不定但候选册同属一个大类 → 并成一个"同类册桶"并标存疑（人一句话就能拆开）
    L4 毫无线索 → 保持独立，交给 graph 的 resolve 节点问模型
    L5 resolve 也拿不准 → 保持独立 + 存疑（**绝不猜并**：错的合并比拆散更难发现）

═══ 一个安全闸门 ═══
form 整体覆盖率 < 50% 说明模型这一步没读出来（可能提示词失效/换了个笨模型），
此时**自动退回旧的顺序切份**（_legacy_order_split），并留一条说明让上层报给用户 ——
宁可退回旧行为，也不能让整卷变成 126 条单页材料。
"""
from __future__ import annotations

import difflib      # 文本相似度：SequenceMatcher 给两段文字算"像不像"
import re           # 正则：标题归一化（去空格/标点）用

from archive.domain import classes          # 目录序（CAT_RANK/date_key/dir_sort_key）
from archive.skill import loader            # 表册对照（册名/大类/版序）从这里读

# 最近一次 build_candidates 需要告诉上层的说明（例如"回退到顺序切份了"）。
# 用模块级变量而不是改返回值：调用方（node_segment 与既有测试）都按"返回 list"写的，
# 改返回值形状会波及一片。build_candidates 由 node_segment 单线程调用，没有竞态。
LAST_NOTE: str = ""

# 覆盖率闸门阈值：form 认得出来的页占比低于它 → 整套身份归组不可信 → 回退
_MIN_COVERAGE = 0.5

# 单页件里"日期也没有"时的兜底：不并（宁可拆散，也别把两张不同的任免表并成一份）
_NO_DATE = "__nodate__"


# ── 读"内容卡"的小工具（每个都从 record 里取出建档时存下的字段）──────────
def _mark(r: dict) -> dict:
    """从一条 record 取它的"建档卡" ocr.mark（见 domain/models.py）。没有给空 dict。"""
    return (r.get("ocr") or {}).get("mark") or {}


def _t(r):
    """取建档卡里的 title（表头/标题），去空格；没有给 None。"""
    v = _mark(r).get("t")
    return (v or "").strip() or None


def _doc(r):
    """取文号。"""
    v = _mark(r).get("doc")
    return (v or "").strip() or None


def _norm(s) -> str:
    """把字符串"归一化"：全变小写 + 删掉空白/各种括号引号标点。

    目的：比较标题时忽略书写差异。"干部任免审批表"、"干部 任免 审批表" 归一化后一样。
    re.sub(正则, 替换成空, 文本)：把正则匹配到的所有字符删掉。
    """
    return re.sub(r"[\s【】\[\]（）()、《》\"'“”.．,，:：;；\-—_·\t]+", "",
                  (s or "").strip().lower())


def _date(r) -> tuple | None:
    """取制成时间 (年,月,日)；没年就给 None。"""
    d = _mark(r).get("date") or {}
    return (d.get("y"), d.get("m"), d.get("d")) if d.get("y") else None


def _form(r):
    """取"这页属于哪本册子"（归组最重要的一把钥匙）。"""
    v = _mark(r).get("form")
    return (v or "").strip() or None


def _pk(r):
    """取"栏目名"（册子内页的小标题，如"工作经历""入党介绍人的意见"）。"""
    v = _mark(r).get("pk")
    return (v or "").strip() or None


def _pg(r):
    """取页面上【印刷的】页码/栏目序号（int）；没有给 None。"""
    v = _mark(r).get("pg")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# ── 表册对照的查表工具 ────────────────────────────────────────────
def _book_index(books: dict) -> dict:
    """把"册名/别名 → 规范册名"编成一张归一化查表。

    为什么要归一化：模型可能写"干部履历表"也可能写"履历表"或有空格/书名号，
    全都该指到同一本册子。归一化后做 key，查表就稳。
    """
    idx: dict[str, str] = {}
    for name, b in books.items():
        idx[_norm(name)] = name
        for a in b.get("aliases", []):
            idx.setdefault(_norm(a), name)
    return idx


# 栏目名里的"噪声词"：同一个栏目，不同表格/不同年份的写法会来回加这些词。
# 实测一栏就有三种写法：家庭成员及主要社会关系情况表 / 家庭主要成员及社会关系情况 /
# 家庭成员及社会关系情况登记 —— 去掉噪声后它们是同一个 key。
_PK_NOISE = ("主要", "情况", "登记", "有关", "等", "及", "的", "表")


def _pk_norm(s) -> str:
    """栏目名归一：先做通用归一（去标点空格），再逐个去掉噪声词。

    注意顺序：先去掉长词（"主要""情况"）再去掉单字（"及""表"），否则"情况"会被拆散。
    """
    t = _norm(s).replace("其它", "其他")     # 真实卷里"其它/其他"混用（同一栏两种写法）
    for w in _PK_NOISE:
        t = t.replace(w, "")
    return t


def _key_match(a: str, b: str) -> bool:
    """两个（已归一的）栏目名算不算同一个 —— 全等，或一个包含另一个。

    包含是为了兜"多写几个字"的情况；长度设 3 字下限，避免"说明"这种短词把
    "填表说明/使用说明"一网打尽。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


# 信纸抬头（"XX公司稿纸/公用笺/简纸"）：那是**信纸上印的字**，不是材料名、更不是册名。
# 手写稿（入党申请、思想汇报）大量写在信纸上，模型很容易把抬头当标题 —— 必须挡掉，
# 否则会冒出叫"山西省运城汽车运输公司稿纸"的材料（实测出现过，还把别的页并了进去）。
LETTERHEAD = ("稿纸", "公用笺", "简纸", "信纸", "便笺", "笺")


def is_letterhead(t) -> bool:
    """这个"标题"是不是信纸抬头（不能当材料名用）。"""
    s = str(t or "")
    return bool(s) and any(w in s for w in LETTERHEAD)


def _book_by_column(value, index: dict, books: dict):
    """把"其实填的是栏目名"的 form 值认回册子。

    实测：模型有时把栏目名填进 form（"主要简历""本人经历"），而那是**册子里的栏目**，
    不是册名 —— 光按册名查表就会给这些页另起一个桶，好好的册子被拆开。
    这里补一条：这个值若能唯一对到某本册子的栏目，就按那本册子算。
    （对到多本 → 不认，交回给上层走"歧义"分支，别猜。）
    """
    if not value or _norm(value) in index:
        return None                       # 空 / 已经是册名或别名 → 走原路径
    hits = _pk_candidates(value, books)
    return hits[0] if len(hits) == 1 else None


def _pk_candidates(pk, books: dict) -> list[str]:
    """栏目名 → 它可能是哪几本册子的栏目（**一格一格**对照各册的"版序"表）。

    返回册名列表：长度 1 = 唯一确定（L1）；长度 ≥2 = 两本册子都有这个栏目（歧义，L2）。
    """
    n = _pk_norm(pk)
    if not n:
        return []
    return [name for name, b in books.items()
            if any(_key_match(n, _pk_norm(k)) for k in (b.get("order") or {}))]


def _rank(pk, book: str, books: dict):
    """栏目在册内的版序号（越小越靠前）；查不到给 None。用于排册内页序。"""
    n = _pk_norm(pk)
    if not n:
        return None
    hit = [v for k, v in (books.get(book, {}).get("order") or {}).items()
           if _key_match(n, _pk_norm(k))]
    return min(hit) if hit else None        # 命中多条时取最靠前的（保守）


# ── 桶内页序 ───────────────────────────────────────────────────
def _order_pages(seqs: list[int], books: dict, form, meta: dict) -> list[int]:
    """把一份材料内部的页排成"装订/阅读"顺序（照片顺序是随机的，不能直接用）。

    三档降级（有把握的优先）：
      ① 页面上印了页码、**这一份里每一页都有且互不重复** → 按印刷页码排
      ② 没有页码，但栏目名都在册子的"版序"表里 → 按版序排
      ③ 都不行 → 按版序（能查到的那几页）+ 照片号兜底

    ★ 为什么①要求这么苛刻（用户 2026-09-12 特别提醒）：**一卷里不同表册各编各的页码**
      —— 甲表册的第 3 页和乙表册的第 3 页都印着"3"，谁也不是全局坐标。
      所以印刷页码只能在"一份材料内部"当顺序用：必须整份都带号、且号不重复，
      才说明这一份用的是同一套编号。只要有一页缺号或有重号（说明混进了另一套编号），
      就一律不信它，退回按册子的栏目版序排 —— 版序是我们自己维护的、可控。
      命令目录用的 `pg` 也没有参与：材料之间的排序走 类号 → 日期 → 照片号（见 dir_sort_key）。
    """
    pgs = {s: meta[s]["pg"] for s in seqs}
    with_pg = [s for s in seqs if pgs[s] is not None]
    if len(with_pg) == len(seqs) and len(set(pgs[s] for s in with_pg)) == len(seqs):
        # ① 全员带号且互不重复 → 才敢按印刷页码排
        return sorted(seqs, key=lambda s: (pgs[s], s))
    # ②③ 版序（查不到给一个大数，排在已知栏目之后）
    BIG = 10 ** 6
    return sorted(seqs, key=lambda s: (
        _rank(meta[s]["pk"], form, books) if (form and _rank(meta[s]["pk"], form, books) is not None) else BIG,
        s))


def _pick_rep(seqs: list[int], books: dict, form, meta: dict) -> int:
    """选"代表页"——材料名与日期从它身上取，定类时也给模型看它。

    以前是"取 seq 最小的页当首页"，照片顺序随机后这等于随便抓一页（可能抓到栏目页、
    结果材料名就成了"工作经历"）。现在的偏好顺序：
      ① 页面的标题 == 册名（封面页最理想）
      ② 册内版序靠前的（封面 → 说明 → 基本情况）
      ③ 照片号小的（纯兜底，保证结果确定）
    """
    BIG = 10 ** 6
    canon = _norm(books.get(form, {}).get("title") if form else "")
    form_n = _norm(form)

    def score(s):
        t = _norm(meta[s]["t"])
        # ① 标题就是册名（或册名的别名之一）→ 最优先
        hit0 = 0 if (canon and (t == canon or form_n and t == form_n)) else 1
        r = _rank(meta[s]["pk"], form, books) if form else None
        return (hit0, r if r is not None else BIG, s)

    return min(seqs, key=score)


# ── 一式 N 份（复本）────────────────────────────────────────────
def _sig(pages: dict, seqs) -> dict:
    """给一组页算"签名"（指纹），用来判断两组是不是同一份的重复。

    签名 = 页数 + 代表页标题 + 代表页日期 + 每页正文要点（归一化后）。
    ★ 刻意**不含照片号/md5**：同一份材料拍两次（不同照片）也该认出来（rules §4）。
    """
    pp = [pages[s] for s in seqs]
    return {"n": len(seqs), "title": _norm(_t(pp[0])), "date": _date(pp[0]),
            "texts": [x for x in (_norm(_mark(r).get("s")) for r in pp) if x]}


def _same(a, b, thresh: float = 0.9) -> bool:
    """两个签名"是不是同一种东西"（→ 一式 N 份）。用相似度判断而非死等。

    difflib.SequenceMatcher.ratio() ≈ 文本相似度 0~1。
    """
    if a["n"] != b["n"]:                 # 页数都不同 → 不是同一份
        return False
    if a["title"] or b["title"]:         # 有标题就要求标题一致
        if a["title"] != b["title"]:
            return False
    if a["date"] or b["date"]:           # 有日期就要求日期一致
        if a["date"] != b["date"]:
            return False
    if not a["texts"] and not b["texts"]:   # 都没正文要点 → 只凭上面判断
        return True
    if not a["texts"] or not b["texts"]:    # 一个有正文一个没有 → 存疑，不算重复
        return False
    ok = [difflib.SequenceMatcher(None, x, y).ratio() >= thresh
          for x, y in zip(a["texts"], b["texts"])]
    return sum(ok) >= max(1, int(len(ok) * 0.9))


def _split_copies(seqs: list[int], pages: dict, books: dict, form, meta: dict):
    """看这个桶里是不是装着"同一份拍了好几遍"（一式 N 份），是就拆成 N 份。

    为什么需要它：同一个日期、同一本册子拍两遍 → 身份键相同 → 会被并进一个桶，
    页数就翻倍了。判法：按"每页的(栏目,要点,日期)指纹"分簇，**每簇页数都一样**才算
    复本（说明每个位置都恰好出现 N 次）；一旦各簇大小不齐就说明只是碰巧有重复页，
    保守起来不拆。

    返回 (本体页, [其它复本页组…])；不是复本时返回 (全部页, [])。
    """
    if len(seqs) < 2:
        return list(seqs), []
    ordered_all = _order_pages(list(seqs), books, form, meta)   # 先按装订序排好
    clusters: dict[tuple, list[int]] = {}
    for s in seqs:
        m = _mark(pages[s])
        key = (_norm(m.get("pk") or m.get("t")), _norm(m.get("s")), _date(pages[s]))
        clusters.setdefault(key, []).append(s)
    k = len(clusters)
    sizes = {len(v) for v in clusters.values()}
    n = len(next(iter(clusters.values()))) if k else 0
    if k < 2 or len(sizes) != 1 or n < 2:
        return ordered_all, []           # 各簇大小不齐 / 只有一簇 → 不当复本，整桶一份
    ordered = sorted(clusters.values(), key=lambda v: min(v))
    first = [min(v) for v in ordered]                       # 每个位置取第一份 = 本体
    dups = [[sorted(v)[i] for v in ordered] for i in range(1, n)]
    return _order_pages(first, books, form, meta), dups


# ── 回退路径：老的"按顺序切份"（只在闸门触发时用）──────────────────
def _same_title(a, b) -> bool:
    """两个标题"算不算同一份"（老规则，仅在回退路径里用）。"""
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return True
    if not a or not b:
        return False
    return a == b or (len(a) > 1 and len(b) > 1 and (a in b or b in a))


def _split(prev: dict, p: dict) -> bool:
    """老规则：第 p 页要不要"另起一份"（看相邻两页像不像）。"""
    m = _mark(p)
    if m.get("is_first") is True:
        return True
    a, b = _doc(p), _doc(prev)
    if a and b and a != b:
        return True
    ta, tb = _t(p), _t(prev)
    if ta and tb and not _same_title(ta, tb):
        return True
    if ta is None and m.get("is_cont") is not True and a is None and tb is None:
        return True
    return False


def _legacy_order_split(pages: list[dict]) -> list[list[int]]:
    """老做法：顺照片顺序切份（假设"相邻两页像不像同一份"）。

    只当"form 覆盖率过低"时兜底 —— 退回旧行为总好过让整卷塌成 126 条。
    """
    segs: list[list[int]] = []
    cur: list[int] = []
    for p in pages:
        if not cur:
            cur = [p["seq"]]
        elif _split(_by(pages, cur[-1]), p):        # ★用 _by 按 seq 找页，不能拿 seq 当下标
            segs.append(cur)
            cur = [p["seq"]]
        else:
            cur.append(p["seq"])
    if cur:
        segs.append(cur)
    return segs


def _legacy_build(pages: list[dict]) -> list[dict]:
    """把老路径的切份结果装配成新契约的 cands（字段补齐，好让下游无感）。"""
    raw = _legacy_order_split(pages)
    out = []
    for i, s1 in enumerate(raw, 1):
        head = _by(pages, s1[0])              # 老规则里"第一页"就是首页，代表页取它
        d = _date(head)
        out.append({"idx": i, "seqs": list(s1), "dup_groups": [], "copies": 1,
                    "pages": len(s1), "form": None,
                    "date": {"y": d[0], "m": d[1], "d": d[2]} if d else None,
                    "rep": s1[0], "raw_seqs": sorted(s1), "doubt": False,
                    "orphan": False})
    return out


def reorder(seqs: list[int], records: list[dict], form=None) -> list[int]:
    """给一组页重算装订序（供 graph.node_resolve 并页后、交互层调序时复用）。

    seg 内部排页序的规则只在 _order_pages 一处，这里开个小口子复用，
    免得别处各写一份排序（改口径时漏改）。
    """
    pages = sorted(records, key=lambda r: r["seq"])
    meta = {r["seq"]: {"t": _t(r), "pk": _pk(r), "pg": _pg(r), "date": _date(r)}
            for r in pages}
    return _order_pages(list(seqs), loader.formbooks(), form, meta)


def _by(pages, seq):
    """按 seq 从 pages 里找出那一页；找不到给第一页（防御）。"""
    return next((p for p in pages if p["seq"] == seq), pages[0])


# ── 主入口 ─────────────────────────────────────────────────────
def build_candidates(records: list[dict], mode: str = "identity") -> list[dict]:
    """主入口：把整卷记录分成"份候选"，每份附一式 N 份信息。

    返回 [{idx, seqs, dup_groups, copies, pages, form, date, rep, raw_seqs, doubt}, …]。
    之后 classify 每份取"代表页"去定类。
    """
    global LAST_NOTE
    LAST_NOTE = ""
    pages = sorted(records, key=lambda r: r["seq"])   # 按照片号排（只是稳定遍历用）
    books = loader.formbooks() if mode == "identity" else {}

    if not books:                                     # 对照表读不出来 → 没法判身份
        LAST_NOTE = "表册对照读不出，已退回按照片顺序切份"
        return _legacy_build(pages)

    index = _book_index(books)
    by_seq = {r["seq"]: r for r in pages}
    meta: dict[int, dict] = {}
    for r in pages:
        meta[r["seq"]] = {"t": _t(r), "pk": _pk(r), "pg": _pg(r), "date": _date(r)}

    # ① 逐页归一 form：册名/别名 → 规范册名；认不出的自由表单名 → 归一化后原样用
    raw_forms: dict[int, tuple] = {}                  # seq -> (规范名|None, 是不是册子)
    covered = 0
    for r in pages:
        f = _form(r)
        canon = index.get(_norm(f)) if f else None
        if canon:
            raw_forms[r["seq"]] = (canon, bool(books[canon].get("whole")))
            covered += 1
        elif f:
            via_col = _book_by_column(f, index, books)     # 填的其实是册内栏目名
            if via_col:
                raw_forms[r["seq"]] = (via_col, bool(books[via_col].get("whole")))
            else:
                raw_forms[r["seq"]] = (f, False)           # 真·自由表单名（不在对照表里）
            covered += 1
        else:
            raw_forms[r["seq"]] = (None, False)
            # 没给 form，但**栏目名对得上某本册子**也算"有线索" —— 包括"两本册子都有
            # 这个栏目"的歧义情况（那种页走 L3.5 同类册桶，仍是有效信号）。
            # 只有"既没册名、栏目也不认识"才是真残页。否则整卷只有栏目、没册名的卷子
            # 会被误判成"读不出"而白白回退到旧逻辑。
            if _pk_candidates(_pk(r), books):
                covered += 1

    # ② 覆盖率闸门：模型整体没读出 form → 别硬上身份归组
    if covered < len(pages) * _MIN_COVERAGE:
        LAST_NOTE = (f"form 覆盖率过低（{covered}/{len(pages)}），"
                     f"已退回按照片顺序切份 —— 请检查建档提示词是否生效")
        return _legacy_build(pages)

    # ③ 分桶（与照片号完全无关）
    buckets: dict[tuple, list[int]] = {}
    doubt_keys: set[tuple] = set()
    for r in pages:
        seq = r["seq"]
        form, is_book = raw_forms[seq]
        pk = meta[seq]["pk"]
        cands = _pk_candidates(pk, books) if pk else []
        if form is None:
            if len(cands) == 1:                       # L1 栏目唯一 → 册
                key = ("book", cands[0])
            elif cands and len({books[c]["cat"] for c in cands}) == 1:
                key = ("cat", books[cands[0]]["cat"])  # L3.5 歧义但同类 → 并成一个"同类册桶"
                doubt_keys.add(key)
            else:                                     # L4 毫无线索 → 各自独立，交给 resolve
                key = ("orphan", seq)
        elif is_book:                                 # 册子：忽略日期，整本一份
            # L2 交叉校验：栏目能定位到别的册子集合里、却不含这本 → 说明 form 或栏目
            # 有一个读错了。以"栏目"为准（栏目是照抄的原文，比册名推断可靠），标存疑。
            if cands and form not in cands:
                if len({books[c]["cat"] for c in cands}) == 1:
                    key = ("cat", books[cands[0]]["cat"])
                    doubt_keys.add(key)
                else:
                    key = ("orphan", seq)
            else:
                key = ("book", form)
        else:                                         # 单页件：靠日期区分"同表不同份"
            d = meta[seq]["date"]
            key = ("form", form, d if d else _NO_DATE + str(seq))
        buckets.setdefault(key, []).append(seq)

    # ④ 装配成分候选：成员排序 + 代表页 + 复本拆分
    outs: list[dict] = []
    for key, seqs in buckets.items():
        kind = key[0]
        form = key[1] if kind == "book" else (key[1] if kind == "form" else None)
        ordered, dups = _split_copies(seqs, by_seq, books, form, meta)
        rep = _pick_rep(ordered, books, form, meta) if ordered else seqs[0]
        d = meta[rep]["date"]
        outs.append({
            "idx": 0, "seqs": ordered, "dup_groups": dups,
            "copies": 1 + len(dups), "pages": len(ordered),
            "form": form,
            "date": {"y": d[0], "m": d[1], "d": d[2]} if d else None,
            "rep": rep, "raw_seqs": sorted(seqs),
            "doubt": key in doubt_keys,
            "orphan": kind == "orphan",
        })

    # ⑤ 跨桶的一式 N 份：全卷比签名（老代码只看后面 20 份 —— 顺序随机后复本会散到
    #    相隔很远的地方，窗口就是漏检的来源；去掉窗口，桶数只有几十，两两比也很便宜）
    used = [False] * len(outs)
    merged: list[dict] = []
    for i, a in enumerate(outs):
        if used[i]:
            continue
        used[i] = True
        sig_a = _sig(by_seq, a["seqs"])
        dups = list(a["dup_groups"])
        for j in range(i + 1, len(outs)):
            if used[j]:
                continue
            b = outs[j]
            if b["form"] != a["form"] or _sig(by_seq, b["seqs"])["n"] != sig_a["n"]:
                continue
            if _same(sig_a, _sig(by_seq, b["seqs"])):
                used[j] = True
                dups.append(list(b["seqs"]))
        merged.append({**a, "dup_groups": dups, "copies": 1 + len(dups)})

    # ⑥ 按目录序排好并重新编号：idx 从 1 起，下游（分类/导出/全景卡）都按这个顺序走
    merged.sort(key=classes.dir_sort_key)
    for i, c in enumerate(merged, 1):
        c["idx"] = i
    return merged
