"""三层路由（L1 规则 → L2 小模型 → 未命中落 L3 agent）——**只识别 skill**。

设计原则：工具不是全局的，所以路由层绝不直接返回 archive.set_category 这类
具体工具名。它只回答一个问题：“这句话该交给哪个技能？”
  L1  本地关键词/正则（零成本）：出件/改类/OCR/写周报等明确话术 → skill；
  L2  qwen-flash 拿“skill 描述 + 工具用途提炼的话术锚点”做语义判断，
      自报 confidence，过阈值才输出 skill_id；
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
    return None


def _parse_json(text: str) -> dict | None:
    """从模型回答里抠出 JSON（先整体试，再抠第一个 {…} 块）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:                     # noqa：模型可能夹带解释文字
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:                 # noqa
            return None
    return None


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
    ids = {c.id for c in catalog}
    t0 = time.perf_counter()
    try:
        from station.core.model import Model
        from station import modelcfg
        threshold = float(os.environ.get("ROUTE_THRESHOLD")
                          or config.ROUTE_THRESHOLD)
        # 短超时 + 不重试：判词只是“加速通道”，慢或挂了就该立刻降级给主 agent，
        # 而不是让用户在输入框前干等（实测这条链路偶发 5 秒级）。
        timeout = float(os.environ.get("ROUTE_TIMEOUT") or config.ROUTE_TIMEOUT)
        # 判词走「路由判词」槽位的降级链（用户在界面「模型配置」里选，默认就是
        # ROUTE_CHANNEL 指定的那家）。★ total_budget 必须传 timeout ——
        # 1.5 秒是**整条链一共**的预算，不是每条链各 1.5 秒；否则用户给判词排了
        # 3 家时就要等 4.5 秒，等于把 09-10 刚修好的卡顿原样还回来。
        model = Model("text", timeout=timeout, max_retries=0,
                      entries=modelcfg.resolve(user_id, "route"),
                      total_budget=timeout)
        skill_lines = []
        for c in catalog:
            skill_lines.append(
                f"- {c.id}：{c.name}。{c.description} "
                f"用户常见说法：{c.hints}")
        skill_lines.append(
            '- ""：普通闲聊/开放问题/都不确定')
        prompt = (
            "你是 station 的技能路由判词器，只负责判断这句话该交给哪个技能。\n"
            "技能清单（含用户常见说法，用于理解话术，不是让你调用工具）：\n"
            + "\n".join(skill_lines)
            + '\n只输出一个 JSON：{"skill":"上面的id或空串","confidence":0~1}\n'
            f"confidence 低于 {threshold:.2f} 时也把 skill 设为空串。\n"
            f"用户输入：{text}\n")
        # ★ 硬截止包住整次调用（含建连）：Model 那边的 timeout 只作用到 httpx 的
        #   单个阶段，兜不住总时长 —— 见 _with_deadline 的注释。
        resp = _with_deadline(
            lambda: model.respond([{"role": "user", "content": prompt}], None),
            timeout)
        d = _parse_json(resp.get("content") or "")
    except RouteDeadline:                        # 我们自己的墙钟预算用完了
        _note(diag, reason="timeout",
              ms=round((time.perf_counter() - t0) * 1000, 1))
        return None
    except Exception as e:                       # noqa：没 key/调用失败/对端超时 → 上层兜底
        _note(diag, reason=f"error:{type(e).__name__}",
              ms=round((time.perf_counter() - t0) * 1000, 1))
        return None
    ms = round((time.perf_counter() - t0) * 1000, 1)
    if not d:
        _note(diag, reason="bad_json", ms=ms)
        return None
    try:                                         # 模型偶尔把 confidence 写成非数字
        confidence = float(d.get("confidence") or 0)
    except (TypeError, ValueError):
        _note(diag, reason="bad_confidence", raw=d.get("confidence"), ms=ms)
        return None
    skill = str(d.get("skill") or "").strip()
    if confidence < threshold:
        _note(diag, skill=skill, conf=confidence, reason="below_threshold", ms=ms)
        return None
    if skill == "":
        _note(diag, skill="", conf=confidence, reason="chat", ms=ms)
        return RouteHit(skill_id="", level="l2", confidence=confidence)
    if skill in ids:
        _note(diag, skill=skill, conf=confidence, reason="hit", ms=ms)
        return RouteHit(skill_id=skill, level="l2", confidence=confidence)
    _note(diag, skill=skill, conf=confidence, reason="unknown_skill", ms=ms)
    return None


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
