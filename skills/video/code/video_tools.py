"""video 技能 —— 「AI 视频生成」的对话面（tools）+ 后台执行器（build_runner）。

新手视角（Java 朋友版）：
  一个技能 = 一个 entry 模块，**暴露什么就挂什么**（见 src/station/skills/registry.py）：
    · 写了 `tools()`        → 模型在对话里能点名调用的动作；
    · 写了 `build_runner()` → 宿主拿去当**后台任务**跑的长活。
  本技能两个都有 —— 和 skills/archive 是同一种形态。为什么要两个入口：生成一条视频要
  几分钟，不能让一次工具调用干等（HTTP 早超时了），所以 `make` 只**起任务**，
  真正跑的是 `build_runner`。

它干什么：
  draft  用「通用模型」把用户的一句白话补成完整画面描述（**不生成、不花钱**）
  make   拿着那段描述去真生成（**过批准闸**，因为这一步真的花额度）
  status 查进度
  show   把生成好的视频作为卡片发到对话里

★ 两步制是刻意的（09-14 用户拍板）：先出「提示词草稿」给人看，人点头才生成。
  视频生成即使限时免费、也要等好几分钟（免费档每分钟只允许 1 次请求），
  让人先看一眼描述再决定，比"生成完不满意再重来"划算得多。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

from station.core.tool import Tool

# ── 视频模型的硬约束（Agnes Video 2.5 Flash，官方文档）────────────────
# 这几个数字散在工具描述、校验、卡片文案好几处，统一放这儿，改一处就够。
SECONDS_MIN, SECONDS_MAX, SECONDS_DEFAULT = 4, 12, 5
ASPECTS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
ASPECT_DEFAULT = "16:9"
SIZE = "720P"                       # Flash 档只支持 720P

# 轮询间隔（秒）。★ 免费档的 RPM 只有 1（每分钟 1 次请求），官方文档建议 1–2 秒查一次
# 那是给付费档的；这里默认给 5 秒，撞上 429 会自动退避（见 build_runner）。
# 机器上想调就设环境变量，不用改代码。
POLL_SECONDS = float(os.environ.get("STATION_VIDEO_POLL", "5") or "5")
# 一整条任务最多等多久（秒）。超了就报错 —— 否则对端卡住会变成一个永远不动的任务。
POLL_DEADLINE = float(os.environ.get("STATION_VIDEO_TIMEOUT", "900") or "900")

_SKILL_DIR = Path(__file__).resolve().parents[1]     # skills/video/


# ── 小工具 ───────────────────────────────────────────────────────────

def _owner(ctx) -> str:
    """这次操作算谁的：thread 上有登录用户就优先用它，否则回落 ctx.user_id。

    （和 skills/weekly-report、skills/archive 逐字同款 —— 归属必须一处一个规矩，
      见 docs/conventions.md「技能落库归属一律取 ctx.user_id」。）
    """
    t = getattr(ctx, "thread", None)
    return ((getattr(t, "user_id", "") if t else "") or
            getattr(ctx, "user_id", "") or "")


def _read_prompt(name: str) -> str:
    """读技能目录下 prompts/<name>.md 的原文（识别/生成口径都外置成文件，和 archive 一致）。

    为什么要外置：改口径是**改文件**，不是改代码 —— 不用碰 python、不用重新装载技能。
    """
    return (_SKILL_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def _norm_seconds(v) -> str:
    """把模型/用户给的时长收拾成 4–12 的**字符串**。

    ★ 必须宽容：模型很容易传 "8 秒"、8、甚至 "99"。直接透传的后果是要么被对端 400
      挡回来，要么生成一条 99 秒（不可能，但报错会更难懂）。给不出数字就用默认值 ——
      宁可生成一条正常的，也别让整条链路失败。
    """
    m = re.search(r"\d+", str(v or ""))
    if not m:
        return str(SECONDS_DEFAULT)
    return str(min(SECONDS_MAX, max(SECONDS_MIN, int(m.group()))))


def _norm_aspect(v) -> str:
    """比例不在白名单里就回 16:9（同上：宽容优先于报错）。"""
    a = str(v or "").strip()
    return a if a in ASPECTS else ASPECT_DEFAULT


def _video_entry(ctx) -> tuple[dict, str]:
    """取这次该用的视频连接信息 (entry, 报错文案)。

    走的是**用户自己的模型配置**（「视频生成模型」那一行），所以界面里改了就生效 ——
    这正是那个槽位有意义的理由。返回的 entry 形状见 station/modelcfg._entry。
    """
    from station import modelcfg
    entries = modelcfg.resolve(_owner(ctx), "video")
    if not entries:
        return {}, ("还没配置「视频生成模型」。点右上角 ⚙ 「模型配置」，"
                    "在「视频生成模型」那一行选中一家并填好密钥。")
    return entries[0], ""


def _thread_job_id(ctx) -> str:
    """本会话最近一次起的视频任务号（刷新/回放后前端靠它接进度）。"""
    t = getattr(ctx, "thread", None)
    return ((getattr(t, "meta", {}) or {}).get("job_id") or "") if t else ""


def _remember_job(ctx, job_id: str) -> None:
    """把"这个会话正在跟的任务"写进 Thread.meta —— 前端刷新/回放时才知道接哪条。

    （和 archive 的 t_recognize 同一个做法；thread 缺席时静默跳过，不是错误。）
    """
    t = getattr(ctx, "thread", None)
    if t is not None:
        t.meta["job_id"] = job_id


def _remember_photos(ctx, items: list[dict]) -> None:
    """把这一批人像的 id **按序**记进会话 —— make 从这里取。

    ★ 为什么不走"模型回传文件 id"：模型抄一长串 hex 必然出错；更要命的是**顺序必须准**
      （images 数组的顺序就是 <Picture N> 的编号，错位会让"以 <Picture 1> 为准"指到
      另一张脸上）。draft 存一次、make 按序读，比让模型复述靠谱得多。
    ★ 空列表也要写（= 清掉上一次的）：用户这轮说"不用我的照片"，下一轮 make 就不该
      还翻出旧的来。
    """
    t = getattr(ctx, "thread", None)
    if t is not None:
        t.meta["video_photos"] = [it["fid"] for it in items]


def _photos_from_meta(ctx) -> list[dict]:
    """从会话里取回 draft 记下的那批人像（顺序即 <Picture N> 的编号）。"""
    t = getattr(ctx, "thread", None)
    ids = ((getattr(t, "meta", {}) or {}).get("video_photos") or []) if t else []
    from station.files import store as fs
    out = []
    for fid in ids[:MAX_PHOTOS]:
        p = fs.path(fid)
        if p:
            out.append({"fid": fid, "name": (fs.meta(fid) or {}).get("name") or "",
                        "path": str(p)})
    return out


# ── 人像参考：找照片 / 读特征 / 拼参考块 ──────────────────────────────
#
# 数据形状（三处必须一致，改一处要连带想另外两处）：
#   上传时：group_key = f"portrait:{uid}:{batch8}"，一批最多 5 张
#   "当前一组"：该用户 portrait:{uid}: 前缀里 created 最大的那一批
#   批内顺序：按 created 升序 = 提示词里的 <Picture 1> … <Picture N>
# ★ 这个顺序**全程不得重排** —— images 数组的顺序就是 <Picture N> 的编号，
#   错位会让"以 <Picture 1> 为准"指到另一张脸上去。

PORTRAIT_PREFIX = "portrait:"
MAX_PHOTOS = 5


def _portrait_batch(ctx) -> list[dict]:
    """取"这个用户**当前那一批**人像"：按 group_key 分组、挑最新的一批，批内按时间升序。

    返回 [{"fid","name","path"}…]；没有就回空列表（调用方据此走纯文生视频那条路）。
    ★ 用文件区当"记录"而不是另开一张表：照片本来就存在那儿，多一份索引就多一处会漂。
    """
    from station.files import store as fs
    uid = _owner(ctx)
    if not uid:
        return []
    prefix = f"{PORTRAIT_PREFIX}{uid}:"
    mine = []
    for f in fs.iter_files(uid):
        key = str(f.get("group_key") or "")
        if key.startswith(prefix) and f.get("id"):
            mine.append(f)
    if not mine:
        return []
    # 最新的一批（batch 号就是那次上传的时间序，取 created 最大的那批）
    newest = max(mine, key=lambda f: (f.get("created") or 0))
    batch = newest.get("group_key")
    same = [f for f in mine if f.get("group_key") == batch]
    # 批内顺序 = **上传时的先后**（上传端点写进 portrait_index）。
    # ★ 别只按 created 排：同一批连着写几张可能撞到同一个时间戳，那种**静默错位**会让
    #   提示词里的 <Picture 1> 指到另一张脸上 —— 而这个功能的全部意义就是"像本人"。
    same.sort(key=lambda f: (f.get("portrait_index", 999), f.get("created") or 0))
    out = []
    for f in same[:MAX_PHOTOS]:
        p = fs.path(f["id"])
        if p:
            out.append({"fid": f["id"], "name": f.get("name") or "", "path": str(p)})
    return out


def _img_data_url(path: str) -> str:
    """本地图片 → base64 data URL（喂给识图模型）。

    ★ 这张图**只走这一条一次性消息**，绝不写进 Thread 历史 —— 历史落 SQLite 且每轮重发，
      几 MB 的 base64 会让会话表和 token 一起炸（见 core/model.py `_to_llm` 的告诫）。
    """
    data = Path(path).read_bytes()
    mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _extract_json(text: str) -> dict | None:
    """从模型回答里抠出第一个 {...} 块并解析（模型常常会加一句解释或围栏）。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):                      # 去掉 ```json 围栏
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def _describe_portrait(ctx, items: list[dict]) -> tuple[dict | None, str]:
    """让「识图模型」看这几张照片，回一份结构化外貌描述。返回 (结果, 失败原因)。

    走的是**用户自己配置的那一行**（含他排的降级链），和档案识别用的是同一套配置。
    ★ 失败**不抛异常、也不静默**：调用方要把原因写进草稿卡的 note 里 ——
      "特征没读出来"和"读出来了但不好看"对用户是两回事。
    """
    from station import modelcfg
    from station.core.model import build

    entries = modelcfg.resolve(_owner(ctx), "vision")
    if not entries:
        return None, ("「识图模型」还没配置 —— 打开右上角 ⚙ 模型配置，"
                      "在「识图模型」那一行选中一家并填好密钥。")

    content = [{"type": "text",
                "text": _read_prompt("portrait") + "\n\n"
                        f"（这次一共 {len(items)} 张照片，按下面顺序编号）"}]
    for i, it in enumerate(items, 1):
        content.append({"type": "text", "text": f"第 {i} 张（<Picture {i}>）：{it['name']}"})
        content.append({"type": "image_url",
                        "image_url": {"url": _img_data_url(it["path"])}})
    msgs = [{"role": "user", "content": content}]

    try:
        resp = build("vision", entries=entries).respond(msgs)
        raw = (resp.get("content") or "").strip()
    except Exception as e:                        # noqa：没 key / 对端挂了 / 超时
        return None, f"识图模型调用失败（{type(e).__name__}）"
    d = _extract_json(raw)
    if not d:
        return None, "识图模型没有按格式返回外貌描述"
    return d, ""


def _ref_block(items: list[dict], traits: dict | None) -> str:
    """拼出塞进提示词的那一段「人物参考」。**没有照片时返回空串**。

    ★ 只有照片时才有这一段 —— 让"没有参考图"这条路和加功能之前**逐字一致**，
      回归风险为零（模板里 {REF_BLOCK} 被替换成空串，其余一个字没动）。
    """
    if not items:
        return ""
    photos = ((traits or {}).get("photos") or [])
    framing = {}
    for p in photos:
        if isinstance(p, dict) and p.get("n"):
            framing[int(p["n"])] = p
    lines = [f"## 本次会随提示词一起提交 {len(items)} 张照片（按此编号）"]
    for i, it in enumerate(items, 1):
        f = framing.get(i) or {}
        tag = f"（{f.get('framing')}）" if f.get("framing") else ""
        bad = "（这张不太清楚，仅供参考）" if f.get("usable") is False else ""
        lines.append(f"- <Picture {i}>{tag}{bad}")
    feats = [str(x).strip() for x in ((traits or {}).get("features") or []) if str(x).strip()]
    if feats:
        lines.append("")
        lines.append("这个人物的外貌**必须忠于**下面这组特征，不要另编长相：")
        lines.append("；".join(feats))
        subj = str((traits or {}).get("subject") or "").strip()
        if subj:
            lines.append(f"（整体印象：{subj}）")
    lines += [
        "",
        "在描述里**用 `<Picture 1>` 这样的写法明确指代**"
        "（例：\"以 `<Picture 1>` 中人物的五官和发型为准\"）。",
        "**不要**写\"照片\"\"参考图\"\"素材\"这类元话术，直接描述画面。",
    ]
    # ★ 以**两个**换行结尾，且模板里占位符后面直接跟 `## 用户想要的内容`（没有自己的换行）
    #   —— 这样"替换成空串"时模板与加这个功能之前**逐字一致**（回归风险为零），
    #   非空时又能和后面的标题之间留出标准的空行。
    return "\n".join(lines) + "\n\n"


# ── 工具：draft（用通用模型优化提示词，不花钱）────────────────────────

def t_draft(ctx, idea: str, seconds: str = "5", aspect_ratio: str = "16:9",
            use_my_photos: bool = False) -> dict:
    """把用户一句白话补成完整的画面描述，**发出草稿卡等人点头**（不生成视频）。

    带人像时多一步：先用「识图模型」把照片里的人物外貌读成文字，一起融进描述。
    ★ 这一步**不生成视频、不花钱**，照片也**还没离开这台服务器** ——
      真正"把照片交给视频服务"发生在 make 那一步（那里要过批准闸）。

    ★ `use_my_photos` **默认关**：用户传过照片不等于每条视频都想出镜。
      只有他明说"用我的照片/让我出镜/以我为主角"时才传 true。
    """
    from station import modelcfg
    from station.core.model import build

    raw = (idea or "").strip()
    if not raw:
        return ("还没告诉我你想看什么。用一句话说说就行，比如"
                "「一只猫跳上窗台」——我来补成完整的画面描述。")

    sec, ar = _norm_seconds(seconds), _norm_aspect(aspect_ratio)

    # —— 人像：只有用户明确要用才带上。这一批同时记进会话，make 从那儿按序取 ——
    items, traits, why = [], None, ""
    if use_my_photos:
        items = _portrait_batch(ctx)
        if items:
            traits, why = _describe_portrait(ctx, items)
        else:
            why = ""            # 没传过照片不是"读失败"，别让下面那句提示出现
    _remember_photos(ctx, items)

    tmpl = _read_prompt("optimize")
    ask = (tmpl.replace("{IDEA}", raw)
               .replace("{SECONDS}", sec)
               .replace("{ASPECT}", ar)
               .replace("{REF_BLOCK}", _ref_block(items, traits)))

    notes = []
    if items and why:
        # ★ 读不出来**不静默降级**：照样出草稿卡、照片照样会交给视频模型，
        #   但要把话说清楚 —— 否则用户会以为特征已经用上了。
        notes.append(f"（照片里的人像没读出来：{why}。照片仍会原样交给视频模型）")
    elif items and not ((traits or {}).get("features")):
        notes.append("（没读出可用的外貌特征，照片仍会原样交给视频模型）")

    # 优化走「通用模型」那一槽 —— 就是日常对话用的那个模型。
    # ★ 失败不回退成"直接用原话去生成"：那样用户会以为优化过了。老实说清楚，
    #   让他自己决定（重试、或者直接点生成）。
    entries = modelcfg.resolve(_owner(ctx), "chat")
    if not entries:
        return "「通用模型」还没配置，没法帮你把描述补完整。请先在 ⚙ 模型配置里配好。"
    try:
        resp = build("text", entries=entries).respond(
            [{"role": "user", "content": ask}])
        text = (resp.get("content") or "").strip()
    except Exception as e:                       # noqa：没 key / 对端挂了 / 超时
        return (f"优化描述时模型调用失败（{type(e).__name__}）。"
                f"你可以直接说「就用这句话生成」，我照原话交给视频模型。")

    if not text:
        # 模型返回空 = 没优化成功。退回原话，但**明说是原话**，别让人以为优化过了。
        text = raw
        notes.append("（模型没给出改写结果，下面是你原话）")

    render = {"type": "prompt-card", "idea": raw, "prompt": text,
              "seconds": sec, "aspect_ratio": ar, "size": SIZE,
              "note": " ".join(notes)}
    if items:
        render["photos"] = [{"id": it["fid"], "name": it["name"]} for it in items]
        render["features"] = (traits or {}).get("features") or []
        render["subject"] = (traits or {}).get("subject") or ""

    who = f"（含 {len(items)} 张人像参考）" if items else ""
    return {
        "text": (f"给你一版提示词草稿{who}：{text}\n"
                 f"（{sec} 秒 / {ar} / {SIZE}）{' '.join(notes)}"
                 f"觉得可以就说「生成」，要改就说改哪儿，我重写一版。"),
        "render": render,
    }


def t_ask_photo(ctx) -> dict:
    """发一张"选你的人像照片"上传卡片（用户在自己电脑上选 1~5 张）。

    照 archive 的 `t_ask_photos` 的做法：卡片由前端渲染，选完文件由**前端**直接传给
    `/api/portrait`（不经过工具调用），然后用户那侧发一句话回来。
    """
    have = _portrait_batch(ctx)
    note = f"（现在用的是你上次传的 {len(have)} 张）" if have else ""
    return {
        "text": (f"已经把上传卡片发出去了{note}。用户选完照片会自己上传，"
                 f"你会收到一条「人像照片已上传，用于视频参考」的消息 —— "
                 f"那时候再调 draft 并带上 use_my_photos=true。"),
        "render": {"type": "upload-card", "purpose": "portrait",
                   "hint": "选 1~5 张你自己的照片（正脸那张最有用）"},
    }


# ── 工具：make（真生成，过批准闸）─────────────────────────────────────

def _preview_make(ctx, prompt: str = "", seconds: str = "5",
                  aspect_ratio: str = "16:9") -> str:
    """批准卡上给用户看的那段话（只读，不产生任何副作用）。

    ★ 这段是用户"点头之前唯一能看到的东西"，所以要**把要发出去的画面原样列出来** ——
      生成是不可撤销的（白等几分钟），他得知道到底会生成什么。
    """
    sec, ar = _norm_seconds(seconds), _norm_aspect(aspect_ratio)
    p = (prompt or "").strip() or "（空 —— 请先给我一句画面描述）"
    entry, err = _video_entry(ctx)
    who = f"{entry.get('label') or entry.get('id')} · {entry.get('model')}" if entry else "（未配置）"
    lines = [f"用 {who} 生成一条 {sec} 秒 / {ar} / {SIZE} 的视频。",
             f"画面：{p}"]
    # ★★ 这里有张照片会**离开你的服务器**，用户唯一能知情的地方就是这张卡 —— 必须说全：
    #   几张、名字、链接活多久、以及"有效期里谁拿到链接都能打开"。
    from station.files import share
    photos = _photos_from_meta(ctx)
    if photos:
        names = "、".join((it["name"] or "照片") for it in photos)
        mins = int(share.link_ttl() // 60) or 1
        lines.append(f"⚠ 会把其中 {len(photos)} 张人像（{names}）交给视频服务做参考 ——"
                     f"为此会开一个**临时公开链接**，约 {mins} 分钟后自动失效；"
                     f"链接有效期内，谁拿到这个地址都能打开这几张图。")
    lines.append("预计要等几分钟（后台生成，进度在下方面板里）。")
    return "\n".join(lines)


def t_make(ctx, prompt: str, seconds: str = "5", aspect_ratio: str = "16:9"):
    """起一个后台生成任务，立刻返回（不阻塞对话）；**顺手发一张进度块卡片**。

    返回 `{"text": …, "render": {"type":"job", "job_id": …}}` —— 文字照旧回给模型，
    卡片由宿主落到会话里（前端拿 job_id 拉进度、成了就把成片嵌在里面播）。

    ★ 进度块做成**卡片**是 09-16 改的，为的是修用户报的两个毛病，根子是同一个：
      卡片经 `_remember_card` 进会话 meta，刷新后**在原位**重画；而以前那块进度条是
      前端"一个回合结束时追加到对话末尾"的 ——
        ① 它不在历史里：成片只活在那块面板上，一刷新就没了；
        ② 一回合以"等你批准"结束时也会追加，而那一刻会话里的 job_id 还是**上一个**
           任务 —— 旧成片会顶在新批准卡下面，看着像"重新生成却把以前的视频调出来"。
      卡片把这两条一起解决了：位置对（跟着工具调用走）、内容在（落库）。
    """
    from station.jobs.manager import get_manager
    from station.skills.registry import get_registry

    p = (prompt or "").strip()
    # ★ 硬门：没描述就不建任务。光靠 system.md 写"要先问"挡不住模型图省事 ——
    #   同一件事落到代码层才牢（和 archive 的"没姓名不建卷"是同一条规矩）。
    if not p:
        return ("还没告诉我画面是什么 —— 先给我一句话，比如「一只猫跳上窗台」，"
                "我可以先帮你补成完整的描述。")

    # 起任务前先把"能不能连"问清楚：没配模型就直说，别建一个注定失败的 job
    entry, err = _video_entry(ctx)
    if err:
        return err

    # 人像：draft 那一步把它记进了会话，这里取出来。
    # ★ 交给 job.args 的是**文件 id**，不是 URL —— 临时链接只该在执行器里现签现撤，
    #   一旦进了 `jobs.args` 就是一行长期的明文（见 build_runner 的告诫）。
    photos = [it["fid"] for it in _photos_from_meta(ctx)]
    if photos:
        from station import config
        # ★★ **硬门**：没配对外地址就不建任务。
        #   对端抓不到本机的地址，硬跑下去只有两种结局：等几分钟后失败，或者**静默**生成
        #   一条"照片压根没送出去、所以不像本人"的片子。后者更糟 —— 用户花了额度、
        #   还以为功能就是这样。宁可在这里一句话说清楚。
        if not config.public_base():
            return "这次要用你的人像照片，但" + config.public_base_hint()

    sec, ar = _norm_seconds(seconds), _norm_aspect(aspect_ratio)
    skill = get_registry().get(ctx.skill_id or "video")
    if skill is None or skill.build_runner is None:
        return "视频生成引擎没装载，重启工作站再试。"
    job = get_manager().submit(
        skill, {"prompt": p, "seconds": sec, "aspect_ratio": ar, "photos": photos},
        user_id=_owner(ctx))
    _remember_job(ctx, job.id)
    who = f"，带 {len(photos)} 张人像参考" if photos else ""
    return {
        "text": (f"开始生成了（任务 {job.id}）：{sec} 秒 / {ar} / {SIZE}，"
                 f"用 {entry.get('label')}{who}。预计要等几分钟，"
                 f"进度就在上面那条进度块里，生成好会直接嵌在里面；"
                 f"期间你可以继续跟我说话。"),
        # ★ job_id 就是一切：前端拿它拉 /api/jobs/{id} 画进度、成片也从那儿取。
        #   卡片只放 id、不放视频字节 —— 卡是要落进会话 meta 的（见上面 docstring）。
        "render": {"type": "job", "job_id": job.id},
    }


# ── 工具：status / show ──────────────────────────────────────────────

def t_status(ctx, job_id: str = "") -> str:
    """查生成进度（不给编号就用这个会话最近一次的任务）。"""
    from station.jobs.manager import get_manager
    jid = (job_id or "").strip() or _thread_job_id(ctx)
    if not jid:
        return "这个会话还没有生成过视频。"
    snap = get_manager().get(jid)
    if snap is None:
        return f"任务 {jid} 不存在。"
    st = snap.get("status") or ""
    if st == "done":
        return f"生成完成了（任务 {jid}）。说一声我就把它放出来给你看。"
    if st == "failed":
        return f"生成失败了（任务 {jid}）：{snap.get('message') or '原因未提供'}"
    return (f"还在生成中：{snap.get('progress') or 0}% —— {snap.get('message') or ''}")


def t_show(ctx, file_id: str = "") -> dict:
    """把生成好的视频作为卡片发到对话里（对话里能直接播放 + 下载）。"""
    from station.files import store as fs
    from station.jobs.manager import get_manager

    fid = (file_id or "").strip()
    if not fid:
        # 没给文件号 → 取这个会话最近一次任务的最后一个产物
        jid = _thread_job_id(ctx)
        snap = get_manager().get(jid) if jid else None
        arts = (snap or {}).get("artifacts") or []
        fid = arts[-1] if arts else ""
    if not fid:
        return "没找到生成好的视频。先让我生成一条？"

    meta = fs.meta(fid) or {}
    if not fs.path(fid):
        return "这个视频文件已经不在了（可能被清理过）。"
    # ★ 归属校验：产物是私人的，别人（或猜到的 id）不能把它调出来看。
    owner = meta.get("owner") or ""
    if owner and owner != _owner(ctx):
        return "这个视频不属于当前账号。"
    name = meta.get("name") or "video.mp4"
    return {
        "text": (f"视频在这里：{name}（{fid}）。"
                 f"对话里可以直接播放和下载；产物区也有一份。"),
        "render": {"type": "video-card",
                   "files": [{"id": fid, "name": name}],
                   "prompt": meta.get("prompt") or ""},
    }


# ── 执行器入口 ───────────────────────────────────────────────────────

def build_runner(ctx, prompt: str = "", seconds: str = "5",
                 aspect_ratio: str = "16:9", photos: list | None = None):
    """真跑一次生成：建任务 → 轮询 → 下载 → 落产物区，一路 yield 事件。

    `photos`：人像照片的**文件 id 列表**（顺序即提示词里 `<Picture N>` 的编号）。
    给了就把它们当**参考图**交给视频模型（`mode=reference`）—— 这样成片才"像本人"，
    而不只是"照着文字描述画了一个人"。

    新手视角：这是个**生成器函数** —— 每 `yield` 一次，宿主就收到一条进度事件并
    落库，所以前端进度条能实时动。宿主只认三种事件（见 jobs/manager.py 的 Job.append）：
      {"type":"progress","percent":N,"message":"…"}   改进度条
      {"type":"artifact","id":"<文件id>"}              登记产物
      {"type":"log","text":"…"}                        只留痕，不改进度条
    ★ 事件里**只放文字和 id，绝不放视频字节** —— 每个事件都会整行重写一次数据库，
      而前端每秒把整个 log 拉一遍。塞几 MB 进去会同时压垮两边（见 conventions 坑区）。
    ★★ **临时链接也绝不进事件、更不进 `job.args`**：前者经 `GET /api/jobs/{id}` 回给
      前端并落进 `jobs.log` 一列，后者落 `jobs.args` 一列 —— 两个地方都是明文长期保存的。
      所以签名这个动作**只在这里做**（make 只传 fid，URL 出了这个函数就没人知道）。
    """
    from station import modelcfg
    from station.files import share
    from station.files import store as fs

    import agnes                     # 同目录的 code/agnes.py（技能装载时已在 sys.path 上）

    sec, ar = _norm_seconds(seconds), _norm_aspect(aspect_ratio)
    text = (prompt or "").strip()
    if not text:
        raise ValueError("没有画面描述，无法生成")

    entries = modelcfg.resolve(_owner(ctx), "video")
    if not entries:
        raise ValueError("还没有配置「视频生成模型」（右上角 ⚙ 模型配置）")

    # 给这一批人像签临时链接（对端要"公网可访问的直链"）。
    # ★ 全程 try/finally：**任务一进终态就撤销**。官方要求链接活到任务完成，
    #   所以不能在提交后就撤（那是自伤）；但也不能不撤（用户的照片不该长期挂在外面）。
    #   `share.revoke` 自己吞异常 —— 善后失败绝不能把一个已经成功的任务判成失败。
    tokens: list[str] = []
    images: list[str] = []
    for fid in (photos or [])[:MAX_PHOTOS]:
        t = share.make_ref_link(fid, _owner(ctx))      # 没配对外地址会在这里抛人话
        tokens.append(t)
        images.append(share.ref_url(t))
    try:
        yield from _generate(ctx, entries, text, sec, ar, images)
    finally:
        # ★ 任务一到终态就撤销这张链接（成功、失败、超时都走这里）。
        #   `share.revoke` 内部把异常吞掉 —— 善后失败绝不能反噬任务结果。
        share.revoke(tokens)


def _generate(ctx, entries, text: str, sec: str, ar: str, images: list):
    """真正的生成链：建任务 → 轮询 → 下载 → 落产物区。

    拆成独立函数是为了让 `build_runner` 那层能干净地 `try/finally` 善后
    （撤销临时链接）—— 在一个几百行的生成器体里绕 try/finally 太容易漏。
    """
    from station.files import store as fs

    import agnes                     # 同目录的 code/agnes.py（技能装载时已在 sys.path 上）

    # ① 建任务。★ 只在**这一步**逐家试：建任务失败还没产生视频、没花掉额度，换一家是免费的；
    #    一旦建成功，后面的轮询/下载就**只认这一家** —— 半途换家等于让两家各生成一次。
    entry, last_err = None, None
    for e in entries:
        try:
            task = agnes.create_task(e["base_url"], e["api_key"], e["model"],
                                     text, sec, ar, images=images or None)
            entry = e
            break
        except Exception as ex:              # noqa：这家不行 → 试下一家
            last_err = ex
    if entry is None:
        raise ValueError(f"创建视频任务失败：{last_err}")

    vid = str(task.get("video_id") or task.get("id") or "").strip()
    if not vid:
        raise ValueError("Agnes 没返回任务编号（video_id），无法跟踪进度")
    # ★ 第一条事件必须是 progress：宿主收到它，界面上的进度条立刻从 0 动起来，
    #   用户知道"点了有反应"。真正的等待在这之后（几分钟），先把这一步做实。
    #   （archive 的识别链也是这么做的，同样的理由。）
    yield {"type": "progress", "percent": 5,
           "message": f"任务已提交（{sec} 秒 / {ar} / {SIZE}）"}
    # 任务编号只留痕、不改进度条 —— 出问题时排查要用它去对端的控制台查
    yield {"type": "log", "text": f"agnes video_id={vid} model={entry['model']}"}

    # ② 轮询到出结果。★ 只认"完成/失败"两个终态，**其余一律当还在跑** ——
    #    漏认一个状态名（比如对端哪天改叫 succeeded）会表现为"跑到最后超时"，
    #    那比多等一轮难查得多。
    deadline = time.time() + POLL_DEADLINE
    pct = 5
    status: dict = {}
    while True:
        try:
            status = agnes.query_task(entry["base_url"], entry["api_key"],
                                      entry["model"], vid)
        except agnes.AgnesError as e:
            # 被限流（免费档 RPM=1）不是失败：按对端建议退避后接着查。
            if e.status != 429:
                raise
            yield {"type": "log", "text": f"429 限流，等待 {e.retry_after or POLL_SECONDS}s"}
            time.sleep(agnes.sleep_for(e, POLL_SECONDS))
            if time.time() > deadline:
                raise ValueError(f"查询任务一直被限流，已等 {int(POLL_DEADLINE)} 秒") from e
            continue

        s = str(status.get("status") or "").strip().lower()
        if s in ("completed", "succeeded", "done"):
            break
        if s in ("failed", "error", "cancelled"):
            # 对端把原因放在 error 字段里（实测有这个键，正常时是 null）
            raise ValueError(
                f"生成失败：{status.get('error') or status.get('message') or '对端未说明原因'}")
        if time.time() > deadline:
            raise ValueError(f"生成超时（已等 {int(POLL_DEADLINE)} 秒，任务 {vid} 仍在跑）")
        # ★ 进度只许涨不许跌：宿主是拿事件里的 percent **直接覆盖**进度条的，
        #   对端偶尔回一个更小的值会让进度条倒退，看着像出错了。
        pct = max(pct, min(95, int(status.get("progress") or 0)))
        yield {"type": "progress", "percent": pct, "message": f"生成中 {pct}%"}
        time.sleep(POLL_SECONDS)

    # ③ 下载并落进产物区。★ 四个来源元数据必须齐（owner/skill_id/group_key/group_label）：
    #    少一个，右上角「我的产物」抽屉就会掉回"按文件名猜分组"，产物会七零八落。
    url = agnes.result_url(status)
    if not url:
        # ★ 报错时把"响应里到底有哪些字段"带上 —— 09-14 就是因为这条只说了"没有地址"，
        #   排查时还得再去对端手查一遍响应。字段名对不上是这类接口最常见的变化，
        #   把实际的键列出来，下次一眼就能看出该改成读哪个。
        raise ValueError("任务完成了，但响应里没有视频地址（顶层 url 与 metadata.url 都是空）。"
                         f"响应里的字段有：{sorted(status)}")
    yield {"type": "progress", "percent": 96, "message": "正在下载视频…"}
    data = agnes.download(url)

    stamp = time.strftime("%m%d-%H%M%S")
    fid = fs.save_bytes(
        data, ".mp4", name=f"AI视频-{stamp}.mp4",
        owner=_owner(ctx), skill_id=ctx.skill_id or "video",
        group_key=f"video:{vid}",
        group_label=f"AI 视频 · {text[:18]}")
    yield {"type": "artifact", "id": fid}
    yield {"type": "progress", "percent": 100, "message": "完成"}


# ── 工具面 ───────────────────────────────────────────────────────────

def tools() -> list[Tool]:
    """本技能的工具面。description 写给**模型**看，label 写给**人**看（两个受众别合并）。

    刻意**没有**把预览图/时长做成参数表：视频那一排只有「视频生成模型」一项可配，
    参数越少模型越不容易填错。
    """
    return [
        Tool(name="ask_photo", label="发人像上传卡片",
             description=("发一张上传卡片，让用户选他自己的照片（1~5 张）当视频主角的参考。"
                          "★ 用户说\"用我的照片/让我出镜/以我为主角\"但**还没传过照片**时，"
                          "先调它；他说过已经传了就别再发卡片，直接 draft。"),
             run=t_ask_photo, args=[]),
        Tool(name="draft", label="起草视频提示词",
             description=("把用户一句随口的想法改写成完整的视频画面描述（主体/动作/场景/"
                          "光线/镜头），发一张草稿卡给用户过目。"
                          "★ 用户说要做视频/生成视频/来一段视频时，**先调它**，别直接生成。"
                          "seconds=时长秒数（4-12，默认 5）；aspect_ratio=画面比例"
                          "（21:9/16:9/4:3/1:1/3:4/9:16，默认 16:9）。"
                          "★ use_my_photos=true 会用**用户自己上传的人像照片**当主角"
                          "（读出外貌融进描述、照片本身也会交给视频模型）—— 只有用户明说"
                          "\"用我的照片/让我出镜/以我为主角\"时才传 true，别自作主张。"
                          "这一步不生成视频、不花钱。"),
             run=t_draft,
             args=[{"name": "idea", "type": "str",
                    "desc": "用户原话里想看的内容（照原样传，别自己先润色）"},
                   {"name": "seconds", "type": "str",
                    "desc": "视频时长秒数，4-12 的整数，默认 5", "required": False},
                   {"name": "aspect_ratio", "type": "str",
                    "desc": "画面比例，默认 16:9", "required": False},
                   {"name": "use_my_photos", "type": "bool",
                    "desc": "true=用用户本人上传的照片当主角（默认 false）",
                    "required": False}]),
        Tool(name="make", label="生成视频",
             description=("拿一段画面描述去真生成一条 4-12 秒的视频（后台任务，要等几分钟）。"
                          "★ 只在用户**已经看过草稿并同意**之后才调（他说「生成」「就这个」"
                          "「可以」时）。prompt 要传经过 draft 的那版完整描述。"
                          "这一步会真的消耗生成额度。"),
             run=t_make, risk="approve", preview=_preview_make,
             args=[{"name": "prompt", "type": "str",
                    "desc": "完整的画面描述（draft 给的那版）"},
                   {"name": "seconds", "type": "str",
                    "desc": "视频时长秒数，4-12 的整数，默认 5", "required": False},
                   {"name": "aspect_ratio", "type": "str",
                    "desc": "画面比例，默认 16:9", "required": False}]),
        Tool(name="status", label="查生成进度",
             description="查视频生成到哪一步了（用户问「好了吗/到哪了」时用）。",
             run=t_status,
             args=[{"name": "job_id", "type": "str",
                    "desc": "任务号（可省，默认本会话最近一次）", "required": False}]),
        Tool(name="show", label="播放生成的视频",
             description=("把某一条成片单独发成一张卡片（对话里能直接播放、能下载）。"
                          "★ 成片本来就嵌在 `make` 发出的那块进度块里（生成完自动出现），"
                          "所以**生成完不用再调它**；只在用户要重看某一条、或要下载链接，"
                          "或那条不是这个会话刚生成的时候才用。"),
             run=t_show,
             args=[{"name": "file_id", "type": "str",
                    "desc": "产物文件号（可省，默认本会话最近一次的产物）",
                    "required": False}]),
    ]
