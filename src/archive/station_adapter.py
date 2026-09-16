"""archive → station 的识别执行器（后台识别 job 的入口）。

把“建档→切份→分批定类→二次合并”识别链（LangGraph）包装成后台 job：
识别过程按节点吐 progress；跑完把现场（materials/issues）落 DB
（interactive.save_state，键=project 路径）——**没有确认门了**（09-07 对话式
改造）：job 自然 done，之后一切核对/修正/出件由对话里的 archive.* 工具完成
（见 skills/archive/code/archive_tools.py；出件走 export_archive()）。

输入（build_runner(ctx, **args)）：
  project    已建好的 project.json 路径（对话流程由 scan_photos 建卷后传它）
  photos_dir 原始翻拍图目录（旧直连方式保留；与 project 二选一）
  person      干部名（默认取项目名/目录名）
  limit       只跑前 N 张（0=全部，联调用）
  order       create_project 排序策略（拍摄时间|文件名|修改时间）

注意：本模块顶层只 import 标准库，archive/engine/export 等全部函数内懒加载——
让 station 注册表扫到它时零副作用，也避免宿主被 archive 依赖污染。

新手视角：这里是“档案识别链”的后台执行器。宿主 JobManager 驱动它：拿输入
→ storage 建项目/读记录 → 造模型 → 跑 LangGraph（graph.stream 逐节点吐进度）
→ 现场落 DB → 结束。“识别怎么干”全在 archive 包（engine/graph.py 5 节点）；
“识别完之后怎么交互”全在对话工具层（skills/archive）。
"""
from __future__ import annotations

import os


def _percent(node: str) -> int:
    # LangGraph 节点 → 任务进度（近似）
    # ★ 节点名要与 graph.build_graph() 里注册的一致：09-12 把 consolidate（二次合并）
    #   换成了 resolve（残页裁决），这里忘了同步的话前端就永远看不到那一段的进度。
    return {"mark": 18, "segment": 26, "resolve": 34, "classify": 60,
            "build_mats": 80}.get(node, 0)


# LangGraph 节点 → 给人看的进度文案（前端直接显示；别让 mark/segment 这类英文漏给用户）
_NODE_LABELS = {
    "mark": "正在逐页读取内容", "segment": "正在按册归组",
    "resolve": "正在安置认不出归属的页",
    "classify": "正在判定每份的类别", "build_mats": "正在整理材料清单",
}


def page_url(project_json: str, seq, thumb: bool = False) -> str:
    """拼"某页图"的 URL（识别进度面板、页卡、全景卡共用这一份实现）。

    ★ project 参数**必须 quote 编码**：它是含中文和反斜杠的绝对路径，直接塞进 URL
      会被浏览器/代理改写 → 表现为**部分缩略图破图**（09-10 踩过，服务端日志还全是
      200，极难查）。编码规则只留这一处，别再各处手拼。
    ★ thumb=True 走缩略图分支（服务端 `Image.draft()` 跳采样 + `data/station/thumbs/`
      磁盘缓存，实测生成 21ms → 命中 5ms）。**识别进度面板必须用它** —— 一展开就是
      上百张，拿原图（每张几 MB）会把界面拖死。
    """
    from urllib.parse import quote
    q = quote(str(project_json), safe="")
    return f"/api/archive/page/{seq}?project={q}" + ("&thumb=1" if thumb else "")


def _iter_flow(records, llm_text, llm_vision, vision_channel="", sink=None,
               refresh: bool = False):
    """跑完整识别链；逐条 yield **事件 dict**，最终图状态回填进 sink。

    产出两种事件（都是 dict，宿主的 job 直接收）：
      {"type":"progress", "percent":18, "message":"正在逐页读取内容"}
      {"type":"page", "seq":37, "total":126, "title":…, "cached":false}   ← 每页一条

    两个设计点：
      1) **为什么交回最终状态要用 sink 参数**：生成器 `return x` 的值会被 for 循环
         丢掉（它只藏在 StopIteration.value 里），调用方拿不到 → 所以让调用方传一个
         可变 dict 进来，跑完 update 进去（build_runner 传的就是 last_merged）。
      2) **stream_mode 必须带 "custom"**：node_mark 里 `get_stream_writer()` 推的页级
         事件，只有 mode 含 custom 时才会被送出来；**不含则静默丢弃、不报错**（驱动方
         漏写就变成"看起来什么都没发生"）。updates 是原有的"每跑完一个节点给我一次
         状态更新"。两种混在一起出来，形如 (mode, payload)。
    """
    from archive.engine.graph import build_graph
    g = build_graph()
    state = {"records": records, "llm_text": llm_text, "llm_vision": llm_vision,
             # 建档走的是哪条链 —— node_mark 拿它当 OCR 缓存的键之一
             "vision_channel": vision_channel,
             # 用户明确要求"重新识别一遍"时置真：node_mark 跳过缓存、真读一遍
             "refresh": bool(refresh)}
    merged: dict = {}
    try:
        for mode, payload in g.stream(state, stream_mode=["updates", "custom"]):
            if mode == "custom":                 # 节点内推的页级事件 → 原样转出去
                if isinstance(payload, dict) and payload.get("type") == "page":
                    yield payload
                continue
            # —— 以下 = 原来的"节点更新"分支（payload 形如 {节点名: 状态更新}）——
            for node, upd in (payload or {}).items():
                if isinstance(upd, dict):
                    for k, v in upd.items():
                        if v is not None:
                            merged[k] = v
                if node in _NODE_LABELS:
                    yield {"type": "progress", "percent": _percent(node),
                           "message": _NODE_LABELS[node]}
    except Exception as e:                       # noqa
        raise RuntimeError(f"识别链失败: {type(e).__name__}: {e}") from e
    if sink is not None:
        sink.update(merged)                      # 最终状态（materials/issues/…）交回调用方
    yield {"type": "progress", "percent": 95, "message": "识别完成"}


def _export_all(records, materials, issues, person, out_dir):
    """导出 4 件套到 out_dir，返回生成的文件绝对路径列表。

    单独拎出来：正常流程（确认后）和旧式一键流程共用同一段导出逻辑。
    """
    from archive.export import excel, original_pdf, reports
    paths = [
        excel.export(materials, person, str(out_dir)),
        original_pdf.export_pdf(records, materials, person, str(out_dir)),
        *reports.export_reports(records, materials, issues, [],
                                str(out_dir), person),
    ]
    return [p for p in paths if p and os.path.isfile(p)]


def _save_files(paths, meta=None):
    """把导出的文件收进 station 文件区，返回 [(文件id, 文件名)]。

    meta 可选携带来源信息（owner/skill_id/group_key/group_label），
    前端“我的产物”抽屉按它分组；没给也不拦（脚本/旧调用）。
    """
    from station.files import store as fs
    meta = meta or {}
    out = []
    for pth in paths:
        data = open(pth, "rb").read()
        fid = fs.save_bytes(data, os.path.splitext(pth)[1],
                            name=os.path.basename(pth),
                            owner=meta.get("owner", ""),
                            skill_id=meta.get("skill_id", ""),
                            group_key=meta.get("group_key", ""),
                            group_label=meta.get("group_label", ""))
        out.append((fid, os.path.basename(pth)))
    return out


def export_archive(result: dict):
    """确认门之后的一步：导出 4 件套（用**用户修正后**的最新现场）。

    现场来源优先级：SQLite archive_states（分拣台上每改一次就落库的最新态）
    > result 里带的原始 materials/issues（用户没进过分拣台时的兜底）。
    ——这里曾有个真 bug（09-06 发现）：早先只读 result，用户在分拣台改了
    半天，确认出件的却是改之前的原始识别结果。修复 = 永远以 DB 现场为准。
    返回 [(文件id, 文件名)]（station 文件区引用，前端直接渲染下载/预览）。
    """
    from archive.storage.project import load_project
    from archive.service.interactive import active_channel, load_state
    proj = result["project"]
    # DB 现场优先（它才是"用户看过并改过"的那份）—— 不指定 channel 就是"当前展示的那家"
    st = load_state(proj)
    materials = st["materials"] if st else (result.get("materials") or [])
    issues = st["issues"] if st else (result.get("issues") or [])
    person = (st or {}).get("person") or result.get("person") or "干部"
    _, records = load_project(proj)   # 页记录（PDF 导出要按页出图）
    # ★ 导出目录按模型分：out/<channel>/ —— 两家模型各导一次，4 件套不该互相覆盖
    #   （文件名一样：目录.xlsx / 原件.pdf…）。没识别过（channel 为空）时仍用 out/。
    ch = active_channel(proj)
    out_dir = os.path.join(os.path.dirname(proj), "out", ch) if ch \
        else os.path.join(os.path.dirname(proj), "out")
    os.makedirs(out_dir, exist_ok=True)
    paths = _export_all(records, materials, issues, person, out_dir)
    prod_meta = {k: result.get(k, "") for k in
                 ("owner", "skill_id", "group_key", "group_label")}
    return _save_files(paths, prod_meta)


def _job_retry_notifier(ctx):
    """给识别链造一个"正在重连"的显示回调（模型层退避时叫它）。

    为什么落在这里：`core/model.py` 与 `archive/engine/*` **都不认识 job**（那是宿主
    的东西），engine 只负责"叫一声"，由**上站桥**（本文件）把它接到宿主的进度条上。
    engine 侧只收一个普通函数，不 import 任何宿主模块 —— 分层不破。

    返回 None 表示"没人听"（CLI/离线跑没有 job_id），engine 那边会安静跳过。
    """
    jid = str(getattr(ctx, "job_id", "") or "")
    if not jid:
        return None
    from station.jobs.manager import get_manager        # 函数内 import：顶层只留标准库

    def note(ev: dict) -> None:
        # ★ 刻意**不传 percent**：manager.Job.append 的写法是
        #   `progress = ev.get("percent", 当前值)` —— 不带这个键就保持原进度、
        #   只刷新文案。传了会把进度条冲回 0，用户看着像倒退了。
        # ★ Job.append 自带锁，从识别链的并发 worker 线程里调是安全的。
        job = get_manager().get(jid)
        if job is not None:
            job.append({
                "type": "progress",
                "message": (f"模型连接失败，{float(ev.get('wait') or 0):g} 秒后重试"
                            f"（第 {ev.get('attempt')}/{ev.get('total')} 次）"),
            })

    return note


def build_runner(ctx, photos_dir: str = "", project: str = "", person: str = "",
                 limit: int = 0, order: str = "拍摄时间",
                 provider: str = "", refresh: bool = False):
    """识别 job 的入口**生成器** —— 宿主一驱动它，就把一整卷识别完并落库。

    新手视角（Java 朋友版）：把它想成"一个能分段汇报进度的长任务"，不是普通方法。
      1) **它有 yield，所以是生成器**：写 build_runner(...) 只是造出一个生成器对象，
         函数体**一行都不会跑**；真正的执行从宿主第一次 for 取值才开始（这就是为什么
         在 t_recognize 里看不到"识别"发生 —— 那边只负责把生成器交出去）。
      2) 宿主这样驱动它（src/station/jobs/manager.py 的 _run）：
             gen = skill.build_runner(ctx, **args)   # 造生成器（此时还没跑）
             for ev in gen:                          # 取一次 = 往前跑一段
                 job.append(ev)                      # 把这一步写进任务记录 + 落库
         所以"识别跑在哪个方法里"的准确答案是：**宿主的工作线程**在跑，本函数是它的引擎。
      3) 每次 yield 出去的是一个**事件 dict**，宿主只认这三种（见 manager 模块 docstring）：
             {"type":"progress", "percent":18, "message":"正在逐页读取内容"}
             {"type":"artifact", "id":…, "name":…}
             {"type":"log", …}
         迭代自然走完 = 任务成功（宿主置 done）；中途抛异常 = 任务失败（宿主置 failed
         并把堆栈记进 job.log）。所以这里的 ValueError/RuntimeError 不是崩溃，是
         "把失败原因用中文说清楚，让人在界面上看到"。
      4) **它不"返回识别结果"**：结果（materials/issues）在这一路被**落进 SQLite 现场**
         （末尾 save_state），之后对话里的核对/改类/出件都读那份现场。这就是模块
         docstring 说的"跑完现场落 DB 即 done，没有确认门"（09-07 对话式改造）。

    入参（由调用方给，对话流程里来自工具 t_recognize）：
      project    已建好的卷的 project.json 绝对路径 —— 对话流程走这条
      photos_dir 原始翻拍图目录 —— 旧的直连方式（CLI/联调用），与 project 二选一
      person     干部姓名（默认取项目名）
      limit      只跑前 N 张（0=全部）—— 联调用，省 key；注意它只限制"跑"，
                 不丢其余页（见下面 all_records 的注释）
      order      建卷时的排序策略（拍摄时间|文件名|修改时间）
      provider   **本次用哪一家识图模型**（空=用户配置的识图链，逐家降级）。
                 传了就只用这一家 —— 这同时决定了两件事：① 调谁；
                 ② OCR 缓存键里的"通道"那一段（所以"换一家读"= 换一套缓存，**天然不覆盖**
                 前一家的结果）。用户没配过的名字会报人话错误。
      refresh    **忽略 OCR 缓存重读**（用户明确说"重新识别一遍/有缓存也要重读"时才传）。
                 不改缓存键，只是跳过 lookup、读完照常 store（覆盖那家自己那份）。
    """
    from archive.storage.project import create_project, load_project
    from archive.engine.providers import make_model

    # ── 输入解析：两种入参二选一 ──
    # project 优先：对话流程里用户已经用 scan_photos 建好卷了，这里直接用那个卷。
    # photos_dir 是给"没有对话上下文"的调用方留的（CLI、离线联调），它自己现场建卷。
    if project:
        pj = os.path.abspath(project)
        if not os.path.isfile(pj):
            raise ValueError(f"project.json 不存在：{project}")
    elif photos_dir:
        d = os.path.abspath(photos_dir)
        if not os.path.isdir(d):
            raise ValueError(f"照片目录不存在：{photos_dir}")
        proj_root = ctx.data_dir / "projects"     # 宿主给本技能的数据目录（按 skill 分）
        proj_root.mkdir(parents=True, exist_ok=True)
        name = (person or os.path.basename(d.rstrip("/\\")) or "archive")
        try:
            pj = create_project(name, d, str(proj_root), order=order)
        except Exception as e:                   # noqa
            raise ValueError(f"导入照片失败：{type(e).__name__}: {e}") from e
    else:
        raise ValueError("需提供 photos_dir（翻拍图目录）或 project（project.json）")

    proj, records = load_project(pj)             # 读这卷：proj=卷信息，records=每页一条记录
    all_records = records                        # ★ 留一份全量的 —— 下面 limit 会截短 records，
                                                 #   但"落盘写回 photos.json"必须用全量，否则
                                                 #   联调跑 limit=5 会把其余 121 页从卷里抹掉
    # 第一个 yield 很早（5%）不是随便定的：宿主收到它，界面上进度条立刻从 0 动起来，
    # 用户知道"点了有反应"（识别链真正花时间的是后面的 mark 视觉建档）。
    yield {"type": "progress", "percent": 5,
           "message": f"已载入 {len(records)} 页（项目：{proj.get('name')}）"}
    if limit and 0 < int(limit) < len(records):
        records = records[: int(limit)]          # 只跑前 N 张（records 被截，all_records 没动）
        yield {"type": "progress", "percent": 6, "message": f"limit={limit}，只跑前 {limit} 页"}
    person = person or proj.get("name") or "干部"

    # ── 识别链（模型/提示词均走 archive 自带：providers + skills/archive）──
    # 模型按**发起这次识别的用户**的「模型配置」来：建档走「识图模型」槽、
    # 定类/合并走「通用模型」槽，每个槽位一条降级链（主家挂了自动换下一家）。
    # ★ 定类走 chat（通用模型）而不是单开一个槽：两者取的都是 provider 的**同一个**
    #   角色模型名 (text_model)，分开配没有意义 —— 09-11 把界面上那个「给材料分类」
    #   槽砍了，这里跟着改。下面 make_model 的第一个参数仍是 Model 的 kind(文本/视觉)，
    #   和"槽位"是两回事，别一起改掉。
    # 注入方式是现成的：这里先造好两个模型，再经 _iter_flow 塞进 LangGraph state，
    # node_mark/_text_llm 本来就优先用 state 里传进来的那份 —— graph.py 的节点零改动。
    try:
        from station import modelcfg
        uid = getattr(ctx, "user_id", "")
        entries_text = modelcfg.resolve(uid, "chat")
        entries_vision = modelcfg.resolve(uid, "vision")
        if provider:
            # 本次只用指定这一家（不改用户的配置）：先从链里挑（保序、形状一致），
            # 链里没有就用 resolve_one 单独解析这一家 —— 用户可能就是想把某家
            # 单独拿来跑一遍对比，哪怕它没排进识图链。
            entries_vision = ([e for e in entries_vision if e["id"] == provider]
                              or modelcfg.resolve_one(uid, provider, "vision"))
            if not entries_vision:
                raise RuntimeError(
                    f"识图模型「{provider}」没配置或看不了图 ——"
                    f"在界面右上角「模型配置」里给它填上「识图模型」再试")
        # on_retry：模型层退避重连时，把"正在重连 / 还有几秒"写进 job 的进度文案。
        # 识别链的 6 路并发 worker 都会调它，所以 Job.append 那把锁是必需的（它本来就有）。
        note_retry = _job_retry_notifier(ctx)
        llm_text = make_model("text", entries=entries_text, on_retry=note_retry)
        llm_vision = make_model("vision", entries=entries_vision, on_retry=note_retry)
        # 建档链的标签：OCR 缓存的键之一。★ 必须传下去 —— 缓存键若不跟着用户的
        # 通道选择走，把视觉换成 glm 之后，同一张图用 glm 读的结果会被 qwen 的请求
        # 命中（静默的错误结果，不是崩溃，极难发现）。
        # 09-12：它同时还是"这卷的现场归哪一家"的键（archive_states.channel），
        # 所以换一家重跑 = 多一行现场，**不会覆盖**前一家。
        vision_channel = modelcfg.chain_label(entries_vision)
    except Exception as e:                       # noqa
        raise RuntimeError(
            f"模型不可用（检查 src/.env 的 key，或界面「模型配置」）: {e}") from e

    # ── 跑识别链：_iter_flow 逐条吐事件（节点进度 + 每页一条）──
    # ★ 结果（materials/issues/records…）不再随事件走，而是由 _iter_flow 跑完后
    #   **回填进 last_merged**（生成器的 return 值会被 for 丢掉，所以走这个可变 dict）。
    #   循环里只管把事件转给宿主即可。
    last_merged = {}
    for ev in _iter_flow(records, llm_text, llm_vision, vision_channel,
                         last_merged, refresh=refresh):
        # 页级事件在这里补上图片 URL：只有这一层知道卷的路径（project.json），
        # 而 engine/graph.py 不该知道 web 那层怎么取图（见 page_url 的注释）。
        if ev.get("type") == "page":
            ev["img"] = page_url(pj, ev.get("seq"), thumb=True)
        yield ev
    materials = last_merged.get("materials") or []
    issues = last_merged.get("issues") or []
    ocr_cached = int(last_merged.get("ocr_cached") or 0)   # 本轮有几页命中了 OCR 缓存
    cached_msg = f"（其中 {ocr_cached} 页命中缓存，未重复识别）" if ocr_cached else ""

    # 建档卡写回 photos.json：识别出的每页 OCR 内容（标题/日期/文种）必须落盘 ——
    # 不写回的话，之后"看某页原图"、重开这卷、导出 PDF 都读不到每页内容。
    # 注意用 all_records 拼（全量），只把跑过的那几页换成新记录 —— 联调跑 limit=5 时
    # 不能把没跑的其余页从卷里丢掉。
    orient_msg = ""
    if last_merged.get("records"):
        from archive.storage.project import save_records
        updated = {r.get("seq"): r for r in last_merged["records"]}   # seq → 新记录
        merged_records = [updated.get(r.get("seq"), r) for r in all_records]
        # —— 方向校正：把横躺的照片统一转向（"整卷该往哪转"由模型二选一探出来）——
        # 放在**落盘之前**做：rotate 是 photos.json 里的字段，顺手一次写完，
        # 免得为了它再整卷写一遍。探测失败/没把握就一页都不动（见 orient 模块注释），
        # 退回"在下面那句收尾话里提醒用户"这条路。
        try:
            from archive.engine.orient import auto_rotate
            _, _, orient_msg = auto_rotate(merged_records, llm_vision,
                                           channel=vision_channel)
        except Exception:                    # noqa：方向探测出问题不能拖垮整卷识别
            orient_msg = ""
        save_records(pj, merged_records)

    # ── 现场落 DB（识别的终点）──
    # 这是整条链最重要的落点：识别出的材料清单（含每份的类别/标题/页号/存疑）写进
    # SQLite 的 archive_states 表。之后用户说的每句话（改类/并份/出件）都作用在这份
    # 现场上，而不再重跑识别 —— 出件时读的也是它（曾因读 job.result 的原始态出过错，
    # 见 export_archive 的注释）。job_id/user_id 一起存，方便溯源与归属。
    # ★ channel=vision_channel：这份现场**属于哪一家模型**。于是"换个模型重跑"是
    #   多写一行、不是覆盖 —— 两家的结果并存，展示时用 use_model 切（09-12）。
    from archive.service.interactive import save_state, set_active
    save_state(pj, materials, issues, person,
               job_id=getattr(ctx, "job_id", "") or "",
               user_id=getattr(ctx, "user_id", "") or "",
               channel=vision_channel)
    set_active(pj, vision_channel)       # 新跑完的这份自动成为"当前展示"（用户刚等到它）
    yield {"type": "progress", "percent": 100,
           "message": (f"识别完成：材料 {len(materials)} 行，问题 {len(issues)} 项"
                       f"{cached_msg}{orient_msg}")}
