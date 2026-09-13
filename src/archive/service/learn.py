"""口径学习 —— 把「用户改了什么」算成一条"改哪份文件、怎么改、改完长什么样"的提案。

新手视角（Java 朋友版）：这个模块 ≈ 一个 **Service 层**，负责"算"和"落盘"两件事：
    ① 修正账本（用户改了啥）  → `db.corrections_*`（已有）
    ② 口径文件（真提示词）    → `kouju.py`（解析/追加/摘词/写盘）
    ③ 落盘                  → `apply()` → `kouju.write_text()`（全仓唯一写口）

**一句话流程**：用户改一次类 → `from_correction()` 算出一条提案 → 卡片长在对话流里
→ 用户确认 → 模型调 **`archive.apply_learning`**（`risk="approve"`，宿主的批准闸拦住）
→ `apply()` 现算现写。

**★ 模型不参与生成文件内容**（09-13 定稿）：工具只收"哪一条"（文种 + 类别），
条文由 `kouju` 现算、写到固定的那份文件。所以模型既**不可能写歪一行**，也**没有路径
参数**可以写别处 —— 这是这类"让 AI 改提示词"功能最容易出的两种错，直接从形状上消掉。

**★ 为什么不用 nonce/令牌那套**：写这条路要过 `apply_learning` 的**批准闸**
（`risk="approve"`，跟 `export` 同一个机制）—— **用户不点"允许"就写不动**。
"必须用户确认"由宿主统一的闸保证，不必再造一个。

**★ 也不需要任何新表**：状态全在已有数据里 —— "用户改过什么"在账本（corrections）里，
"学没学过"看口径文件的「用户已确认」节。所以这个模块是**无状态**的：
同一条修正算几次，结果都一样（除非文件变了）。
"""
from __future__ import annotations

import os
import re
import time

from archive.domain import classes      # 类别码归一（写进口径前必须过它）
from archive.service import kouju

# 条文里书名号内的文种名（`- 《任免审批表》→ **十**…`）
_TITLE_RE = re.compile(r"《([^》]+)》")

# 「你在对话里」/「你改后的终版目录里」—— 提案卡片上那句"为什么会有这条"
_SUB_PREFIX = {"ledger": "你在对话里", "excel": "你改后的终版目录里"}


_TITLE_NOISE = re.compile(r"[\r\n《》*]+")


def stamp() -> str:
    """今天的日期（写进口径条文的出处戳）。"""
    return time.strftime("%Y-%m-%d")


def _clean(s) -> str:
    """清掉文种名里的换行/书名号/星号，再去首尾空白。

    ★ 这几个字符会**破坏条文行的格式**：换行能往共享的口径文件里注入新行、
      甚至插一个 `## 基础口径` 标题出来（而写前只检查三个标题在不在，照样通过）；
      `《》` 和 `*` 会把 `- 《X》→ **类**` 这条格式撑坏。文种名本来也不该有它们。
    """
    return _TITLE_NOISE.sub("", str(s or "")).strip()


def _entry_title(entry: str) -> str:
    """从一条已确认条文里抠出文种名（归一后）。"""
    m = _TITLE_RE.search(entry or "")
    return kouju.norm(m.group(1)) if m else ""


def already_confirmed(title: str, category: str, text: str | None = None) -> bool:
    """这条口径是不是**已经在「用户已确认」节里**了（= 学过了，不用再提）。

    这是本模块唯一的"去重"依据，而它读的是**文件本身**而不是某个库 ——
    所以用户手工把一条口径写进文件后，系统也不会再问一遍。
    """
    text = kouju.read_text() if text is None else text
    want = kouju.norm(title)
    for e in kouju.confirmed_entries(text):
        if _entry_title(e) == want and kouju.cat_of(e) == category:
            return True
    return False


def propose(changes: list[dict], text: str | None = None) -> dict:
    """把一串改动算成一份提案：**一份完整的新内容** + 每条改动的人话说明。

    `changes` = `[{"title", "category", "old", "count"?}]`，**按顺序**作用到文件上
    （第二条是在第一条改完之后的基础上再改 —— 所以那份 `content` 是"全做完"的样子，
    模型一次 `station.write` 写下去即可，不必自己一条条拼）。

    返回 `{"file": 仓库相对路径, "items": [...], "content": 改完的完整文本}`。
    ★ `content` 是给 `apply()` **写盘**用的；`card()` 会把它摘掉 —— 前端不需要整份文件，
      模型也不需要（它只传"哪一条"，条文由这里现算）。
    """
    text = kouju.read_text() if text is None else text
    items: list[dict] = []
    cur = text
    for ch in changes:
        title = _clean(ch.get("title"))
        # ★ 类别码必须**归一**再写。模型完全可能给 `9-2` 这种写法，而 `cat_of()` 只认
        #   汉字类号 —— 写进去以后永远读不回来，`already_confirmed` 恒为假，同一条提案
        #   会反复弹、每次确认再追加一条重复行（review 抓到的真 bug）。
        #   归不了就整条拒掉（`t_set_category` 也是这么做的，两边口径要一致）。
        cat = classes.normalize_category(ch.get("category"))
        if not title or not cat:
            raise ValueError(
                f"类别码不合法：{ch.get('category')!r}（十类，四/九带小类如 四-1、九-2）")
        # ★ 已经学过的**不再写**：这一步原来只在账本那条路上做，显式传 title+category
        #   的那条路漏了 —— 于是重复确认会往「用户已确认」节里堆重复行（review 抓到的
        #   另一个口子）。挪到这里，三条入口就都盖住了。
        if already_confirmed(title, cat, cur):
            continue
        conflict = kouju.find_conflict(cur, title, cat)
        count = int(ch.get("count") or 1)
        entry = kouju.render_entry(title, cat, stamp(), count)
        cur, dropped = kouju.apply_amendment(cur, entry, conflict)
        who = _SUB_PREFIX.get(ch.get("source") or "ledger", "你")
        old = _clean(ch.get("old"))
        items.append({"title": title, "category": cat, "entry": entry,
                      "why": f"{who}把《{title}》从 {old or '（未判）'} 改成了 {cat}",
                      "from": [old] if old else [], "count": count,
                      "drop": conflict, "dropped": dropped})
    return {"file": kouju.REL_PATH, "items": items, "content": cur}


def card(prop: dict, note: str = "") -> dict:
    """把提案包成前端 `learn-card` 认识的那一张卡。

    ★ 刻意**不**带 `content` —— 整份文件是给模型递的，前端只要标题/说明/要摘哪个词。
    """
    if not prop:
        return {"type": "learn-card", "items": [], "note": note}
    return {"type": "learn-card", "items": prop["items"], "note": note,
            "file": prop["file"]}


def from_correction(before: dict, after: dict, source: str = "ledger") -> dict | None:
    """一次归类修正 → 一条提案；**没什么可学的就返回 None**（静默）。

    "返回 None" 是这个功能**不吵人**的关键：同一文种、同一结论已经学进去了就不再问，
    没改出所以然（改成了同一个类）也不问 —— 这些都静默返回 None，调用方一张卡都不出。
    """
    title = (before.get("title") or "").strip()
    new_cat = (after.get("category") or "").strip()
    old_cat = (before.get("category") or "").strip()
    if not title or not new_cat or old_cat == new_cat:
        return None                              # 没名字 / 没类别 / 根本没改 → 无事可做
    text = kouju.read_text()
    if already_confirmed(title, new_cat, text):
        return None                              # 学过了 → 闭嘴
    return propose([{"title": title, "category": new_cat, "old": old_cat,
                     "source": source}], text)


def pending_changes(project: str) -> list[dict]:
    """从这卷的修正账本里挑出**还没学进口径**的那些改动（`changes` 列表）。

    ★ 不需要任何新表 —— 账本本来就是"用户改过什么"的唯一记录，"学没学过"看
      口径文件的「用户已确认」节。同一文种+同一结论只算一条（重复改的记录会合并）。
    """
    from archive.service import interactive as it
    text = kouju.read_text()
    # ★ 去重键是**文种**，不是「文种+类别」。按 (文种, 类别) 去重会让**被推翻的早先结论
    #   存活下来：用户把考核表 三→九-1 又反悔改成 九-1→九-2（中间没确认过），两条记录的
    #   键不同，于是「九-1」和「九-2」会**一起**写进那个自称"冲突以本节为准"的节里
    #   —— 本该裁决冲突的地方自己先自相矛盾（review 抓到）。账本是旧→新，**后写的赢**。
    latest: dict = {}
    for c in it.read_corrections(project):
        if c.get("kind") != "set_category":
            continue
        before, after = c.get("before") or {}, c.get("after") or {}
        title = _clean(before.get("title"))
        cat = classes.normalize_category(after.get("category"))
        old = _clean(before.get("category"))
        if not title or not cat or old == cat:
            continue
        latest[kouju.norm(title)] = {"title": title, "category": cat, "old": old,
                                     "source": c.get("src") or "ledger"}
    return [v for v in latest.values()
            if not already_confirmed(v["title"], v["category"], text)]


def pending_from_ledger(project: str) -> dict:
    """把 `pending_changes` 算成一条**提案**（给卡片展示用）；没有待学项返回 `{}`。"""
    changes = pending_changes(project)
    return propose(changes) if changes else {}


def apply(changes: list[dict]) -> dict:
    """把改动**真写进**口径文件。★ 全仓唯一会调 `kouju.write_text` 的地方。

    **只该被 archive 的 `apply_learning` 工具调用**，而那个工具是 `risk="approve"`
    —— 用户不点"允许"，走不到这里。这台闸就是"必须用户确认"的全部实现，别绕过它。

    写之前先过 `kouju.looks_valid`：算出来的东西结构不对（缺节）就**拒写**。
    这份文件是全站共享的，写坏了 `loader.hard()` 读不出内容 → **每一卷的分类都跟着坏**，
    所以宁可报错让模型重来。
    """
    prop = propose(changes)
    if not prop["items"]:
        return {"written": [], "dropped": []}
    if not kouju.looks_valid(prop["content"]):
        raise ValueError("算出来的内容结构不对（三节不齐），拒绝写入 —— "
                         "口径文件是全站共享的，写坏会影响每一卷的分类。")
    _backup()
    kouju.write_text(prop["content"])
    # ★ 报的是**结果**（`dropped`，真的摘掉没有），不是**计划**（`drop`，本来打算摘谁）。
    #   两者会不一致：文种找不到、或它在括号组里摘不掉时，改完的文件一个字节没变，
    #   却按计划回报"已摘掉" —— 用户以为旧规则没了，下一卷照样按旧的判（review 抓到）。
    return {"written": [i["entry"] for i in prop["items"]],
            "dropped": [i["drop"]["token"] for i in prop["items"] if i.get("dropped")]}


def _backup() -> None:
    """写前把口径文件的旧内容存一份进文件区（内容寻址，同内容不会存两份）。

    ★ 为什么必须有：这份文件是**全站共享**的，一次判错的改动会让每一卷的分类都跟着错
      —— 而 `looks_valid` 只检查三个节标题在不在，结构对了内容错了一样放行。
      原来管这件事的是全局写工具（覆盖前自动备份），它 09-13 删了，这里把这条不变量
      重新建立在**唯一的写口**上。
    ★ 备份失败不拦写入（改都批准过了）—— 但**不静默**：留一句说明，出问题时查得到。
    """
    try:
        from station.files import store as fs
        old = kouju.read_text()
        if old.strip():
            fs.save_bytes(old.encode("utf-8"), ".md", name="文种对照-改写前.md",
                          skill_id="archive", group_key="口径学习",
                          group_label="口径文件改写前的备份")
    except Exception as e:                        # noqa：备份是兜底，不是前提
        print(f"[archive] 口径备份失败（不影响本次写入）：{type(e).__name__}: {e}")


# ── 入口 B：用户在 Excel 里改完再传回来 ──────────────────────────────
# 新手视角：这一段 ≈ 一个"两版报表比对"的工具。要点全在**怎么把同一份材料认出来** ——
# Excel 里不存我们的 uid，所以只能用"实物锚点"来认：这份材料是**哪几张照片**。
# 照片号跨"AI 现场 → xlsx → 用户改过的 xlsx"这条链路不会丢（用户改类别不会去动页号），
# 所以它是第一匹配键；认不出时才退到"按名字认"。

_SKEL_RE = re.compile(r"^[一二三四五六七八九十](-\d)?$")     # 骨架行的 A 列（一 / 四-1 …）


def read_xlsx_materials(path: str) -> list[dict]:
    """把一份目录 xlsx 读回成材料行（用户改过的、或刚导出的都能读）。

    返回 `[{"cat", "title", "seqs", "row"}]`，**按文件里的先后顺序**。
      · `cat`   ：它落在哪一类（主表**没有类别列**，靠"上一个骨架行"推出来）
      · `seqs`  ：源照片号集合（来自隐藏的 `_trace` 表；没有/对不上就是 None）
    怎么认出"这是材料行"：主表里**骨架行的 A 列是类号字符串**（一/四-1…），
    而**材料行的 A 列是类内序号（int）**且 B 列有名字 —— 两者不会混。
    """
    from openpyxl import load_workbook              # 函数内 import：用到才加载
    from archive.domain import classes
    wb = load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows: list[dict] = []
    cur = ""
    for r in range(1, ws.max_row + 1):
        a = ws.cell(row=r, column=1).value
        b = ws.cell(row=r, column=2).value
        if isinstance(a, str) and _SKEL_RE.match(a.strip()):
            cur = a.strip()                          # 记下"现在走到哪一类了"
            continue
        if isinstance(a, int) and b is not None and str(b).strip():
            rows.append({"cat": classes.normalize_category(cur) or cur,
                         "title": str(b).strip(), "seqs": None, "row": r})
    tr = wb["_trace"] if "_trace" in wb.sheetnames else None
    if tr is not None:
        vals = [list(v) for v in tr.iter_rows(min_row=2, values_only=True)]
        # ★ 只有"行数对得上"才敢按行序配对：用户可能插过/删过行，那样行序就不可信了
        if len(vals) == len(rows):
            for it, v in zip(rows, vals):
                s = v[3] if len(v) > 3 else None
                if s:
                    got = {int(x) for x in str(s).split(",") if x.strip().lstrip("-").isdigit()}
                    it["seqs"] = got or None
    return rows


def diff_materials(ai_mats: list[dict], user_rows: list[dict]) -> dict:
    """比对"AI 现场"与"用户改后的目录"，返回差异清单。

    匹配分两轮（**从强到弱**，认不出就如实报成多/少，不硬凑）：
      ① 按**源照片号集合**（最稳：改类别不会动页号）；
      ② 剩下的按**材料名**（同名唯一的才认 —— 重名的宁可认不出）。
    返回 `{"cat_changed":[{title,from,to}], "renamed":[{from,to}],
           "missing":[{title,cat}], "added":[{title,cat}]}`
    """
    from archive.domain import classes
    out: dict = {"cat_changed": [], "renamed": [], "missing": [], "added": []}

    def ai_cat(m):
        return classes.normalize_category(m.get("category")) or (m.get("category") or "")

    taken: dict[int, dict] = {}                # user 行下标 → 配上的 AI 行

    # ① 按源照片号（最稳：改类别不会去动页号）
    by_seqs: dict = {}
    for m in ai_mats:
        s = frozenset(int(x) for x in (m.get("members") or []))
        if s:
            by_seqs.setdefault(s, []).append(m)
    for i, row in enumerate(user_rows):
        if not row["seqs"]:
            continue
        cand = by_seqs.get(frozenset(row["seqs"]))
        if cand:
            taken[i] = cand.pop(0)             # 同页号有多行时按顺序取，取完不再配

    used = {id(m) for m in taken.values()}     # 已被配走的 AI 行

    # ② 按材料名（**两边都唯一**才认；重名的宁可不认，免得张冠李戴）
    def _uniq(pairs):
        d: dict = {}
        for idx, title in pairs:
            k = kouju.norm(title)
            if k:
                d.setdefault(k, []).append(idx)
        return {k: v[0] for k, v in d.items() if len(v) == 1}

    ai_by_title = _uniq([(i, m.get("title") or "")
                         for i, m in enumerate(ai_mats) if id(m) not in used])
    for i, row in enumerate(user_rows):
        if i in taken:
            continue
        j = ai_by_title.get(kouju.norm(row["title"]))
        # ★ `ai_by_title` 建好就不再删，所以**必须**再查一次 `used` —— 否则用户那版里
        #   两份同名的材料会都配给同一行 AI 材料，多出来的那份既不进 `added` 也不进
        #   `missing`，从差异里凭空消失，`diff_note` 还会说"完全一致"（review 抓到）。
        if j is not None and id(ai_mats[j]) not in used:
            taken[i] = ai_mats[j]
            used.add(id(ai_mats[j]))

    # ③ 结算
    for i, row in enumerate(user_rows):
        m = taken.get(i)
        if m is None:
            out["added"].append({"title": row["title"], "cat": row["cat"]})
            continue
        if ai_cat(m) != row["cat"]:
            out["cat_changed"].append({"title": m.get("title") or row["title"],
                                       "from": ai_cat(m), "to": row["cat"]})
        if kouju.norm(m.get("title") or "") != kouju.norm(row["title"]):
            out["renamed"].append({"from": m.get("title") or "", "to": row["title"]})
    for m in ai_mats:
        if id(m) not in used:
            out["missing"].append({"title": m.get("title") or "", "cat": ai_cat(m)})
    return out


def diff_note(d: dict) -> str:
    """把差异清单说成一句人话（给卡片上的说明用）。"""
    bits = []
    if d["cat_changed"]:
        bits.append(f"{len(d['cat_changed'])} 处类别不同")
    if d["renamed"]:
        bits.append(f"{len(d['renamed'])} 处名称不同")
    if d["missing"]:
        bits.append(f"你的版本少了 {len(d['missing'])} 份")
    if d["added"]:
        bits.append(f"你的版本多了 {len(d['added'])} 份")
    if not bits:
        return "和系统生成的完全一致，没有发现差异。"
    return "对照结果：" + "、".join(bits) + "。" + \
           ("名称与多/少的差异只在卡片里列出；**只有类别差异**会被提炼成口径提案。")


def _safe_upload_path(path: str) -> str:
    """只认「上传终版目录」那个目录里的文件 —— 模型给的 `path` 不能指到别处去。

    ★ 服务器上的文件读取原来有一道路径闸（`tools/guard.py`），它随文件工具在 09-13 删了；
      而这个工具的参数是**模型给的自由文本**。不收紧就等于：一句话
      `reconcile(path='…/随便哪个.xlsx')` 就能把服务器上任意 xlsx 的内容读进对话
      （review 抓到的口子）。收在一个目录里，够用且够紧。
    """
    from station import config
    root = os.path.realpath(str(config.sub("reconcile")))
    p = os.path.realpath(os.path.abspath(str(path or "")))
    # ★ 比前缀一定要带分隔符：`/a/bc` 不该被当成 `/a/b` 的子路径
    #   （`guard.py` 的注释里专门记过这个坑：字符串前缀比是路径穿越最常见的误判）。
    if not p.startswith(root + os.sep):
        raise ValueError("只能对账**上传上来的**终版目录 —— 让用户用上传卡片传一份。")
    return p


def reconcile(project: str, xlsx_path: str, user_id: str) -> dict:
    """用户改过的终版目录 vs AI 现场 → 差异清单 + 待确认的口径提案。

    ★ 类别差异**复用 `set_category` 记账**（不新造一种 kind）：这样"对话里改的"和
      "终版 Excel 里改的"汇进同一张账本，来源用 `src` 字段区分（ledger / excel）。
    ★ 名称/日期/份数差异**不进提案** —— 《文种对照》管的是"什么文种归哪类"，
      不管材料名怎么写；那些差异只在卡片里如实列出。
    """
    from archive.service import interactive as it
    st = it.load_state(project)
    if st is None:
        raise ValueError("这卷还没有识别现场，先识别完再来对账。")
    xlsx_path = _safe_upload_path(xlsx_path)
    try:
        rows = read_xlsx_materials(xlsx_path)
    except FileNotFoundError:
        # 上传的文件可能已经被清掉了（比如用户又传了一次照片、或重传了目录）
        # —— 给一句人话，别让 openpyxl 的 FileNotFoundError 冒到用户面前
        # （它**不是** ValueError，`t_reconcile` 那个 except 接不住）。
        raise ValueError("找不到那份目录文件了（可能被后续上传覆盖或清理掉了）——"
                         "让用户重新上传一次改后的目录。")
    except Exception as e:                        # noqa：坏文件/非 xlsx 都归成一句人话
        raise ValueError(f"这份表读不了（{type(e).__name__}）——"
                         "确认它是《人事档案目录》那份 .xlsx、且没有损坏。")
    if not rows:
        raise ValueError("这份表里没读到材料行（是不是选错文件了？）。")
    d = diff_materials(st.get("materials") or [], rows)

    text = kouju.read_text()
    changes = [{"title": ch["title"], "category": ch["to"], "old": ch["from"],
                "source": "excel"} for ch in d["cat_changed"]]
    for ch in d["cat_changed"]:
        it.record_correction(project, "set_category",
                             {"title": ch["title"], "category": ch["from"]},
                             {"title": ch["title"], "category": ch["to"]},
                             user_id, extra={"src": "excel"})
    # 去重/去已学都在 `propose` 里做（三条入口共用一处，别再各写一份）
    return {"diffs": d, "proposal": propose(changes, text) if changes else {},
            "note": diff_note(d)}
