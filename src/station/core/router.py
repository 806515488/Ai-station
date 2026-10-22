"""三层路由（L1 规则 → L2 小模型 → 未命中落 L3 agent）——**只识别 skill**。

设计原则：工具不是全局的，所以路由层绝不直接返回 archive.set_category 这类
具体工具名。它只回答一个问题：“这句话该交给哪个技能？”
  L1  本地关键词/正则（零成本）：出件/改类/OCR/写周报等明确话术 → skill；
  L2  意图识别链（普通聊天模型 / Jev System One）拿“skill 描述 + 工具用途提炼的话术锚点”做语义判断，
      校验置信度与白名单，过阈值才输出 skill_id；
  L3  前两层都没把握 → 交给通用聊天/活跃技能 agent，由该技能自己用工具兜底。

技能被选中后，server 才 get_skill() 加载它的 tools；工具的选择留在技能内
（规则快道或 agent function calling），全局不持有工具名。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass

from station import config

# 类别小类码（十类 + 四-x/九-x），改类意图的正则要认它
_CAT = r"[一二三四五六七八九十]+(?:-\d+)?"

# ---- L1 的正则（顺序即优先级：越具体越靠前，避免“第41张”先被页卡接走）----
_RE_SEQ_CAT = re.compile(
    r"(第\s*\d+\s*(?:张|页|份))[^。]{0,12}?"
    r"(?:改成|改到|改为|归到)\s*(" + _CAT + r")")
_RE_TITLE_CAT = re.compile(
    r"《([^》]+)》[^。]{0,12}?(?:改成|改到|改为|归到)\s*(" + _CAT + r")")
_RE_RECOCR = re.compile(
    r"第\s*(\d+)\s*(?:张|页|份)[^。]{0,20}?"
    r"(?:换一家|重读|OCR|换通道|重新读)")
_RE_ADOPT = re.compile(
    r"(?:第\s*(\d+)\s*(?:张|页|份))?\s*采用(?:新读法|新结果|新卡|新的)")
_RE_PAGE = re.compile(r"第\s*(\d+)\s*(?:张|页|份)")


@dataclass
class RouteHit:
    """一次意图命中：只回答“这句话属于哪个 skill / 是不是纯闲聊”。"""
    level: str = "l1"
    confidence: float = 1.0
    skill_id: str = ""       # archive / weekly-report；空串=通用聊天


def l1_route(text: str, skill_ids: set[str]) -> RouteHit | None:
    """L1 本地规则：只把明确话术归到某个 skill，不碰工具名。"""
    t = (text or "").strip()
    if not t:
        return None
    has_archive = "archive" in skill_ids
    has_weekly = "weekly-report" in skill_ids
    has_video = "video" in skill_ids
    has_interview = "interview-coach" in skill_ids

    if (has_archive and any(k in t for k in
            ("干部档案", "档案整理", "整理档案", "翻拍照片", "上传照片", "选照片",
             "开始识别", "识别进度", "材料清单", "看全景", "全景", "出件", "4件套",
             "对账", "终版目录",
             # ★「口径」是档案分类规则的说法，而且**学习卡上的「确认，写进去」按钮
             #   预填的就是"确认这条口径…"** —— 不收这个词，用户点确认后会掉进通用聊天
             #   （那边没有 apply_learning），点了没反应。09-13 真浏览器实测踩到。
             "口径", "文种对照"))
            or (has_archive and (_RE_SEQ_CAT.search(t) or _RE_TITLE_CAT.search(t)
                                 or _RE_RECOCR.search(t) or _RE_ADOPT.search(t)))):
        return RouteHit(skill_id="archive")

    if has_archive and _RE_PAGE.search(t) and (
            "看" in t or "显示" in t or "查" in t or t.startswith("第")):
        return RouteHit(skill_id="archive")

    if has_weekly and ("周报" in t or "本周总结" in t or "写总结" in t):
        return RouteHit(skill_id="weekly-report")

    if has_interview and any(k in t for k in (
            "面试复盘", "面试视频", "面试录音", "面试音频", "面试问答",
            "提取音频", "视频转音频", "ffmpeg", "FFmpeg", "整理面试问答",
            "逐题复盘", "改进面试回答", "面试表现总结")):
        return RouteHit(skill_id="interview-coach")

    # 视频排在最后：档案/周报的正则更具体，别被"视频"这种宽词先接走。
    # ★「视频」这两个字必须收 —— 草稿卡上的按钮预填的就是"…生成视频吧"，
    #   按钮也是一条路由输入（跟 09-13 学习卡那次同一条教训）。
    if has_video and any(k in t for k in ("视频", "短片", "动画")):
        return RouteHit(skill_id="video")
    return None


# ── "切换词根"：线程已经在某个技能里时，靠什么判断"用户真的换了一件事" ──────
#
# ★★ 为什么需要这条规则（09-14 用户实测踩到的真 bug）：
#   L1 和 L2 **都是无上下文的** —— 它们只看当前这一句。于是在一条**视频**线程里，
#   用户说"重新生成一个，我重新上传照片"，L1 撞上档案的词表里的"上传照片"、L2 也跟着
#   判档案，线程就被抢走了。后果不只是"答非所问"：**技能一换，工具集跟着换** ——
#   `video.ask_photo` 当场从模型手上消失，而模型（从自己记的笔记里）知道该用它，
#   手上却没有，只能反复调 remember/recall 兜圈子，一整轮什么都没办成。
#
#   规矩：**已经在做的事，要有一个明确的反向信号才换**。这里的"明确信号"= 消息里
#   真的出现了目标技能的词根（用户说"帮我整理这卷干部档案"就该切过去）。
#   ★ 没登记的新技能**不限制**（行为与加这条之前一致）—— 宁可新技能好使，
#     也不要它因为漏登记而不响应。
_SWITCH_ROOTS = {
    "archive": ("档案", "翻拍", "卷", "出件", "材料清单", "全景", "口径", "文种"),
    "weekly-report": ("周报", "总结"),
    "video": ("视频", "短片", "动画", "出镜"),
    "interview-coach": ("面试", "复盘", "音频", "ffmpeg", "FFmpeg", "转写", "文字稿"),
}


def may_switch_to(text: str, target: str, active: str) -> bool:
    """线程已经在 `active` 技能里时，准不准切到 `target`。

    返回 False = **留在原技能**（调用方据此忽略这次判定）。
    """
    if not target or not active or target == active:
        return True                      # 没目标 / 没在做什么 / 本来就是它 → 放行
    roots = _SWITCH_ROOTS.get(target)
    if roots is None:
        return True                      # 没登记的技能不限制
    t = text or ""
    return any(r in t for r in roots)


def _note(diag: dict | None, **kw) -> None:
    """把 L2 这轮的判定细节记进诊断字典（调用方据此写路由日志）。

    diag 传 None 时什么都不做 —— 这样不关心日志的调用方（含单测）不必改代码。
    """
    if diag is not None:
        diag.update(kw)


class RouteDeadline(Exception):
    """L2 判词用满墙钟预算（**我们自己兜的那层**，不是底层抛的）。

    刻意不复用内置 TimeoutError：底层模型也可能自己抛 TimeoutError，两者要能分开
    —— 前者记 reason="timeout"（我们放弃了），后者记 reason="error:TimeoutError"
    （对端出错了）。混在一起，看路由日志时就分不清是"预算不够"还是"对方超时"。
    """


def _with_deadline(fn, seconds: float):
    """跑 fn，最多等 seconds 秒 —— **真正的墙钟截止**，超了就放弃。

    ★ 为什么不直接靠 HTTP 客户端的 timeout：httpx 的 timeout 是**分阶段**的
      （connect / read / write / pool 各算各的），不是"整个请求最多 N 秒"。
      实测（黑洞服务 + timeout=1.5）单次调用跑满 3.3 秒才返回；真机上
      data/station/route_log.jsonl 里还见过 23 秒的路由卡顿 —— 那几次的
      ROUTE_TIMEOUT 写的也是 1.5 秒，**预算根本没兜住**。判词只是"加速通道"，
      它慢一秒用户就在输入框前多等一秒，所以这里自己兜一层硬截止。

    超时后那个线程留在后台跑完（daemon，结果丢弃）：L2 本来就是"拿不准就降级"
    的定位，不需要它的迟到答案。
    """
    box: dict = {}

    def run():
        try:
            box["r"] = fn()
        except BaseException as e:        # noqa：连 KeyboardInterrupt 也要带回主线程
            box["e"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise RouteDeadline(f"超过 {seconds}s")
    if "e" in box:
        raise box["e"]
    return box["r"]


def l2_route(text: str, catalog: list, diag: dict | None = None,
             user_id: str = "") -> RouteHit | None:
    """L2 小模型：拿着“技能目录 + 话术锚点”判断该进哪个 skill。

    输入是 build_skill_catalog() 的 SkillRef 列表（描述/锚点，无工具 schema），
    输出是 skill_id（空串=纯闲聊）；超过白名单/低置信/超时/没配 key 返回 None，
    让上层继续按活跃技能或通用聊天兜底。

    diag：可选字典，会把这轮的细节（耗时/原始置信度/放弃原因）写进去，
    供上层落路由日志 —— 将来调 ROUTE_THRESHOLD 就靠这些数据。
    """
    if os.environ.get("STATION_FAKE") == "1":
        _note(diag, reason="fake", ms=0.0)
        return None
    t0 = time.perf_counter()
    try:
        from station.models import config as modelcfg
        from station.core.intent import identify
        timeout = float(os.environ.get("ROUTE_TIMEOUT") or config.ROUTE_TIMEOUT)
        threshold = float(os.environ.get("ROUTE_THRESHOLD") or config.ROUTE_THRESHOLD)
        decision, details = identify(
            text, catalog, modelcfg.resolve(user_id, "route"), threshold=threshold,
            timeout=timeout, with_deadline=_with_deadline, deadline_error=RouteDeadline)
        _note(diag, **details)
    except Exception as exc:
        _note(diag, reason=f"error:{type(exc).__name__}")
        decision = None
    _note(diag, ms=round((time.perf_counter() - t0) * 1000, 1))
    if decision is None:
        return None
    return RouteHit(skill_id=decision["skill"], level="l2", confidence=decision["conf"])


def log_route(entry: dict) -> None:
    """把一次路由判定追加到 data/station/route_log.jsonl（每行一条 JSON）。

    用途：跑两周真对话后回头统计 —— L2 到底放行了什么、L3 兜了什么、置信度分布
    长什么样，再据此调 ROUTE_THRESHOLD（现在 0.8 是没有实据的默认值）。
    写日志绝不能影响对话，所以异常一律吞掉；ROUTE_LOG=0 可整体关闭。
    """
    if not config.ROUTE_LOG:
        return
    try:
        line = json.dumps({"ts": round(time.time(), 3), **entry},
                          ensure_ascii=False)
        p = config.DATA_DIR / "route_log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                            # noqa：日志写不了也不该影响对话
        pass
