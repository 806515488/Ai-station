"""archive 技能工具 —— 对话式档案整理的全部工具面（claude code 模式的"底层工具"）。

对标 codex/claude code：它们的底层工具是 read/edit/shell，我们的底层工具是
建卷/识别/改类/并拆/换OCR/导出。用户在对话里说目标，agent（模型）选工具干活——
固定四幕、表单、分拣台按钮都不存在了，这是唯一入口。

工具分六组（共 18 个）：
  输入    ask_photos（发上传卡片）、scan_photos（建卷）
  识别    recognize（后台 job 整卷识别，可换识图模型/重读）、job_status（查进度）
  模型    list_vision_models（报菜单：哪几家可用、各自缓存情况）、use_model（切看哪家）
  现场    list_state、show_page、show_overview、show_files
  修正    set_category、merge、split、reocr（对比→采用）、set_orientation
  回退    checkpoints（不传编号=列出，传了=退回）
  出件    export（risk=approve，宿主批准闸兜底）
  学习    list_learning（看待确认的口径提案；确认动作在卡片上，模型碰不到）

每个 Tool 都带 **label**（一句给人看的人话，如"改材料类别"）—— 对话流里显示的是它，
不是 `archive.set_category` 这种名字（用户看不懂）。description 才是写给模型看的。

"当前卷"怎么定：skills/archive 的数据目录（ctx.data_dir）里存一个 current.json
（本技能最近一次 scan/recognize 的 project 路径）——同一用户同一时间只整理一卷，
Thread 会话天然对应"手头这卷"。多卷并行是 §9 开放问题，初版不做。

渲染契约（渲染即工具）：工具想给人看东西，返回
  {"text": 给模型看的一句结论, "render": {"type": ..., ...}}
宿主 agent 循环把 render 转成 EV_RENDER 事件推给前端（见 core/agent.run_tool）。
"""
from __future__ import annotations

import json
import os
import re
import time

from station.core.tool import Tool


# ── "当前卷"登记（ctx.data_dir/current.json）─────────────────────────

def _cur_path(ctx) -> "os.PathLike | str":
    """本技能数据目录下 current.json 的路径（登记"手头这卷"）。"""
    return os.path.join(str(ctx.data_dir), "current.json")


def _set_current(ctx, project: str, person: str = "", job_id: str = "") -> None:
    """把"现在在整理哪一卷"记下来（scan/recognize 成功后调）。"""
    os.makedirs(str(ctx.data_dir), exist_ok=True)
    with open(_cur_path(ctx), "w", encoding="utf-8") as f:
        json.dump({"project": os.path.abspath(project), "person": person,
                   "job_id": job_id, "at": time.time()}, f, ensure_ascii=False)


def _get_current(ctx) -> tuple[str, str]:
    """读"手头这卷"→ (project_json, person)；没有给 ("", "")。

    每个工具开头都调它：拿不到就返回人话提示"还没建卷"，模型会引导用户先传照片。
    """
    p = _cur_path(ctx)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("project") or "", d.get("person") or ""
        except Exception:                    # noqa：坏文件当没有
            return "", ""
    return "", ""


def _state(ctx) -> tuple[str, dict]:
    """拿 (project, 现场state)；没有现场抛 ValueError（工具统一转人话）。"""
    pj, person = _get_current(ctx)
    if not pj:
        raise ValueError("还没有正在整理的档案卷。先发照片（我会给上传按钮），或说出卷所在目录。")
    from archive.service.interactive import load_state
    st = load_state(pj)
    if st is None:
        raise ValueError("这卷还没识别完（现场未建立）。先让我识别：说\"开始识别\"。")
    return pj, st


def _by_uid(st: dict, keyword: str):
    """按 uid 或标题关键词找材料行（模型爱说"任免表"而不是 m7）——沿用旧 review 代码。"""
    from archive.service.interactive import mat_by_uid
    m = mat_by_uid(st["materials"], keyword)
    if m is not None:
        return m
    kw = (keyword or "").strip()
    for m in st["materials"]:
        t = (m.get("title") or "").strip()
        if kw and t and (kw in t or t in kw):    # 双向包含，两边非空才认
            return m
    return None


def _by_mention(st: dict, target: str):
    """把“第41张/页/份”这类口语指代解析成材料行（按成员页码找）。

    用户张口就是"第41张那个改成九-2"、"41 和 42 并一起" —— 模型多半会把
    "第41张"原样塞进 target，所以**每个接受 target 的工具都要能用它**
    （set_category/merge/split 都接了；只给 set_category 接的话，
    "第41张和42张并一起"会直接失效）。
    ★ 注意：路由层（core/router.py）**只决定进哪个技能，不决定调哪个工具** ——
      所以别指望"某句话术被 L1 翻译成工具参数"，工具选择 100% 靠模型读 description。
      （这里原有一句"L1 会把'第41张改成九-2'转成 set_category(...)"，是过时说法，已删。）
    """
    m = re.search(r"第\s*(\d+)\s*(?:张|页|份)", target or "")
    if not m:
        return None
    seq = int(m.group(1))
    return next((x for x in (st.get("materials") or [])
                 if seq in (x.get("members") or [])), None)


def _fmt_row(m: dict) -> str:
    """材料行 → 一行人话（工具结果给模型看的格式，也是说话素材）。"""
    return (f"{m['uid']}：{m.get('category') or '未定类'}"
            f"《{m.get('title') or '未命名'}》 第{','.join(map(str, m['members']))}张"
            f"{'（存疑）' if m.get('doubt') else ''}")


def _err(e: Exception) -> str:
    """业务异常 → 人话（ValueError 是我们主动抛的，直接用；其它给类型+信息）。"""
    return str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"


def _owner(ctx) -> str:
    """当前操作归属谁（对话场景从 Thread 带，job 场景 Context 直接带）。"""
    t = getattr(ctx, "thread", None)
    return (getattr(t, "user_id", "") if t else "") or getattr(ctx, "user_id", "") or ""


def _prod_meta(ctx, pj: str, person: str) -> dict:
    """产物来源信息：归属用户 + 档案技能 + 以卷为单位的分组（抽屉/卡片共用）。"""
    return {"owner": _owner(ctx), "skill_id": "archive",
            "group_key": os.path.abspath(pj),
            "group_label": f"干部档案 · {person or '未命名卷'}"}


# ══ 输入组 ═══════════════════════════════════════════════════════════

def t_ask_photos(ctx) -> dict:
    """工具：发一张上传卡片（用户在自己电脑选这卷翻拍照片）。

    卡片按钮走前端已有的 POST /api/photos（客户端上传，落 uploads/<人名>-<uid>/）。
    这是"参数表单删除后"照片进系统的新路：agent 缺输入就在对话里要。
    """
    return {"text": "已发上传卡片，等用户选照片。",
            "render": {"type": "upload-card",
                       "hint": "选择这位干部整卷翻拍照片的文件夹（或直接多选照片），可分几次选。"}}


def _person_from_dir(dir: str) -> str:
    """从上传目录名里抠出人名 —— `uploads/张三-6a7ebab3/` → `张三`。

    上传落地时目录名是 `<人名>-<8位随机>`（见 station/app/uploads.store_upload，
    后缀是为了防两卷撞名）。直接拿 basename 当人名会带一串 uid（"张三-6a7ebab3"），
    建出来的卷名和导出件都难看，所以**回落时必须把这截洗掉**。
    """
    base = os.path.basename(str(dir).rstrip("/\\"))
    return re.sub(r"-[0-9a-f]{6,}$", "", base) or base


def t_scan_photos(ctx, dir: str = "", person: str = "") -> str:
    """工具：把一批照片建卷（create_project 复制入库+编号）。

    dir 来自上传卡片回填（前端把服务器目录发给模型）或用户手输的本机目录。
    person 是干部姓名 —— 由上传卡片上的输入框收集、随消息一起带过来
    （消息形如"照片已上传：<目录>（126 张，姓名：张三）"）。

    ★ **person 为空时不建卷**，只回一句"先确认姓名"（并附上从目录名猜的名字供参考）。
      这是产品决定落进代码里（"留空就问一句再建卷"，09-12）：光靠提示词说"要问"
      挡不住模型图省事 —— 它会把目录名当姓名直接用（用户实测反馈：消息里直接写着
      "126 张，李明"，而他根本没填）。卡在这里，模型忘了问也建不成卷。
      用户说"就用文件夹名"时，模型把上面给的那个名字当 person 传过来即可。
    """
    from archive.storage.project import create_project
    if not dir or not os.path.isdir(dir):
        return f"目录不存在：{dir or '（空）'}。请先上传照片或给一个有效目录。"
    name = (person or "").strip()                 # 没填就是没填，不拿目录名顶替
    if not name:
        guess = _person_from_dir(dir)             # 目录名里的名字，只用来"提示"，不用来建卷
        hint = f"（目录名看着像《{guess}》）" if guess and guess != "照片" else ""
        return (f"还没确认这是哪位干部的档案，先不建卷。{hint}"
                "先问用户一句；拿到姓名后带 person 再调一次。"
                "用户说\"就用文件夹名\"时，把上面那个名字当 person 传过来。")
    proj_root = ctx.data_dir / "projects"        # 宿主按技能分目录（见 Context）
    proj_root.mkdir(parents=True, exist_ok=True)
    try:
        pj = create_project(name, dir, str(proj_root), order="拍摄时间")
    except Exception as e:                       # noqa：目录没图/选错目录等
        return f"建卷失败：{_err(e)}"
    _set_current(ctx, pj, name)
    # 建卷即登记归属（空现场）：这样**识别过程中**就能取这一卷的页图了 ——
    # 页图端点按 archive_states 的归属鉴权，而识别结果要到跑完才写进去。
    # 不登记的话，识别进度面板每张缩略图都会拿到 404（浏览器里=破图）。
    from archive.service.interactive import register_volume
    register_volume(pj, name, _owner(ctx))
    from archive.storage.project import load_project
    _, records = load_project(pj)
    return (f"已建卷《{name}》：{len(records)} 页（按拍摄时间排序）。"
            f"下一步对我说\"开始识别\"即可。")


# ══ 识别组 ═══════════════════════════════════════════════════════════

def _vision_chain_label(ctx) -> str:
    """当前用户「识图模型」槽的链首 id —— 也就是"不指定时，默认会用哪一家读"。

    与 station_adapter 里算 `vision_channel` 的口径**必须一致**（都是
    `modelcfg.chain_label(resolve(uid,"vision"))`）：这个标签既是 OCR 缓存键的第二段，
    也是"这卷的现场归哪一家"的键。两处口径一旦不一致，recognize 的"是否识别过"判断
    就会跟实际落库的 channel 对不上（表现：明明识别过却一直让你重跑，或反过来）。
    """
    from station import modelcfg
    return modelcfg.chain_label(modelcfg.resolve(_owner(ctx), "vision"))


def t_recognize(ctx, provider: str = "", refresh: bool = False) -> dict | str:
    """工具：起后台识别 job（整卷 LangGraph 流水线），立刻返回 job_id。

    为什么后台：126 页要 ~90 秒，HTTP 请求等不起。进度由前端轮询 job 状态展示
    （每页还会推一条"正在识别第 N 张"）；agent 之后用 job_status 查结果。

    provider  换个识图模型读这卷（空=用你「模型配置」里的识图链）。
    refresh   忽略缓存重读一遍（用户明确说"重新识别/有缓存也重读"时才传）。

    **默认什么都不重跑**：这家已经识别过就直接说"识别过了"，并提示可换模型/可重跑。
    ★ 换一家已经有结果时**不重跑**，直接切过去展示（用户要的是"看那家读的"，
      不是"再烧一遍"）—— 见下面 set_active 那段。
    """
    from station.jobs.manager import get_manager
    from station.skills.registry import get_registry
    pj, person = _get_current(ctx)
    if not pj:
        return "还没有正在整理的档案卷，先发照片建卷。"
    from archive.service.interactive import (
        invalidate_state, load_state, set_active)
    # 这一趟会落在哪个 channel：指定了就是那家，没指定就是识图链的链首
    ch = provider or _vision_chain_label(ctx)
    st = load_state(pj, ch) if ch else load_state(pj)
    if st and (st.get("materials") or []) and not refresh:
        if provider:
            set_active(pj, ch)               # 切过去展示，别重跑
            out = t_show_overview(ctx)
            out["text"] = (f"「{st.get('model_name') or provider}」已经读过这卷了，"
                           f"我直接切过去给你看（没有重新读，不花钱）。")
            return out
        return ("这卷已经识别过了。要接着核对就说\"看全景\"；"
                "想换个识图模型读、或者重新读一遍，也直接说。")
    # 空现场要分两种（09-12）：stage="created" 是建卷时写的**归属登记**，必须留着
    # （页图鉴权靠它）；其它空现场是上一轮识别留下的 0 材料残留，清掉重跑。
    # ★ 只清**目标这家**的：换模型重跑绝不能把别家已经读好的结果删掉。
    if st and st.get("stage") != "created" and not (st.get("materials") or []):
        invalidate_state(pj, ch)
    # 取**自己所在的技能**（ctx.skill_id 由宿主填好：起对话/起工具时就写上了）。
    # 执行器是本技能自己的第二个入口（见本文件底部的 build_runner）——不再按字符串
    # 去够另一个技能：那样等于把跨技能依赖藏进注册表查找，谁也看不出它俩是一体的。
    skill = get_registry().get(ctx.skill_id or "archive")
    if skill is None or skill.build_runner is None:
        return "识别引擎没装载，请联系管理员。"
    # 归属走 _owner(ctx)（thread 优先、回落 ctx.user_id）—— 别只从 thread 上摸：
    # 这条 job 的 user_id 决定"识别结果算谁的"，thread 缺席时（非对话路径）会变成
    # 无主任务。规矩见 docs/conventions.md「技能落库归属一律取 ctx.user_id」。
    # provider/refresh 随 args 透传给 build_runner（**args 就是它的形参）
    job = get_manager().submit(
        skill, {"project": pj, "person": person,
                "provider": provider, "refresh": bool(refresh)},
        user_id=_owner(ctx))
    _set_current(ctx, pj, person, job.id)
    # 把“这个会话正在跟的识别任务”写进 Thread.meta —— 前端刷新/回放时才能知道
    # 该接哪条 job 的进度，而不是让后台任务“消失”。
    thread = getattr(ctx, "thread", None)
    if thread is not None:
        thread.meta["job_id"] = job.id
    who = f"（识图模型：{provider}）" if provider else ""
    how = "，忽略缓存重读" if refresh else ""
    return (f"识别已开始{who}{how}（任务 {job.id}），预计每百页约 1-2 分钟。"
            f"进度在下方进度条实时显示；期间你可以随时问我进度或插话。")


def t_job_status(ctx, job_id: str = "") -> str:
    """工具：查识别 job 的状态/进度（agent 汇报进度用）。"""
    from station.jobs.manager import get_manager
    pj, person = _get_current(ctx)
    if not job_id:
        thread = getattr(ctx, "thread", None)
        job_id = ((thread.meta or {}).get("job_id") or "") if thread else ""
        if not job_id:                            # 旧会话兜底：读技能目录的 current.json
            p = _cur_path(ctx)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    job_id = json.load(f).get("job_id") or ""
    if not job_id:
        return "还没有进行过识别。"
    snap = get_manager().get(job_id)
    if snap is None:
        return f"任务 {job_id} 不存在。"
    return (f"识别任务 {job_id}：{snap['status']} {snap['progress']}% —— "
            f"{snap.get('message') or ''}"
            + ("" if snap["status"] == "running"
               else f"（材料 {len((snap.get('result') or {}).get('materials') or [])} 行，"
                    f"问题 {len((snap.get('result') or {}).get('issues') or [])} 项）"
               if snap.get("result") else ""))


# ══ 现场组（展示）════════════════════════════════════════════════════

def _vision_menu(ctx, pj: str) -> tuple[list[dict], str]:
    """列出可用的识图模型 + 各自对这卷的识别情况，返回 (菜单行, 一句汇总)。

    每家的状态分三种（这就是用户要的"哪些已缓存、哪些没有"）：
      · 已识别过        —— archive_states 里有这家的一整份现场（可以直接切过去看）
      · 缓存里有 N/M 页 —— OCR 缓存有货但没跑过完整识别（重跑会很快，只补缺的）
      · 没读过          —— 缓存也是空的（要真读一遍）
    菜单的顺序：识图链里的按链序在前（链首=默认会用的那家），其余排后面。
    """
    from station import modelcfg
    from archive.service.interactive import active_channel, cache_coverage, channels
    provs = modelcfg.vision_providers(_owner(ctx))
    done = {c["channel"]: c for c in channels(pj)}
    cur = active_channel(pj)
    rows, total = [], 0
    for p in provs:
        got, total = cache_coverage(pj, p["id"])
        if p["id"] in done:
            state = f"已识别过（{total} 页）"
        elif got:
            state = f"缓存里有 {got}/{total} 页（没跑过完整识别）"
        else:
            state = f"没读过（{total} 页要读一遍）"
        if p["id"] == cur:
            tail = "  ← 当前展示"
        elif p["in_chain"]:
            tail = "  （在识图链里，默认会用它）" if p is provs[0] else "  （在识图链里）"
        else:
            tail = ""
        rows.append({"id": p["id"], "label": p["label"], "model": p["model"]})
        rows[-1]["line"] = f"  {p['label']}（{p['id']}）· {p['model']} —— {state}{tail}"
    return rows, f"这卷共 {total} 页"


def t_list_vision_models(ctx) -> str:
    """工具：列出可用的识图模型，以及每家对这卷的识别/缓存情况。

    换模型重跑之前**必须先调它**：用户要看到"有哪些家、哪些已经读过、哪些要重读"，
    再决定换哪家 —— 不然他以为要花钱重读，其实那家早就读过了（或者反过来）。
    """
    pj, _ = _get_current(ctx)
    if not pj:
        return "还没有正在整理的档案卷，先发照片建卷。"
    rows, summary = _vision_menu(ctx, pj)
    if not rows:
        return ("还没有配置任何识图模型。去界面右上角「模型配置」，"
                "给某一家填上「识图模型」再回来。")
    return ("可用的识图模型（" + summary + "）：\n"
            + "\n".join(r["line"] for r in rows)
            + "\n\n要看哪家读的结果就说“用 XX 的”；要让它读一遍就说“用 XX 重新识别”。")


def t_use_model(ctx, provider: str) -> dict | str:
    """工具：把"当前展示"切换到某一家识图模型读的结果（不重跑、不花钱）。

    切换只改"看哪家"（元数据行里的 active），任何一家的现场都原样留着。
    这家还没读过 → 不切，而是告诉用户"要我读一遍吗"。
    """
    pj, _ = _get_current(ctx)
    if not pj:
        return "还没有正在整理的档案卷，先发照片建卷。"
    from archive.service.interactive import channels, set_active
    chans = {c["channel"] for c in channels(pj)}
    if provider not in chans:
        return (f"「{provider}」还没读过这卷，没有它的结果可看。"
                f"要我读一遍吗？说“用 {provider} 重新识别”就行。")
    set_active(pj, provider)
    out = t_show_overview(ctx)
    out["text"] = f"已切到「{provider}」读的结果（没有重新读，不花钱）。"
    return out


def t_list_state(ctx, keyword: str = "") -> str:
    """工具：列材料清单（模型动手前先看的现场快照）。"""
    _, st = _state(ctx)
    rows = [_fmt_row(m) for m in st["materials"]
            if not keyword or keyword in (m.get("title") or "") or keyword == m.get("uid")]
    issues = st.get("issues") or []
    head = f"共{len(st['materials'])}份材料" + (f"，{len(issues)} 项待核对" if issues else "")
    return head + "\n" + "\n".join(rows or ["（无匹配）"])


def t_show_page(ctx, seq: int) -> dict:
    """工具：展示某页原图+内容卡（page-card 长在对话流里）。"""
    from archive.service.interactive import find_record, _load_records
    pj, _ = _get_current(ctx)
    if not pj:
        raise ValueError("还没有正在整理的档案卷。")
    rec = find_record(_load_records(pj), int(seq))
    if rec is None:
        return {"text": f"页不存在：{seq}"}
    mk = (rec.get("ocr") or {}).get("mark") or {}
    d = mk.get("date") or {}
    return {"text": f"第{seq}页：《{mk.get('t') or '未识别'}》",
            "render": {"type": "page-card", "seq": rec["seq"],
                       "img": _page_url(pj, rec["seq"]),
                       "title": mk.get("t"), "doc": mk.get("doc"),
                       "date": (f"{d.get('y')}-{d.get('m') or '?'}-{d.get('d') or '?'}"
                                if d.get("y") else None),
                       "summary": mk.get("s"), "doubt": bool(mk.get("u"))}}


def _page_url(project_json: str, seq, thumb: bool = False) -> str:
    """拼某页图的 URL（thumb=True 取缩略图）。

    实现已挪到 `archive.station_adapter.page_url`（上站桥），这里保留名字做转发 ——
    conventions 里点名的就是 `_page_url`，而且**编码规则只留一份**：识别进度面板
    也在拼同样的 URL，两边各写一份迟早有一边忘了 quote（那正是 09-10 破图的成因）。
    """
    from archive.station_adapter import page_url
    return page_url(project_json, seq, thumb)


def t_show_overview(ctx) -> dict:
    """工具：展示十类全景卡（只读；替代旧分拣台主视图）。

    两类改动（09-10）：
      1) 类别**一律列全**——没有材料的也列出来，用户才知道"这一类是空的"，
         顺序按 domain/classes.py 的 VALID_SUBS 固定，前后端一致；
      2) 每条材料带**首页缩略图**——用户手上是一叠纸质照片，只告诉他"第 41 张"
         他没法对应到是哪一张，给张小图一眼就认出来了。

    ★ 09-12：类内也排序了（以前按大模型给的顺序，跟 Excel 的目录序不一致）。
      `pages` 字段**保持照片号升序**（人靠"第 N 张"去翻实物），装订序另放 `binder`；
      两者顺序不同是正常的（照片顺序随机），前端按 `pages[i]` 配 `thumbs[i]`。
    """
    from archive.domain.classes import VALID_SUBS, dir_sort_key
    pj, st = _state(ctx)
    mats = st["materials"]
    groups: dict = {}
    for m in mats:
        groups.setdefault(m.get("category") or "?", []).append(m)
    cats = []
    # 固定顺序 = 分类体系顺序；"?"（未归类）只在真有材料时才追加
    order = list(VALID_SUBS) + (["?"] if groups.get("?") else [])
    for cat in order:
        rows = sorted(groups.get(cat) or [], key=dir_sort_key)   # 类内＝目录序
        cats.append({"category": cat, "n": len(rows),
                     "items": [{"uid": m["uid"], "title": m.get("title") or "未命名",
                                "pages": sorted(m.get("members") or []),   # 照片号升序
                                "binder": list(m.get("members") or []),    # 装订序
                                "form": m.get("form") or "",
                                "copies": m.get("copies", 1),
                                "doubt": bool(m.get("doubt")),
                                # 每一页都给一张缩略图：多页材料要让用户看到"这份有两张"
                                "thumbs": [_page_url(pj, s, thumb=True)
                                           for s in sorted(m.get("members") or [])]}
                               for m in rows]})
    issues = st.get("issues") or []
    # 这家读的：多模型并存后必须让用户一眼看出"现在看的是哪家读的"（否则切了模型
    # 还以为结果没变）。前端把它显示在卡片头上。
    from archive.service.interactive import active_channel
    ch = active_channel(pj)
    return {"text": (f"全景：{len(mats)} 份材料，{len(issues)} 项待核对。"
                     "点缩略图可就地放大看原图，点条目标题可预填修改话术。"),
            "render": {"type": "overview", "person": st.get("person") or "",
                       "project": pj,           # 前端拼大图 URL 用（原图不带 thumb 参数）
                       "channel": ch,           # 这份结果哪家读的（"" = 还没识别）
                       "cats": cats, "issues": [{"code": i.get("code"),
                                                 "message": i.get("message")}
                                                for i in issues]}}


def t_show_compare(ctx, seq: int) -> dict:
    """工具：换**另一家**读某页 → 发新旧对比卡。

    ★ 09-12 起并入 reocr（原来是两个工具，同一个动作的两半，且两半都漏）：
      直接调 reocr(seq) 就行，这个函数是它 adopt=false 分支的实现。
    ★ 优先读那一家**已有的 OCR 缓存**（0 花费）；只有没缓存才真调模型 ——
      "换一家对比"不该每次都为看一眼再烧一次钱（见 interactive.op_reocr）。
    """
    from archive.service import interactive as it
    pj, _ = _get_current(ctx)
    cmp_ = it.op_reocr(pj, int(seq))
    o, n = cmp_["old"] or {}, cmp_["new"]
    nm = n.get("mark") or n
    how = "（直接用之前那家读过的缓存，没花钱）" if cmp_.get("cached") else ""
    return {"text": (f"第{seq}页两家 OCR 对比已发{how}。旧：《{o.get('t')}》"
                     f"新（{cmp_['channel']}）：《{nm.get('t')}》。用户满意就说\"采用\"。"),
            "render": {"type": "diff-card", "seq": int(seq), "channel": cmp_["channel"],
                       "old": {"title": o.get("t"), "doc": o.get("doc"),
                               "summary": o.get("s")},
                       "new": {"title": nm.get("t"), "doc": nm.get("doc"),
                               "summary": nm.get("s")}}}


def _out_dir(pj: str) -> str:
    """当前卷**当前展示那家模型**的导出目录：<项目>/out/<channel>/。

    ★ 按模型分目录（09-12）：两家模型各导一次，4 件套文件名一样，不分目录会互相覆盖。
      没识别过（没有 active channel）、或老卷导出在旧的 <项目>/out/ 下时，
      回落到那一层，保证老产物还看得见。
    """
    from archive.service.interactive import active_channel
    ch = active_channel(pj)
    base = os.path.join(os.path.dirname(pj), "out")
    return os.path.join(base, ch) if ch else base


def t_show_files(ctx) -> dict:
    """工具：把当前卷的产物（4件套）以文件卡片展示。

    卡片链接走 station 文件区（/api/files/<id>?dl=1）——产物导出时已收进文件区，
    那条链路带正确 MIME 和下载名，浏览器行为稳定。不要用 /api/archive/file 拼
    project 绝对路径当 URL（Windows 反斜杠+中文会被浏览器改写，曾踩坑）。
    """
    from station.files import store as fs
    pj, st = _state(ctx)
    meta = _prod_meta(ctx, pj, (st or {}).get("person") or "")
    out_dir = _out_dir(pj)
    files = []
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            fp = os.path.join(out_dir, fn)
            if os.path.isfile(fp):
                # save_bytes 是**内容寻址**的（同内容 → 同 id），重复调用不会在
                # "我的产物"里堆重复条目 —— 所以这里照旧存一遍是幂等的。
                fid = fs.save_bytes(open(fp, "rb").read(),
                                    os.path.splitext(fn)[1], name=fn, **meta)
                files.append({"name": fn, "url": f"/api/files/{fid}?dl=1"})
    return {"text": f"共 {len(files)} 个产物文件。" if files else "还没有产物（先说出件）。",
            "render": {"type": "file-card", "files": files,
                       "note": "4件套：目录xlsx / 原件PDF / 待核对清单 / 判定报告"}}


# ══ 修正组 ═══════════════════════════════════════════════════════════

def _changed(text: str, ctx, extra_cards: list | None = None) -> dict:
    """改动类工具的统一收尾：一句话给模型 + **顺手重绘全景卡**给用户。

    ★ 为什么每个修正工具都要发卡（09-12）：原来只有 set_orientation 发样例卡，其余
      只回一句文字 —— 用户说"第41张改成九-2"，界面**毫无变化**，得靠模型记得再调一次
      show_overview 才看得见（而它经常不记得）。改完就让用户看到结果，这一整类
      "改了没反应"的失败就没了。

    extra_cards：可选，再挂 0..n 张别的卡（如"学到的口径，待你确认"）。
    **全景卡永远排第一** —— 09-12 那条修复不能被后来的卡片挤掉。
    """
    out = t_show_overview(ctx)
    out["text"] = text
    if extra_cards:
        out["render"] = [out["render"], *extra_cards]
    return out


def _learn_proposal(before: dict, after: dict, source: str = "ledger") -> dict:
    """算一条口径提案；没什么可学的返回 None（静默）。

    ★ 学习失败绝不能反过来影响"改类"这件正事，所以整段兜住 —— 出不来提案就是没有。
    """
    try:
        from archive.service import learn
        return learn.from_correction(before, after, source)
    except Exception:                            # noqa：学习是加分项，不许拖垮主操作
        return None


def _learn_out(prop: dict, headline: str = "") -> dict:
    """把一条口径提案转成工具的返回值：**人话 + 卡片**。

    ★ 刻意**不**把改完的文件内容塞进来（09-13 改的）：条文由 `learn.propose` 现算，
      真正落盘时再算一遍 —— 模型只负责说"哪一条"，它**不生成文件内容**，
      也就写不歪一行。顺带省掉了每轮几百 token 的大段文本。
    """
    from archive.service import learn
    lines = [i["entry"] for i in prop["items"]]
    txt = headline or f"系统从这次修正里提炼出 {len(lines)} 条口径，等你确认。"
    txt += "\n准备写进去的条文：\n" + "\n".join(f"  {x}" for x in lines)
    for i in prop["items"]:
        if i.get("drop"):
            txt += (f"\n同时把「基础口径」里冲突的那一个文种摘掉：{i['drop']['token']}"
                    f"（原判 {i['drop']['old']}）—— 同行其余文种保留。")
    txt += ("\n\n★ 用户确认后，调 **archive.apply_learning** 写进去（不传参数 = 把这卷"
            "所有还没学的一并写；也可以只传 title + category 写其中一条）。"
            "那是危险操作，宿主会先请用户批准 —— 如实转述即可。")
    return {"text": txt, "render": learn.card(prop)}


def t_set_category(ctx, target: str, category: str, title: str = "") -> dict | str:
    """工具：改某份的类别（target=uid 或标题关键词；category=小类码）。"""
    from archive.domain.classes import normalize_category
    if normalize_category(category) is None:
        return f"类别码不合法：{category}（十类，四/九带小类如 四-1、九-2）。"
    pj, st = _state(ctx)
    m = _by_uid(st, target) or _by_mention(st, target)
    if m is None:
        return f"找不到材料：{target}。先调 list_state 看清单。"
    # 动手前先把原值抓下来当"AI 原判"。★ 这是安全的：op_set_category 内部会**再
    # load_state 一次**（拿到的是另一份 dict），所以我们手里这个 m 不会被它改到。
    before = {"title": m.get("title"), "category": m.get("category")}
    try:
        from archive.service.interactive import op_set_category
        st2 = op_set_category(pj, m["uid"], category, title or None, _owner(ctx))
    except ValueError as e:
        return f"改不了：{_err(e)}"
    mm = next(x for x in st2["materials"] if x["uid"] == m["uid"])
    said = (f"已改：{m['uid']}《{mm.get('title')}》→ {mm['category']}"
            f"（原 {m.get('category')}）。")
    prop = _learn_proposal(before, {"category": mm["category"],
                                    "title": mm.get("title")})
    if prop:
        out = _learn_out(prop, said + " 顺带学一条口径（需用户确认才写入）：")
        out["render"] = [t_show_overview(ctx)["render"], out["render"]]
        return out
    return _changed(said, ctx)


def t_merge(ctx, target_a: str, target_b: str) -> dict | str:
    """工具：两份并一份（同一份大表被拆散/重复条目）。"""
    pj, st = _state(ctx)
    # _by_mention 一并接上：用户会说"第41张和42张并一起"，只有 _by_uid 会直接失效
    a = _by_uid(st, target_a) or _by_mention(st, target_a)
    b = _by_uid(st, target_b) or _by_mention(st, target_b)
    if a is None or b is None:
        return f"找不到材料：{target_a if a is None else target_b}。先调 list_state。"
    try:
        from archive.service.interactive import op_merge
        op_merge(pj, a["uid"], b["uid"])
    except ValueError as e:
        return f"并不了：{_err(e)}"
    return _changed(f"已合并 {a['uid']}+{b['uid']} → 保留《{a.get('title')}》，页取并集。",
                    ctx)


def t_split(ctx, target: str, seqs: str) -> dict | str:
    """工具：把某页/几页从一份里拆出来（seqs 如 "41" 或 "41,42"）。"""
    pj, st = _state(ctx)
    m = _by_uid(st, target) or _by_mention(st, target)   # 也认"第41张"这种说法
    if m is None:
        return f"找不到材料：{target}。先调 list_state。"
    try:
        seq_list = [int(s.strip()) for s in str(seqs).replace("，", ",").split(",") if s.strip()]
        from archive.service.interactive import op_split
        st2 = op_split(pj, m["uid"], seq_list)
    except ValueError as e:
        return f"拆不了：{_err(e)}"
    new_uid = max((x["uid"] for x in st2["materials"]), key=lambda u: int(u[1:]))
    return _changed(f"已拆：新份 {new_uid} 含第{seqs}张；剩余在 {m['uid']}"
                    f"（两份都标了存疑，请接着核对）。", ctx)


def t_reocr(ctx, seq: int, adopt: bool = False) -> dict | str:
    """工具：换另一家读某页 —— adopt=false 发对比卡，true 采用新读法落盘。

    ★ 09-12 由 show_compare + reocr 两个工具合成一个（同一动作的两半，还各漏一处）：
      · adopt=false 原来只 return out["text"]，**把 diff-card 丢了** → 用户看不到对比卡；
        现在整个 dict 返回，卡真的渲染出来。
      · adopt=true 原来**又重调了一次模型**（`op_reocr` 不走缓存）→ 落盘的未必是用户
        刚看到的那张卡。现在改成从**那家的缓存**里取回同一张（reocr_page 早把它存下了），
        用户看到的和采用的是同一份。
    """
    from archive.service import interactive as it
    from archive.service.interactive import find_record, _load_records
    pj, _ = _get_current(ctx)
    if not adopt:
        return t_show_compare(ctx, int(seq))     # 第一步：发对比卡（dict，带 render）
    rec = find_record(_load_records(pj), int(seq))
    if rec is None:
        return f"页不存在：{seq}"
    cmp_ = it.op_reocr(pj, int(seq))             # 命中缓存就直接拿回上次那张
    st = it.op_apply_reocr(pj, int(seq), cmp_["new"])
    row = next((m for m in st.get("materials", [])
                if m["members"] and m["members"][0] == int(seq)), None)
    nm = cmp_["new"].get("mark") or cmp_["new"]
    return _changed(f"已采用 {cmp_['channel']} 的读法：第{seq}页"
                    f"《{nm.get('t') or cmp_['new'].get('title')}》"
                    f"{'（所在材料行已刷新）' if row else ''}。", ctx)


def t_checkpoints(ctx, ck_id: int = 0) -> dict | str:
    """工具：看/退回这卷的检查点 —— 不给编号就列出，给了编号就退回去。

    ★ 09-12 由 list_checkpoints + restore_checkpoint 合成一个：回退要 ck_id，而它是
      DB 全局自增号（不是 1..n），模型猜不出来 —— 原来只能靠提示词叮嘱"先调 list 再
      restore，别自己编编号"（09-11 就在 system.md 里补过这句）。合成一个工具后
      "不给编号就是列表"，那条两步制的坑从设计上消失了。
    ★ 服务端还会校验"这个编号属于本卷"（见 restore_checkpoint）：编号是全局的，
      光靠提示词拦不住"编一个号、结果退回了别的卷"。
    """
    pj, _ = _get_current(ctx)
    if not pj:
        return "还没有正在整理的档案卷，先发照片建卷。"
    from archive.service.interactive import list_checkpoints, restore_checkpoint
    if not ck_id:
        cks = list_checkpoints(pj)
        if not cks:
            return "这卷还没有可回退的点 —— 改过材料（改类/并/拆/换OCR）之后才会有。"
        lines = [f"{c['id']}. {c['label'] or '（无说明）'}"
                 f"　{time.strftime('%m-%d %H:%M', time.localtime(c['created']))}"
                 for c in cks]
        return (f"这卷有 {len(cks)} 个可回退的点（新→旧）：\n" + "\n".join(lines)
                + "\n要退回哪一个，把前面的编号给我。")
    try:
        st = restore_checkpoint(pj, int(ck_id))
    except ValueError as e:
        return f"恢复不了：{_err(e)}"
    out = t_show_overview(ctx)                   # 退完发张全景卡，用户立刻看到回到哪一步
    out["text"] = f"已恢复到检查点 {ck_id}。现共 {len(st['materials'])} 份材料。"
    return out


# ══ 出件 / 反哺 ══════════════════════════════════════════════════════

def t_export(ctx) -> dict:
    """工具（risk=approve）：按当前现场导出 4 件套 → 发文件卡片。

    危险操作：agent 循环会先挂起问用户（in-band 批准），用户说"允许"才真跑。
    这里执行时现场一定取 DB 最新态（interactive.load_state）——出件曾因读
    job.result 原始态出过错（09-06 修复，见 station_adapter.export_archive）。
    返回 dict（text 给模型 + render 文件卡片给前端）——渲染契约见文件头。
    """
    from archive.station_adapter import export_archive
    from station.core import heavy
    pj, person = _get_current(ctx)
    if not pj:
        return {"text": "还没有正在整理的档案卷。"}
    # ★ 重活闸（见 station/core/heavy.py）：出件是全站最吃内存的一步（实测 120 页峰值
    #   1.4G）。识别任务正在跑时再出一件，两件是**相加**，小机器直接被内核杀掉 ——
    #   表现是"服务突然没了"，没有任何报错，最难查的那种故障。
    #   这里只等一小会儿（CHAT_WAIT）：对话是同步请求，让用户浏览器干等几分钟不合适；
    #   等不到就**把话说清楚**让他过会儿再来（与 t_recognize 用后台 job 绕开
    #   "工具调用干等"是同一个取舍）。
    with heavy.heavy_slot("档案出件", wait=heavy.CHAT_WAIT) as got:
        if not got:
            return {"text": (f"现在有别的重活在跑（{heavy.busy_with()}），"
                             f"两件一起跑会把服务器内存撑爆。等它跑完再让我出件。")}
        files = export_archive({"project": pj, "person": person,
                                **_prod_meta(ctx, pj, person)})
    if not files:
        return {"text": "导出完成但没有产物文件（检查照片/识别结果）。"}
    listing = t_show_files(ctx)
    return {"text": (f"已出件：{'、'.join(n for _, n in files)}。" + listing["text"]),
            "render": listing["render"]}


def t_reconcile(ctx, path: str = "") -> dict:
    """工具：对账"用户改过的终版目录"（不给路径就先发上传卡片）。

    两步制（与 reocr 同款）：先发卡让用户传文件，服务器把路径回给模型；
    模型再调一次带 path 的，才真做逐行比对。
    """
    pj, _person = _get_current(ctx)
    if not pj:
        return {"text": "还没有正在整理的档案卷。"}
    if not path:
        return {"text": "发了一张「上传改后的目录」卡片给用户，等他传完我会拿到路径。",
                "render": {"type": "upload-card", "purpose": "xlsx", "project": pj}}
    from archive.service import learn
    try:
        res = learn.reconcile(pj, path, _owner(ctx))
    except ValueError as e:
        return f"对不了账：{_err(e)}"
    d = res["diffs"]
    head = (f"对账完成：{res['note']}"
            f"（类别不同 {len(d['cat_changed'])}、名称不同 {len(d['renamed'])}、"
            f"你的版本少 {len(d['missing'])} 份、多 {len(d['added'])} 份）")
    if res["proposal"]:
        return _learn_out(res["proposal"], head + " 已按类别差异算出待确认的口径：")
    return {"text": head + " 没有产生新的口径提案。"}


def _learning_changes(ctx, title: str, category: str) -> list:
    """定出"这次要写哪几条口径"：给全了就是那一条，没给就是这卷所有还没学的。"""
    pj, _ = _get_current(ctx)
    if not pj:
        raise ValueError("还没有正在整理的档案卷。")
    from archive.service import learn
    t_, c_ = title.strip(), category.strip()
    if bool(t_) != bool(c_):
        # ★ 只给一个 = 这次调用本身就不明确。**绝不能退化成"把这卷所有没学的都写进去"**
        #   —— 那等于把一次被批准的小改动偷偷放大成一批（review 抓到）。说清楚、让他重来。
        raise ValueError("title 和 category 要么都给（只写这一条）、"
                         "要么都不给（写这卷所有还没学的）；只给一个没法确定写哪条。")
    if t_ and c_:
        return [{"title": t_, "category": c_, "old": ""}]
    return learn.pending_changes(pj)


def _preview_apply_learning(ctx, title: str = "", category: str = "") -> str:
    """**挂起问用户之前**，先算出来给他看："这一步到底要写什么"。

    ★ 为什么非要这段：这个工具的参数只有"哪一条"，而**真正要写进文件的那条条文是它现算的**
      —— 光看参数用户根本不知道自己在批准什么（用户原话："不然用户都不知道要写入什么内容"）。
    ★ 宿主算不了这个（它不认识业务），所以由工具自己算 —— 见 `core/tool.Tool.preview`。
    ★ 只读不写：这里绝不能改任何东西（用户还没点"允许"）。
    """
    from archive.service import learn
    changes = _learning_changes(ctx, title, category)
    if not changes:
        return "没有要写的口径（这卷的修正都已经学过了）。"
    prop = learn.propose(changes)
    # 条文自己就带 `- ` 前缀（render_entry 出的），别再叠一个项目符号
    out = "准备写进《文种对照》：\n" + "\n".join(i["entry"] for i in prop["items"])
    drops = [i["drop"]["token"] for i in prop["items"] if i.get("drop")]
    if drops:
        out += "\n同时从「基础口径」里摘掉：" + "、".join(drops) + "（同行其余文种保留）"
    return out + "\n写入后，以后每一卷的识别都按它判。"


def _preview_export(ctx) -> str:
    """**挂起问用户之前**，说清要导出什么、导的是哪份现场（出件也是不可逆的外部动作）。"""
    pj, person = _get_current(ctx)
    if not pj:
        return "还没有正在整理的档案卷。"
    from archive.service import interactive as it
    st = it.load_state(pj) or {}
    n = len(st.get("materials") or [])
    return (f"把当前现场导出成 4 件套（共 {n} 份材料）：\n"
            f"· {person or '（未命名）'}-人事档案目录.xlsx\n"
            "· 原件 PDF（按目录序）\n· 待核对清单\n· 自动判定报告\n"
            "导的是**当前展示那一家**识图模型读的那份现场。")


def t_apply_learning(ctx, title: str = "", category: str = "") -> str:
    """工具（risk=approve）：把用户确认过的口径写进《文种对照》（下次识别就按它判）。

    ★ 只收"哪一条"（title + category），**不收内容、更不收路径**：条文由 archive
      自己现算、写到固定那份文件。模型既写不歪一行，也没办法写到别处去。
    ★ 不传参数 = 把这卷**所有还没学**的修正一并写进去（对账一次改好几条的场景）。
    ★ `risk="approve"` —— 宿主会先问用户，**用户不点"允许"就写不动**；
      问之前先给他看 `_preview_apply_learning` 算出的"要写什么"。
    """
    from archive.service import learn
    changes = _learning_changes(ctx, title, category)
    if not changes:
        return "没有要写的口径（这卷的修正都已经学过了）。"
    try:
        res = learn.apply(changes)
    except ValueError as e:
        return f"写不进去：{e}"
    out = "已写进《文种对照》，下次识别就按它判：\n" + \
          "\n".join(f"- {x}" for x in res["written"])
    if res["dropped"]:
        out += "\n同时摘掉了「基础口径」里冲突的文种：" + "、".join(res["dropped"])
    return out


def t_list_learning(ctx) -> dict:
    """工具：列出这卷**还没学进口径**的那些修正（用户问"学什么了/还有哪些口径建议"时用）。

    ★ 它只**列**（状态直接从账本 + 口径文件现算，不落任何库），一个字节都写不进去 ——
      真要写必须走 `apply_learning` 那道批准闸。
    """
    from archive.service import learn
    pj, _ = _get_current(ctx)
    prop = learn.pending_from_ledger(pj)
    if not prop:
        return {"text": "这卷的修正都已经学进口径了，没有待办。",
                "render": learn.card({}, "改一次材料类别、或出件后在 Excel 里改类再传回来，"
                                         "这里就会出现待确认的提案。")}
    return _learn_out(prop, f"这卷还有 {len(prop['items'])} 条修正没学进口径：")


# ══ 工具清单（registry 约定：tools() → 叶子名，装载时补成 archive.*）════

def t_set_orientation(ctx, rotate: int = 90, only_landscape: bool = True) -> dict:
    """工具：把当前卷的照片转正（rotate=90/180/270，正值=逆时针；0=恢复不转）。

    为什么需要它：翻拍设备常常【不写 EXIF 方向】，照片像素横躺 —— 浏览器、PDF
    都无从判断该往哪转（实测拿视觉模型问也判不准），只能由人确认一次，把角度记进
    photos.json 每页的 rotate 字段，之后展示与导出都照它转。

    only_landscape=True（默认）只动"按 EXIF 摆正后仍是横版"的页：档案纸是竖的，
    横版多半是拍摄方向问题；已经是竖版的页保持不动，免得把好页转歪。

    转完顺手发一张样例页卡 —— 方向对不对，用户一眼就知道。
    """
    pj, _ = _get_current(ctx)
    if not pj:
        raise ValueError("还没有正在整理的档案卷。")
    from archive.engine.orient import is_landscape   # "横躺"判据与自动探测共用一份
    from archive.storage.project import load_project, save_records
    _, records = load_project(pj)
    deg = int(rotate) % 360
    hit, skipped = 0, 0
    for r in records:
        if only_landscape and is_landscape(r["path"]) is not True:
            skipped += 1              # 摆正后已是竖版 / 读不动 → 不碰（别把好页转歪）
            continue
        r["rotate"] = deg
        r["rotate_src"] = "manual"    # ★ 标"人工定的"：自动探测以后不能再覆盖它
        hit += 1
    save_records(pj, records)
    out = {"text": (f"已把 {hit} 页设为旋转 {deg}°"
                    + (f"，另有 {skipped} 页本来就是竖版、未动" if skipped else "")
                    + "。请看下面的样例，方向不对就换个角度再来一次。")}
    sample = next((r["seq"] for r in records if r.get("rotate")), None)
    if sample is not None:
        out["render"] = t_show_page(ctx, int(sample))["render"]
    return out


def tools() -> list[Tool]:
    """本技能的工具面。description 写给模型看：什么时候调、参数怎么填。"""
    return [
        # —— 输入 ——
        Tool(name="ask_photos", label="发照片上传卡片", description="发一个照片上传卡片给用户（用户在自己电脑选整卷翻拍照片）。",
             run=t_ask_photos, args=[]),
        Tool(name="scan_photos", label="建卷入库",
             description="把一批照片建成档案卷（复制入库+编号）。dir=照片目录（上传卡片回填的服务器目录或本机目录）；person=干部姓名（消息里带「姓名：X」就原样传；带「未填姓名」就先问用户，拿到答案再调——person 为空本工具不会建卷）。",
             run=t_scan_photos,
             args=[{"name": "dir", "type": "str", "desc": "照片目录"},
                   {"name": "person", "type": "str", "desc": "干部姓名（可省）", "required": False}]),
        # —— 识别 ——
        Tool(name="recognize", label="整卷识别",
             description="开始整卷识别（后台任务，几分钟）。**默认不重跑**：这卷已经识别过就直接告诉你。provider=换一家识图模型读（用之前先调 list_vision_models 报菜单）；refresh=true 才是忽略缓存重读一遍（只在这两处用户明确要求时传）。",
             run=t_recognize,
             args=[{"name": "provider", "type": "str",
                    "desc": "识图模型 id（如 glm/qwen）；不传=用识图链", "required": False},
                   {"name": "refresh", "type": "bool",
                    "desc": "true=忽略缓存重读一遍", "required": False}]),
        Tool(name="job_status", label="查识别进度", description="查识别任务进度（用户问\"跑到哪了\"时用）。",
             run=t_job_status,
             args=[{"name": "job_id", "type": "str", "desc": "任务号（可省，默认最近一次）", "required": False}]),
        Tool(name="list_vision_models", label="查看有哪些识图模型",
             description="列出可用的识图模型，以及每家对这卷的识别/缓存情况（哪些已识别过、哪些没读过）。用户说\"换个模型重新识别/还有别的模型吗/换一家看看\"时**先调它**报菜单，让他挑。",
             run=t_list_vision_models, args=[]),
        Tool(name="use_model", label="切换看另一家的结果",
             description="切换到某一家识图模型读的结果来展示（不重跑、不花钱），并重绘全景卡。用户说\"看 XX 读的/换 XX 那家看看\"时用；这家还没读过就别调它，改说\"要不要用它读一遍\"。",
             run=t_use_model,
             args=[{"name": "provider", "type": "str", "desc": "识图模型 id（list_vision_models 里的）"}]),
        # —— 现场（展示）——
        Tool(name="list_state", label="查看材料清单", description="列出当前卷的材料清单（uid/类别/标题/页号/存疑）。改任何东西前先调它看清现场。",
             run=t_list_state,
             args=[{"name": "keyword", "type": "str", "desc": "可选，按标题过滤", "required": False}]),
        Tool(name="show_page", label="看某一页", description="展示某页原图和识别的内容卡（标题/文号/日期/要点）。用户问某页/核对某页时用。",
             run=t_show_page,
             args=[{"name": "seq", "type": "int", "desc": "页号（第几张）"}]),
        Tool(name="show_overview", label="看十类全景", description="展示整卷十类全景卡（只读，用户点条目可预填修改话术）。识别完成或用户说\"看全景/看看现在什么样\"时用。",
             run=t_show_overview, args=[]),
        Tool(name="show_files", label="看产出的 4 件套", description="展示当前卷的产物文件卡片（4件套下载）。出件后或用户要文件时用。",
             run=t_show_files, args=[]),
        Tool(name="set_orientation", label="把照片转正",
             description="把照片转正（rotate=90/180/270，正值=逆时针；0=恢复不转）。用户说\"照片歪了/方向不对，转90度\"时用。默认只动横躺的页，竖版页不动。",
             run=t_set_orientation,
             args=[{"name": "rotate", "type": "int", "desc": "旋转角度 0/90/180/270（正值=逆时针）"},
                   {"name": "only_landscape", "type": "bool", "desc": "只动横躺的页", "required": False}]),
        # —— 修正 ——
        Tool(name="set_category", label="改材料类别", description="把某份材料改成指定类别（target=uid或标题关键词，category=小类码如 九-2）。可顺带改名称。",
             run=t_set_category,
             args=[{"name": "target", "type": "str", "desc": "材料 uid 或标题关键词"},
                   {"name": "category", "type": "str", "desc": "新类别小类码"},
                   {"name": "title", "type": "str", "desc": "顺带改的材料名称（可省）", "required": False}]),
        Tool(name="merge", label="合并两份材料", description="把两份材料合并成一份（同一份大表被拆散/重复条目）。",
             run=t_merge,
             args=[{"name": "target_a", "type": "str", "desc": "第一份 uid 或标题关键词"},
                   {"name": "target_b", "type": "str", "desc": "第二份 uid 或标题关键词"}]),
        Tool(name="split", label="拆出一页", description="把某页从一份材料里拆出来单独成份。",
             run=t_split,
             args=[{"name": "target", "type": "str", "desc": "材料 uid 或标题关键词"},
                   {"name": "seqs", "type": "str", "desc": "要拆出的页号，如 41 或 41,42"}]),
        Tool(name="reocr", label="换一家重读这页",
             description="换另一家读某一页：**不给 adopt** 就发新旧对比卡（用户说\"这页读得不对/换一家读读看\"时用，优先用那家已有的缓存，不花钱）；用户看完对比说\"采用\"时，**同一页再调一次并传 adopt=true** 落盘。",
             run=t_reocr,
             args=[{"name": "seq", "type": "int", "desc": "页号"},
                   {"name": "adopt", "type": "bool", "desc": "true=采用上次对比的新读法落盘", "required": False}]),
        Tool(name="checkpoints", label="查看/退回上一步",
             description="看/退回检查点（每次改动材料前都自动存一个）。**不传编号就是把可回退的点列出来**；用户说\"退回上一步/刚改错了\"，先列出来、拿最近的编号再调一次传进去。",
             run=t_checkpoints,
             args=[{"name": "ck_id", "type": "int", "desc": "要退回的检查点编号（不传=列出）", "required": False}]),
        # —— 出件 / 反哺 ——
        Tool(name="export", label="导出 4 件套", description="按当前现场导出4件套（目录xlsx/原件PDF/待核对清单/判定报告）。危险操作：系统会先向用户请求批准。",
             run=t_export, risk="approve", args=[],
             preview=_preview_export),      # 挂起前说清导什么、导哪份现场
        Tool(name="reconcile", label="对账改过的终版目录",
             description=("对账用户改过的终版《人事档案目录》xlsx，看和他改的有什么不一样，"
                          "并把类别差异提炼成待确认的口径提案。**不给 path 就先发上传卡片**；"
                          "用户传完会给你文件路径，再调一次带 path 的。"
                          "用户说出件后自己在 Excel 里改了类/要传回来核对时用。"),
             run=t_reconcile,
             args=[{"name": "path", "type": "str", "desc": "用户改过的 xlsx 路径",
                    "required": False}]),
        Tool(name="apply_learning", label="把口径写进去",
             description=("把用户确认过的识图口径写进《文种对照》（下次识别就按它判）。"
                          "**危险操作：系统会先向用户请求批准。** "
                          "只在用户点了卡片上的「确认，写进去」、或明确说「就按这条改」之后才调。"
                          "不传参数 = 把这卷所有还没学的修正一并写进去。"),
             run=t_apply_learning,
             args=[{"name": "title", "type": "str", "desc": "只写这一条时的文种名（可选，与 category 一起给）", "required": False},
                   {"name": "category", "type": "str", "desc": "只写这一条时的类别码，如 九-2（可选）", "required": False}],
             # ★ 必须 approve：它是**唯一能写口径文件**的工具，而口径文件决定
             #   以后每一卷的分类。少了这一行它就变成 auto 类 = 模型可以自己偷偷改口径，
             #   整个"必须用户确认"的设计当场失效（09-13 真浏览器实测踩到过，
             #   补了 `test_learning_tools_have_the_right_risk` 钉住）。
             # ★ preview：批准卡上得写清**要写进去什么内容** —— 参数里只有"哪一条"，
             #   条文是现算的，不预览用户根本不知道在批准什么。
             risk="approve", preview=_preview_apply_learning),
        Tool(name="list_learning", label="看有哪些口径建议",
             description=("列出待用户确认的识图口径学习提案。用户问\"学什么了/有哪些口径建议/"
                          "上次那条口径呢\"时用。★ 你只能列，**不能替用户确认** —— 确认必须"
                          "由用户在卡片上点击完成。"),
             run=t_list_learning, args=[]),
    ]


# ══ 执行器入口 ═══════════════════════════════════════════════════════

def build_runner(ctx, **args):
    """本技能的后台执行器入口（宿主 JobManager 用，**不是**给模型调的工具）。

    新手视角：一个技能为什么有两个入口？
      - tools() 是**对话面**：模型能"点名"调用的那些动作（识别/改类/出件…）。
      - build_runner() 是**执行器入口**：宿主拿去当后台任务跑的东西。
      识别是**长活**（百余页 ≈ 90 秒），不能让一次工具调用干等 —— 所以 t_recognize
      只负责起 job，真正跑识别的是这个入口。宿主 Registry 扫 entry 模块时看到谁就挂谁
      （见 src/station/skills/registry.py 第三步），于是"一次识别" = 起一个后台 job。

    真正的实现在 archive.station_adapter.build_runner（识别链 + 落库都在 archive 包里，
    本技能只是把它挂出来）；这里**只做转发**：一个技能 = 一个入口模块，宿主只认这一个
    entry，不用知道 archive 包里的文件怎么分层。

    ★ 它自己不写 yield —— 是**普通函数**，只不过"返回的东西"是个生成器。所以：
        调用 build_runner(...)  → 转发函数立刻跑完（import + 调用内层），
        返回的生成器            → 内层函数体一行都还没跑；
        宿主 for 取第一次值时   → 内层 build_runner 才真正开始执行。
      （宿主那边等价于还是拿到一个生成器，惰性启动的性质原样保留。）

    函数内 import：和本文件其它地方一样懒加载 —— 装载技能时只 import 这个模块，
    不会顺手把整个 archive 包（连带 langchain/langgraph）拉起来。
    """
    from archive.station_adapter import build_runner as _run
    return _run(ctx, **args)
