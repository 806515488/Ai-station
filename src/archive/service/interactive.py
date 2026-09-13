"""service.interactive —— 分拣台的“交互操作层”：改类/合并/拆分/换OCR，全部落盘可重放。

新手视角（Java 朋友版）：这是审核台背后的“用例层”（Use Case）。前端每个按钮
（改类别、并份、拆份、换一家OCR）都对应这里的一个函数；每个函数做三件事：
  ① 改数据（materials/photos.json 按规则改那一小块）
  ② 记账（往 DB 的 corrections 表记一条“AI 原判 → 用户改成什么”，口径学习的原料）
  ③ 返回更新后的“现场”（materials/issues），前端整体刷新
为什么不用数据库：单用户 + 数据量小（一份材料几十行）→ JSON 文件 + 全量重写
最简单可靠；photos.json 本来就是这么存的（见 storage/project.py）。

“现场”存在哪：SQLite 的 archive_states 表（一份卷一行、最新态；键 = project.json
的绝对路径）。**建卷时先登记一条空现场**（register_volume，让页图鉴权提前生效），
识别 job 跑完时（station_adapter）把识别结果写进去；之后的每次交互操作都改这份 →
出件时导出的就是修正后的终版。（早期版本存 <项目>/review/materials.json，现在只作
升级前的读取兜底，见 load_state。）

脏传播（改一处，什么要跟着变）：
  set_category / rename / set_date → 只动那一条材料行（issue 里对应项消掉）
  merge(a,b)      → 两行并一行：页取并集、seq 重排、**被并走的行消失**
  split(uid, n)   → 拆成两行：选中的页成新行、原行保留剩余页；两行都标 doubt
  reocr(seq)      → 换通道重读该页内容卡 → 该页所在材料行的 title/date 若原来
                    取自这张卡则刷新（其余行不动）——最轻的脏传播
"""
from __future__ import annotations

import json
import os
import time

from archive.domain.classes import dir_sort_key   # 目录序（唯一口径，Excel/PDF/全景卡共用）


def _reorder(project_json: str, seqs, form=None) -> list[int]:
    """把一组页按"装订序"重排（并份/拆分后调成员顺序时用）。

    排序规则本身只在 engine/seg.py 一处（那里才知道册子的栏目版序）；
    这里只是把该页所在的整卷记录读出来交给它，避免交互层再抄一份排序逻辑。
    """
    from archive.engine import seg
    return seg.reorder(sorted(seqs), _load_records(project_json), form)


# ── 现场读写（09-06 起存 SQLite archive_states 表；JSON 文件仅作首次迁移兜底）──

def _review_dir(project_json: str) -> str:
    """项目目录下的 review/ 子目录（旧 JSON 现场位置；迁移兜底用）。"""
    return os.path.join(os.path.dirname(project_json), "review")


def review_path(project_json: str) -> str:
    """旧现场文件路径：<项目>/review/materials.json（迁移兜底读它）。"""
    return os.path.join(_review_dir(project_json), "materials.json")


def save_state(project_json: str, materials: list, issues: list,
               person: str, job_id: str = "", user_id: str = "",
               channel: str = "") -> dict:
    """把**某一家模型**的现场写进 SQLite（同一家覆盖，别家不动）。返回落盘后的 dict。

    channel="" 不传时 = 写"当前展示的那一家"（见 active_channel）—— 这样所有
    op_*（改类/并/拆）不需要各自记着模型，改的永远是用户正在看的那份。
    """
    from station import db
    ch = channel or active_channel(project_json)
    state = {"person": person, "materials": materials, "issues": issues,
             "channel": ch, "updated": int(time.time())}
    db.archive_state_save(project_json, job_id, user_id, person, state, ch)
    return state


def register_volume(project_json: str, person: str, user_id: str = "") -> dict:
    """建卷时先登记**卷级元数据行** —— 让"这卷是谁的"在**识别开始前**就成立。

    为什么需要它（09-12）：页图端点 `/api/archive/page` 的鉴权查的是
    `db.archive_state_owner`（archive_states 表里这卷的归属）。而现场本来只在
    **识别跑完**才写 → 识别过程中进度面板想显示"正在识别这一页"的缩略图，会拿到
    404，在浏览器里就是一片破图。建卷时先登记，归属从建卷那一刻起就有效。

    ★ 这一行（channel=''）从 09-12 起是**卷级元数据行**：归属、姓名、`stage`，
      以及"当前展示哪一家模型"（`active`）。别家模型的现场各占一行，互不覆盖。
    ★ `stage="created"` 是给 t_recognize 认的标记：它把"空现场"分两种处理，
      建卷登记不许被当成"上一轮识别的残留"清掉（清了鉴权又回 404）。
    """
    state = {"person": person, "materials": [], "issues": [],
             "stage": "created", "active": "", "updated": int(time.time())}
    from station import db
    db.archive_state_save(project_json, "", user_id, person, state, "")
    return state


def active_channel(project_json: str) -> str:
    """当前**展示哪一家**模型的现场（元数据行里的 active）。

    没有 active（老卷/只建了卷还没识别）时回落到最近写入的那一家 —— 保证
    "重开这卷看到的是最后识别的那份"，而不是空白。
    """
    from station import db
    meta = db.archive_state_get(project_json, "")
    if meta and meta.get("active"):
        return meta["active"]
    chans = db.archive_state_channels(project_json)
    return chans[0]["channel"] if chans else ""


def set_active(project_json: str, channel: str) -> None:
    """切换"当前展示哪一家模型"。只改元数据行，不动任何一家的现场数据。"""
    from station import db
    meta = db.archive_state_get(project_json, "") or {}
    meta.update({"active": channel, "updated": int(time.time())})
    db.archive_state_save(project_json, "", "", meta.get("person") or "",
                          meta, "")


def channels(project_json: str) -> list[dict]:
    """这卷有哪几家模型的现场（新→旧），并标出哪家是当前展示。

    给"报菜单"和"切换展示"用。返回 [{"channel","person","updated","active"}]。
    """
    from station import db
    cur = active_channel(project_json)
    out = db.archive_state_channels(project_json)
    for d in out:
        d["active"] = (d["channel"] == cur)
    return out


def cache_coverage(project_json: str, channel: str) -> tuple[int, int]:
    """这卷有多少页在**某一家**的 OCR 缓存里 —— 报菜单用（"已缓存 126/126 页"）。

    要点：缓存键里的**提示词指纹**用的是**当前**的建档提示词（`loader.mark()`）——
    提示词一改，所有旧缓存自动失效，这里的数字会跟着归零，正好如实反映
    "换了提示词以后，那家也得重读"。所以别缓存这个结果，每次都现算。

    返回 (已缓存页数, 总页数)；卷读不出来返回 (0, 0)。
    """
    from archive.skill import loader
    from archive.storage import ocr_cache
    try:
        from archive.storage.project import load_project
        _, records = load_project(project_json)
    except Exception:                            # noqa：读不动就当没有
        return 0, 0
    prompt = loader.mark()
    hit = 0
    for r in records:
        md5 = r.get("md5") or ""
        if md5 and ocr_cache.lookup(md5, channel, prompt) is not None:
            hit += 1
    return hit, len(records)


def load_state(project_json: str, channel: str = "") -> dict | None:
    """读现场：**某一家的**；不指定就是当前展示的那家。

    这是全工具层的唯一漏斗 —— 所以"默认读哪家"这件事只在这里决定一次：
    调用方（_state/export/op_*）一律零改动，拿到的永远是"用户正在看的那份"。
    ★ 顺序：先查 DB（指定 channel → 没指定就看 active_channel）；
      DB 里没有再看旧 JSON 文件（升级前的卷不丢）；都没有 None。
    """
    from station import db
    ch = channel or active_channel(project_json)
    st = db.archive_state_get(project_json, ch) if ch else None
    if st is not None:
        return st
    if channel:                                  # 点名要某一家而它没有 → 别拿别家冒充
        return None
    meta = db.archive_state_get(project_json, "")   # 都没识别过 → 至少给元数据行
    if meta is not None:
        return meta
    p = review_path(project_json)                # 旧版遗留卷的兜底
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                        # noqa：坏文件当没有
            return None
    return None


def invalidate_state(project_json: str, channel: str = "") -> None:
    """删掉某卷**某一家的**现场（重识别前清旧空现场用）。

    ★ channel 不传 = 删**卷级元数据行**（老行为，只给"没识别过就当空卷重来"用）；
      要清具体某一家必须显式传 —— 换模型重跑**绝不能**把别家的结果一起删了。
    """
    from station import db
    db.archive_state_delete(project_json, channel)


# ensure_state() 已删（09-11）：零调用方。识别跑完落现场走 station_adapter 里的 save_state。


# ── 修正账本（corrections 表：M4 反哺的原料）─────────────────────────

def record_correction(project_json: str, kind: str, before: dict,
                      after: dict, user_id: str = "",
                      extra: dict | None = None) -> None:
    """记一条修正：什么操作、AI 原判是什么、用户改成了什么（追加进 DB）。

    攒着给「口径学习」聚合成提案。记账失败不影响主操作（账 ≠ 命）。
    extra：附加字段（如 `{"src": "excel"}` 标出这条来自终版对账而不是对话改类）。
    """
    payload = {"before": before, "after": after}
    if extra:
        payload.update(extra)
    try:
        from station import db
        db.correction_add(project_json, user_id, kind, payload)
    except Exception:                            # noqa：账本故障不拦主流程
        pass


def read_corrections(project_json: str) -> list[dict]:
    """读全部修正记录（M4 聚合用；DB 版，旧→新）。"""
    from station import db
    return db.corrections_list(project_json)


# ── 检查点（每次交互操作前自动存，UI 可列可恢复）─────────────────────

def save_checkpoint(project_json: str, state: dict, label: str,
                    user_id: str = "", channel: str = "") -> int:
    """把当前现场存成一个检查点，返回检查点 id。

    调用时机：op_* 每个操作**动手改数据之前**——这样"恢复到某步之前"
    永远可行（undo 语义）。user_id 空也没关系（本地单机场景）。
    channel 不传 = 当前展示那一家：检查点跟着模型走，**换模型后各家的回退链互不干扰**。
    """
    from station import db
    return db.checkpoint_add(project_json, user_id, label, state,
                             channel or active_channel(project_json))


def list_checkpoints(project_json: str, limit: int = 20,
                     channel: str = "") -> list[dict]:
    """列出某卷**某一家模型**的检查点（新→旧），给"恢复到哪一步"用。

    ★ 这个函数是回退的**前置**：回退要一个 ck_id，而 id 是 DB 自增的
      全局号（不是 1..n），用户和模型都猜不出来 —— 没有它，检查点就是"每步都在写、
      却没人能列出来"的死链。别把这条删了。
    """
    from station import db
    return db.checkpoints_list(project_json, limit,
                               channel or active_channel(project_json))


def restore_checkpoint(project_json: str, ck_id: int) -> dict:
    """恢复到某检查点：把它写回**它自己那家模型**的现场（并先给"恢复前"打个点，可反悔）。

    ★ 必须校验"这个编号属于本卷"：ck_id 是全局自增号，模型可能随口编一个，
      而那个号也许属于**另一卷** —— 不校验就把它写进当前卷（09-12 审计发现的真隐患，
      当时只靠 system.md 里一句"别自己编编号"兜着）。现在在代码层拦。
    """
    from station import db
    ck = db.checkpoint_get(ck_id)
    if ck is None:
        raise ValueError(f"检查点不存在：{ck_id}")
    if ck["project"] != project_json:
        raise ValueError(f"检查点 {ck_id} 不属于当前这卷，不能拿来恢复。"
                         f"先看看本卷有哪些检查点。")
    ch = ck["channel"] or active_channel(project_json)
    st = ck["state"]
    cur = load_state(project_json, ch)
    if cur:
        db.checkpoint_add(project_json, "", f"恢复前自动存（回退到检查点 {ck_id} 前）",
                          cur, ch)
    db.archive_state_save(project_json, "", "", st.get("person") or "", st, ch)
    return st


# ── 口径提案：见 service/learn.py（09-13 起）─────────────────────────
# 这里原来有 M4 的 aggregate_corrections / draft_proposal（"攒一批再从账本起草口径"）。
# 09-13 口径学习落地后它们被取代：**每次改类就即时开一条提案**，同一件事重复改会
# 累加次数（learn.from_correction 的 count），本质上是同一份"按(文种,类)分组计数"，
# 只是从"事后批量"变成了"当场记账"。留着就是两套并行的重复机制，故删除。


# ── 页→份 反查（脏传播的地图）────────────────────────────────────────

def _load_records(project_json: str) -> list[dict]:
    """读整卷页记录（photos.json）。"""
    from archive.storage.project import load_project
    _, records = load_project(project_json)
    return records
    # 注：load_project 读 project.json 的 records_file 字段定位 photos.json；
    # 测试手搓的 project.json 若缺该字段会 KeyError——真实项目由 create_project
    # 生成、字段必在，这里不做兼容（宁缺毋滥）。


def find_record(records: list[dict], seq: int) -> dict | None:
    """按 seq 找一页的记录。"""
    return next((r for r in records if r.get("seq") == seq), None)


def mat_by_uid(materials: list, uid: str) -> dict | None:
    """按 uid 找材料行。"""
    return next((m for m in materials if m.get("uid") == uid), None)


def _reseq(materials: list) -> None:
    """材料重编号：按各类内现有顺序重排 seq（1..n）；uid 保持不变（引用稳定）。"""
    by_cat: dict = {}
    for m in materials:
        by_cat.setdefault(m.get("category") or "?", []).append(m)
    for rows in by_cat.values():
        for i, m in enumerate(rows, start=1):
            m["seq"] = i


# ── 四个交互操作（每个都：改数据 + 记账 + 返回新现场）─────────────────

def op_set_category(project_json: str, uid: str, category: str,
                    title: str | None = None, user_id: str = "") -> dict:
    """改一份的类别（顺带可改名称）。category 非法会抛 ValueError。

    流程：先打检查点（可回退）→ 改数据 → 记账 → 落库。
    脏传播：只动这一行；对应 C0/C1 问题若因此消除则从 issues 里移除。
    """
    from archive.domain.classes import normalize_category, SUB_TO_MAIN
    cat = normalize_category(category)
    if cat is None:
        raise ValueError(f"非法类别：{category}")
    st = load_state(project_json)
    if st is None:
        raise ValueError("还没有识别现场（先跑完识别到确认门）")
    m = mat_by_uid(st["materials"], uid)
    if m is None:
        raise ValueError(f"材料不存在：{uid}")
    save_checkpoint(project_json, st, f"改类前：{m.get('title') or uid} → {category}", user_id)
    before = {"category": m.get("category"), "title": m.get("title"),
              "title_src": m.get("title_src")}
    m["category"] = cat
    m["main"] = SUB_TO_MAIN.get(cat) or cat
    if title is not None and title.strip():
        m["title"] = title.strip()
        m["title_src"] = "review"                  # 用户手改 → 名称来源=审核
    m["doubt"] = False                              # 人拍板了 → 不再存疑
    m["verdict"] = "ok"
    # 顺手消掉指向这行的 C0/C1 问题
    st["issues"] = [i for i in st["issues"]
                    if not (i.get("code", "").startswith("C")
                            and set(i.get("seq") or []) == set(m["members"]))]
    _reseq(st["materials"])
    record_correction(project_json, "set_category",
                      before, {"category": cat, "title": m.get("title"),
                               "pages": m["members"]}, user_id)
    save_state(project_json, st["materials"], st["issues"], st["person"],
               user_id=user_id)
    return st


def op_merge(project_json: str, uid_a: str, uid_b: str, user_id: str = "") -> dict:
    """把两份并成一份（典型：干部履历表各栏被拆成多份）。

    流程：检查点 → 改数据 → 记账 → 落库。
    规则：页取并集；保留**页数多的那一行**的类别/标题/日期；copies/dup_pages 合并。

    ★ 保留哪一行：以前比"谁的最小照片号靠前"—— 照片顺序随机后这个判据毫无意义
      （可能留下只有 1 页的碎片、丢掉 13 页的主体）。改成留页数多的那份；
      页数相同再比照片号（至少确定、可复现）。
    """
    st = load_state(project_json)
    if st is None:
        raise ValueError("还没有识别现场")
    a, b = mat_by_uid(st["materials"], uid_a), mat_by_uid(st["materials"], uid_b)
    if a is None or b is None:
        raise ValueError(f"材料不存在：{uid_a} / {uid_b}")
    if a["uid"] == b["uid"]:
        raise ValueError("同一份材料无法与自身合并")
    save_checkpoint(project_json, st,
                    f"并份前：《{a.get('title')}》+《{b.get('title')}》", user_id)
    key = lambda m: (-len(m.get("members") or []), min(m.get("members") or [0]))
    keep, gone = (a, b) if key(a) <= key(b) else (b, a)
    before = [{"uid": a["uid"], "category": a.get("category"),
               "title": a.get("title"), "pages": a["members"]},
              {"uid": b["uid"], "category": b.get("category"),
               "title": b.get("title"), "pages": b["members"]}]
    # ★ 用 seg.reorder 重排装订序（并进来的页该插在哪，规则只在 seg 一处）
    keep["members"] = _reorder(project_json, set(keep["members"]) | set(gone["members"]),
                               keep.get("form"))
    keep["dup_pages"] = sorted(set(keep.get("dup_pages") or [])
                               | set(gone.get("dup_pages") or []))
    keep["assigned_pages"] = keep["members"] + keep["dup_pages"]
    keep["raw_seqs"] = sorted(set(keep.get("raw_seqs") or []) | set(keep["members"]))
    keep["pages"] = len(keep["members"])
    keep["copies"] = 1 + (1 if keep["dup_pages"] else 0)   # 有复本记 2 份，没有 1 份
    keep["doubt"] = bool(a.get("doubt") or b.get("doubt"))
    keep["rep"] = keep["members"][0] if keep["members"] else keep.get("rep")
    st["materials"] = [m for m in st["materials"]
                       if m["uid"] not in (a["uid"], b["uid"])] + [keep]
    st["materials"].sort(key=dir_sort_key)         # 目录序（rules §5.1，与 Excel/PDF 同一口径）
    _reseq(st["materials"])
    record_correction(project_json, "merge", before,
                      {"uid": keep["uid"], "category": keep.get("category"),
                       "title": keep.get("title"), "pages": keep["members"]},
                      user_id)
    save_state(project_json, st["materials"], st["issues"], st["person"],
               user_id=user_id)
    return st


def op_split(project_json: str, uid: str, seqs: list[int],
             user_id: str = "") -> dict:
    """把一份拆成两份：seqs 里的页抽出来成新份，其余留在原份。

    流程：检查点 → 改数据 → 记账 → 落库。新份沿用原份类别/标题，两行都标存疑。
    """
    st = load_state(project_json)
    if st is None:
        raise ValueError("还没有识别现场")
    m = mat_by_uid(st["materials"], uid)
    if m is None:
        raise ValueError(f"材料不存在：{uid}")
    seqs = sorted(set(int(s) for s in seqs))
    if not seqs or not set(seqs) <= set(m["members"]):
        raise ValueError("要拆出的页必须属于该材料")
    if len(seqs) == len(m["members"]):
        raise ValueError("不能把整份都拆走（那等于没拆）")
    save_checkpoint(project_json, st,
                    f"拆份前：从《{m.get('title')}》拆出第{seqs}张", user_id)
    before = {"uid": uid, "category": m.get("category"),
              "title": m.get("title"), "pages": m["members"]}
    # 拆出来的两半都重排一遍装订序（拆出来的一撮可能本来夹在中间）
    kept = [s for s in m["members"] if s not in seqs]
    m["members"] = _reorder(project_json, kept, m.get("form"))
    m["pages"] = len(m["members"])
    m["assigned_pages"] = m["members"] + (m.get("dup_pages") or [])
    m["raw_seqs"] = sorted(m["members"])
    m["rep"] = m["members"][0] if m["members"] else m.get("rep")
    m["doubt"] = True
    new_uid = f"m{max(int(x['uid'][1:]) for x in st['materials']) + 1}"
    new_members = _reorder(project_json, seqs, m.get("form"))
    new_mat = {**m, "uid": new_uid, "members": new_members, "pages": len(new_members),
               "assigned_pages": new_members, "raw_seqs": sorted(new_members),
               "rep": new_members[0] if new_members else None,
               "doubt": True, "title_src": "engine"}
    st["materials"].append(new_mat)
    _reseq(st["materials"])
    record_correction(project_json, "split", before,
                      {"kept": {"uid": uid, "pages": m["members"]},
                       "new": {"uid": new_uid, "pages": seqs}}, user_id)
    save_state(project_json, st["materials"], st["issues"], st["person"],
               user_id=user_id)
    return st


def op_reocr(project_json: str, seq: int, channel: str | None = None) -> dict:
    """换一家 OCR 重读一页 → 返回新旧内容卡对比（不直接改现场）。

    返回 {"seq","old","new","channel","cached"}；前端展示对比，用户选"采用"才调
    apply_reocr 真正落（两步设计：先看货，再付款——防止对比完不满意白改）。
    "cached"= 这次是直接拿那家**已有的缓存**（0 花费），不是现读的。
    """
    from archive.engine.graph import reocr_page
    records = _load_records(project_json)
    r = find_record(records, seq)
    if r is None:
        raise ValueError(f"页不存在：{seq}")
    if channel is None:
        # "换一家"优先挑**这卷已经有结果/缓存的那几家**（多模型并存后这是最自然的
        # 对比对象，而且能命中缓存 = 白拿）；一家都没有才让 graph 层去 .env 里找。
        # 09-12 之前是直接让 graph 层从 .env 的 PROVIDERS 里挑，用户自定义的 provider
        # 会被无声跳过。
        from station import db
        cur = active_channel(project_json)
        others = [c["channel"] for c in db.archive_state_channels(project_json)
                  if c["channel"] != cur]
        channel = others[0] if others else None
    cmp_ = reocr_page(r, channel=channel)
    return {"seq": seq, "old": cmp_["old"], "new": cmp_["new"],
            "channel": cmp_["channel"], "cached": bool(cmp_.get("cached"))}


def op_apply_reocr(project_json: str, seq: int, card: dict,
                   user_id: str = "") -> dict:
    """采用新 OCR 卡：photos.json 回写该页 + 刷新所在材料行 + 检查点/记账。

    脏传播（最轻）：只刷新**代表页是该页**的材料行的 title/date（其余行不动）。

    ★ "首页"的判据从 members[0] 改成 rep：照片顺序随机后 members[0] 是"装订后的第一页"，
      而 rep 才是"取材料名的那个代表页"（封面那种）。用户改某页读法，最该跟着变的就是它。
    """
    import json as _json
    records = _load_records(project_json)
    r = find_record(records, seq)
    if r is None:
        raise ValueError(f"页不存在：{seq}")
    st = load_state(project_json)
    if st:
        save_checkpoint(project_json, st, f"换OCR前：第{seq}张采用新读法", user_id)
    before = (r.get("ocr") or {}).get("mark") or {}
    r["ocr"] = {**(r.get("ocr") or {}), "mark": card.get("mark") or card,
                "title": card.get("title"), "date": card.get("date"),
                "texts": card.get("texts") or []}
    # photos.json 回写（识别结果持久化的另一半：缓存管"跨任务"，这里管"本卷"）
    proj_dir = os.path.dirname(project_json)
    with open(os.path.join(proj_dir, "photos.json"), "w", encoding="utf-8") as f:
        _json.dump(records, f, ensure_ascii=False, indent=1)
    # 刷新现场里"代表页=该页"的材料行（脏传播）
    if st:
        for m in st["materials"]:
            if (m.get("rep") == seq
                    or (len(m.get("members") or []) == 1 and m["members"][0] == seq)):
                mk = r["ocr"]["mark"]
                m["title"] = mk.get("t") or m.get("title")
                m["date"] = r["ocr"].get("date") or m.get("date")
                m["evidence"] = (mk.get("s") or m.get("evidence") or "")[:200]
        record_correction(project_json, "reocr",
                          {"seq": seq, "old": before.get("t")},
                          {"seq": seq, "new": r["ocr"]["mark"].get("t")}, user_id)
        save_state(project_json, st["materials"], st["issues"], st["person"],
                   user_id=user_id)
    return st or {"materials": [], "issues": []}
