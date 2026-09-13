"""LangGraph 状态机：建档→按身份归组→残页裁决→分批定类→产出材料。

- LLM 全部经 langchain（providers.make_model）
- 提示词/口径经 archive.skill.loader 从 skills/archive 读取
- 节点返回 partial state（勿原地改 state）

★ 09-12 结构性改动：**"切份"不再看照片顺序**。用户确认照片上传顺序是随机的，
  于是把"相邻两页像不像同一份"换成"这页属于哪本册子/哪一天"（身份相同）——
  详见 seg.py 开头的长注释。原来的"二次合并"节点(consolidate)因此删掉：
  它靠"跨份两两问模型要不要并"，既有"每批 40 份、跨批永远合不上"的硬伤，
  又要求"同小类"（碎片早被分到不同类），目标已被身份归组取代；
  它留下的"没判出身份"的尾巴，改由新的 resolve 节点专门裁决。

新手视角（Java 朋友版）：LangGraph = 把一条流水线画成“有方向的图”，节点之间传
一个共享状态(state≈一个 Map，五个节点各写各的 key)。读它只看四件事就懂 80%：
    1) build_graph()（文件最底部）——五个节点的先后连线，一眼看全流程
    2) 每个 node_* 的开头 docstring —— 一句话职责
    3) state 里传什么 —— 见 build_graph() 前的 St 类型（records/cands/materials/…）
    4) 关键认识：这段代码不含任何“人事业务口径”！分类规则/提示词全在
       skills/archive/{prompts,口径}，由 loader 读取后填进提示词。
       所以“识别不准”绝大多数要去改口径文件，而不是改这里。
"""
from __future__ import annotations

import base64          # 把图片二进制转成可放进 JSON 的文本（给视觉模型看）
import json
import os
import re             # 正则：从模型回答里抠出 JSON
from concurrent.futures import ThreadPoolExecutor, as_completed   # 线程池并发建档
from typing import TypedDict

from langgraph.graph import END, START, StateGraph   # LangGraph：状态机图
from archive.engine import providers, seg            # 模型工厂 + 确定性归组
from archive.domain import classes                    # 目录序（CAT_RANK/date_key/dir_sort_key）
from archive.domain.classes import DEFAULT_SUB_TITLES, SUB_TO_MAIN  # 类名/大类映射
from archive.skill import loader                     # 读提示词/口径
from archive.storage import ocr_cache                # OCR 持久缓存（重跑不重复花钱）

# 合法小类清单（定类提示词里要发给模型看“有哪些类可选”）
CATS = ("一", "二", "三", "四-1", "四-2", "四-3", "四-4", "五", "六", "七",
        "八", "九-1", "九-2", "九-3", "九-4", "十")


class St(TypedDict, total=False):
    """整个图的共享状态（state）长什么样 —— 类似“流水线上的传送带”上有什么。

    total=False：不是每步都填满。各节点往里面加自己的字段：
      records   整卷每页记录（含建档后的 ocr.mark）
      cands     归组产出的“份候选”（按身份分桶，见 seg.py）
      mat       classify 结果：份序号 → {分类/名称/日期}
      materials 装配好的材料行（export 消费）
      issues    问题清单（C0未归类/C1存疑/C2残页待核…）
      orphan_notes resolve 节点产生的说明（并进 issues）
      llm_text / llm_vision  懒加载的文本/视觉模型（避免没用到也去配 key）
      vision_channel 建档用的是哪条模型链的标签（OCR 缓存的键之一，见 node_mark）
      ocr_cached 本轮建档命中 OCR 缓存的页数（给进度文案"省了 N 页"用）
      refresh   用户说了"重新识别" → 跳过缓存查、照常写（见 node_mark）
    """
    records: list
    cands: list
    mat: dict
    materials: list
    issues: list
    orphan_notes: list
    llm_text: object
    llm_vision: object
    vision_channel: str
    ocr_cached: int
    refresh: bool


# ---------------- 小工具（读/规整各字段） ----------------

def _mark(r):
    """从一条 record 取出“建档卡” ocr.mark；没有给空 dict（避免 .get 链爆炸）。

    r.get("ocr") or {} ：ocr 可能没有 → 先给空 dict
    (…).get("mark") or {}：mark 可能没有 → 再给空 dict
    """
    return (r.get("ocr") or {}).get("mark") or {}


def _norm_cat(code):
    """把模型给的“类别字符串”规整成合法小类；None/非法返回 None。"""
    if code is None:
        return None
    from archive.domain.classes import normalize_category   # 规整逻辑都在 classes.py
    return normalize_category(code)


def _extract_json(text):
    """从模型回答里“抠出”第一个 {...} 并解析成 Python dict。

    模型可能前后夹废话（“好的，结果是：{...}”），所以用正则找第一个大括号块。
    re.S 让 . 也能匹配换行（JSON 经常跨行）。
    """
    m = re.search(r"\{.*\}", text, re.S)        # 找第一个 { 到最后一个 } 的整块
    if not m:
        raise ValueError(f"模型未返回JSON: {str(text)[:240]}")  # 没有就报错（截前240字符）
    return json.loads(m.group(0))               # json.loads：字符串 → Python dict


def _tri(v):
    """把模型可能给的各种“布尔”写法收敛成 True/False/None。

    模型 JSON 里 is_first 可能给 true/false/null，也可能漏；这里只认真正的 True/False，
    其它都当 None（= 不确定）。
    """
    return True if v is True else (False if v is False else None)


def _i(v):
    """把“数字”尽量转成 int；空/非数字给 None。

    模型可能给字符串 "2026"，也可能给数字 2026，还可能给空 —— 一网打尽。
    """
    return v if isinstance(v, int) else (
        int(v.strip()) if isinstance(v, str) and v.strip().isdigit() else None)


def _fmt_date(ymd):
    """把模型给的日期 dict 校验/归一成 {y,m,d}；非法给 None。

    校验规则：年必须在 1900~2100、月 1~12、日 1~31；缺月/缺日允许(给 None)。
    目的：模型经常瞎填日期（把身份证号当日期等），这里当“第一道闸”过滤掉明显离谱的。
    """
    if not isinstance(ymd, dict):
        return None
    y = _i(ymd.get("y"))
    if not (y and 1900 <= y <= 2100):           # 年份不在合理范围 → 整条不要
        return None
    m, d = _i(ymd.get("m")), _i(ymd.get("d"))
    return {"y": y,
            "m": m if (m and 1 <= m <= 12) else None,
            "d": d if (d and 1 <= d <= 31) else None}


def _ymd(d) -> str:
    """把 {y,m,d} 印成 "1999-?-?" 这样的短文本；没有就"无"。提示词/事件里显示日期用。"""
    if not (isinstance(d, dict) and d.get("y")):
        return "无"
    return f"{d.get('y')}-{d.get('m') or '?'}-{d.get('d') or '?'}"


def _parse_mark(text: str) -> dict:
    """把建档模型的 JSON 回答解析成标准“内容卡”结构（存进 record.ocr.mark）。

    它把模型给的宽松字段整理成程序好用的格式，并在顶层再铺平 title/date/texts/cls
    方便后面节点直接取。

    ★ 这是一张**白名单**：模型 JSON 里没在这里列出的键会被悄悄丢掉。加字段必须同时改
      这里和 _empty_mark（否则失败卡与成功卡形状不一致，下游读到两种形状更难查）。
    ★ 字段去向：form/pk/pg 是 09-12 为"按身份归组"新加的（照片顺序随机，只能靠它）；
      is_first/is_cont 保留解析**只给回退路径用**（seg 的 _legacy_order_split 在 form
      覆盖率过低时才走），kind 是只写不读的死字段，已删。
    """
    d = _extract_json(text)                     # 先抠出 JSON
    date = None
    if isinstance(d.get("date"), dict):         # 日期若有就按合理范围校验一遍
        y = _i(d.get("date", {}).get("y"))
        if y and 1900 <= y <= 2100:
            m, da = _i(d["date"].get("m")), _i(d["date"].get("d"))
            date = {"y": y,
                    "m": m if (m and 1 <= m <= 12) else None,
                    "d": da if (da and 1 <= da <= 31) else None}
    t = (d.get("t") or "").strip() or None      # 标题（去空白，空→None）
    s = (d.get("s") or "").strip() or None      # 正文要点
    doc = (d.get("doc") or "").strip() or None  # 文号
    form = (d.get("form") or "").strip() or None   # 所属册子/表单（归组的钥匙）
    pk = (d.get("pk") or "").strip() or None       # 册内栏目名
    pg = _i(d.get("pg"))                           # 页面上印刷的页码（没有→None）
    # 组装成“一页内容卡”：结构化 mark + 顶层便捷字段 + 预置一个空 cls(分类)占位
    return {"mark": {"t": t, "doc": doc, "date": date,
                     "form": form, "pk": pk, "pg": pg,
                     "is_first": _tri(d.get("is_first")),
                     "is_cont": _tri(d.get("is_cont")), "s": s,
                     "u": bool(d.get("u"))},
            "title": t, "date": date, "texts": [s] if s else [],
            "cls": {"category": None, "doubt": bool(d.get("u")), "doc": doc,
                    "evidence": s or ""}}


def _empty_mark(reason):
    """建档失败时给一张“空卡”：标记 u=True(doubt)，并带上失败原因。

    单页失败不能删掉这页，否则后面归组/导出顺序就乱了 —— 用空卡占位，让它进待核对。
    形状必须与 _parse_mark 的成功卡一致（同样的键，值给 None）。
    """
    return {"mark": {"t": None, "doc": None, "date": None,
                     "form": None, "pk": None, "pg": None,
                     "is_first": None, "is_cont": None, "s": None, "u": True},
            "title": None, "date": None, "texts": [],
            "cls": {"category": None, "doubt": True, "evidence": reason}}


def _card(rec) -> str:
    """把“一页的内容卡”压成一行短文字（喂给定类提示词时每份给一行）。"""
    mk = _mark(rec)
    d = mk.get("date") or {}
    ds = f"{d.get('y')}-{d.get('m') or '?'}-{d.get('d') or '?'}" \
        if d.get("y") else "无日期"              # 日期有就说，没有就说“无日期”
    return ("表头《%s》；文号[%s]；%s" % (mk.get("t") or "", mk.get("doc") or "", ds)
            + (f"；栏目:{mk.get('pk')}" if mk.get("pk") else "")
            + (f"；要点:{mk.get('s')}" if mk.get("s") else ""))


def _text_llm(state):
    """取（或懒创建）文本模型并放进 state —— 文本便宜快，定类/合并都用它。"""
    if not state.get("llm_text"):
        state["llm_text"] = providers.make_model("text")
    return state["llm_text"]


# ---------------- 节点（图的每一站） ----------------

def _page_writer():
    """取 LangGraph 的"节点内推自定义事件"写入口；拿不到就给个空函数。

    新手视角：正常的节点只能"跑完返回 state"，中途没法往外说话。LangGraph 为此
    留了一个口子 —— 节点里取到 writer 后可以随时 writer(任意 dict)，这些事件会以
    ("custom", payload) 的形式混进 g.stream() 的产出里（见 station_adapter._iter_flow）。

    ★ 两个坑，都实测过：
      1) get_stream_writer() 只在**图的执行上下文里**可用，直接调用 node_mark
         （单测、脚本、别的地方复用）会抛 RuntimeError → 所以必须兜底成空函数。
      2) 它只在 `stream_mode` 里含 "custom" 时才真把事件送出去；**不含则静默丢弃、
         不报错**（驱动方漏写了这个 mode 就会"看起来什么都没发生"，极难查）。
    """
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:                        # noqa：不在图里 → 当没有 writer
        return lambda _ev: None


def _page_event(rec, total: int, cached: bool) -> dict:
    """一页建档完成的事件 —— 给前端"正在识别第 N 张"的进度面板用。

    只放**内容字段**，不放图片 URL / 字节：URL 是"上站"那层的事（由
    station_adapter 补，见 build_runner），而字节更不能放 —— job 每收一个事件
    会把整行重写一次 SQLite，126 张图的内联 base64 会把库和轮询通道一起压垮。
    date 的口径与 _card() 保持一致（只有年 → y-?-?），免得两处显示不一样。
    """
    mk = _mark(rec)
    d = mk.get("date") or {}
    return {"type": "page", "seq": rec.get("seq"), "total": total,
            "title": mk.get("t") or "", "doc": mk.get("doc") or "",
            "date": (f"{d.get('y')}-{d.get('m') or '?'}-{d.get('d') or '?'}"
                     if d.get("y") else ""),
            "summary": mk.get("s") or "", "cached": bool(cached)}


def node_mark(state):
    """建档：对缺 ocr.mark 的页并行视觉建档（读原图字节，不依赖 PIL）。

    每页产一张内容卡(t/doc/is_first/is_cont/date/kind/s/u)，提示词来自 skill。
    这是视觉模型“看一页、读懂一页”的步骤（不判类，只打内容卡）。

    09-06 起接 OCR 持久缓存（storage/ocr_cache）：建档前先查缓存——
    键 = 图片md5 + 通道 + 建档提示词指纹，任一变了键就变（旧缓存自然失效）。
    只有没见过的页才真调视觉模型 → 重跑/重定类/交互修正不再重复花钱。

    09-12 起**每读完一页推一条页级事件**（_page_event）：126 页原本全闷在"建档 18%"
    这一段里，界面只看得到一个百分比；现在每完成一页就往外报"这是第几页、读到了什么"，
    前端据此逐张展示（缩略图 + 表头/日期/文要）。缓存命中的页**也报**（省了钱不等于
    没识别过，用户照样要看这一页的结果）。事件从下面 as_completed 的**主循环**里发，
    不从 6 个 worker 线程里发：一处、按完成顺序、不必赌 writer 的线程安全性。
    """
    recs = list(state["records"])               # 全卷记录（拷一份，别改原 list）
    todo = [r for r in recs if not _mark(r)]    # 只处理“还没建档卡”的页（可断点续跑）
    if not todo:
        return {}                               # 都已建档 → 本节点无事可做（空更新）
    llm = state.get("llm_vision") or providers.make_model("vision")
    if not state.get("llm_vision"):
        state["llm_vision"] = llm               # 记进 state，下个批次复用同一实例

    # —— 缓存三要素里拿得 early 的两样：建档提示词原文 + 通道标签（第三样是每页的 md5）——
    prompt = loader.mark()
    # ★ 优先用调用方注入的链标签（用户「模型配置」里选的建档通道），没有才回落到
    #   .env 的老路子。别改回只用 _pick("vision")：那样用户把建档换成 glm 之后，
    #   缓存键仍写着 .env 里的 qwen，用 glm 读的结果会被 qwen 的请求命中 ——
    #   静默的错误结果（不是崩溃），而且这份缓存在 data/ocr_cache 是全局共享的。
    channel = state.get("vision_channel") or providers._pick("vision")

    def _one(r):
        """对【一页】建档：读原图 → base64 → 发给视觉模型 → 解析成内容卡。

        一张一张来，为了并发(下面 ThreadPoolExecutor 6 路)所以是独立函数。
        先查 OCR 缓存：命中直接用存好的内容卡（0 成本）；未命中才花钱调模型，
        调完顺手 store 进缓存——下一次任何项目里再遇到这张图就免费了。
        """
        # 先查缓存：键 = 图片md5 + 通道 + 提示词指纹（三要素见 ocr_cache.py）
        # ★ state["refresh"]（用户明确说"重新识别一遍"）时**跳过 lookup**，但下面照常
        #   store —— 语义是"这一次真读一遍，读完覆盖这家自己那份缓存"（与 reocr_page
        #   的强制重读同一个套路）。别把它写成 while 删缓存：换一家读是另一套键，
        #   本来就不冲突。
        md5 = r.get("md5") or ""
        if md5 and not state.get("refresh"):
            hit = ocr_cache.lookup(md5, channel, prompt)
            if hit is not None:
                # 命中：返回缓存卡 + 命中标记。★ 这里必须返回 hit 本身（完整包裹卡
                # {mark,title,date,texts,cls}），和下面真调模型路径的 card 同结构——
                # lookup 返回的已经是 _parse_mark 的输出，不能再取 hit["mark"]（那是
                # 内层裸卡）。曾因多剥这层导致：命中缓存的页 ocr 缺 mark 键 →
                # 切份(seg)读到空卡 → 整卷被误判成“一式N份复本”→ 材料 0（09-09 修）。
                return r["seq"], hit, True
        p = r.get("path") or ""                  # 这页的原图绝对路径
        mime = "image/png" if os.path.splitext(p)[1].lower() == ".png" \
            else "image/jpeg"
        try:
            # 图片不能直接发给 HTTP，要转成 base64 文本（data URL 形式）
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            # langchain 多模态消息：一段文字(建档提示词) + 一张图
            out = llm.invoke([{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}}]}])
            card = _parse_mark(str(out.content))   # 模型JSON→结构化内容卡
            # 调完顺手写缓存（没 md5 的怪记录就跳过——安全优先，别为缓存冒险）
            if md5:
                ocr_cache.store(md5, channel, prompt, card,
                                model=getattr(llm, "model_name", ""))
            return r["seq"], card, False
        except Exception as e:                    # noqa
            # 单页失败不拖垮整卷：给一个“建档失败”的空卡，进待核对
            return r["seq"], _empty_mark(f"建档失败 {type(e).__name__}"), False

    updated = {r["seq"]: r for r in recs}         # seq → 记录 的映射（好按 seq 回填）
    # 线程池：6 个建档任务并发跑（视觉调用慢，并发能省大半时间）
    cached_n = 0                                   # 统计本批命中缓存的页数（给进度文案）
    emit = _page_writer()                          # 页级事件出口（图外调用时是空函数）
    total = len(recs)                              # 总页数，给前端拼"第 N / 总数 张"
    with ThreadPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(_one, r) for r in todo]):
            seq, ocr, was_cached = fut.result()   # 每完成一个就取它的 (seq, 内容卡, 命中?)
            if was_cached:
                cached_n += 1
            updated[seq] = {**updated[seq], "ocr": ocr}   # 用 {**dict} 生成新 dict 替换
            # 每读完一页立刻往外报一条（缓存命中的也报）——前端进度面板靠它逐张刷新
            emit(_page_event(updated[seq], total, was_cached))
    # 保持原来的卷序返回（只更新 ocr 字段）；ocr_cached 一并返回——
    # adapter 从 stream 的节点更新里收出它，给前端显示"本次命中缓存 N 页（省了 N 页的钱）"
    return {"records": [updated[r["seq"]] for r in recs], "ocr_cached": cached_n}


def node_segment(state):
    """归组：按【身份】（同册子 + 同日期）把页聚成"份候选"；顺带判一式N份。

    纯确定性，不调 LLM —— 逻辑都在 seg.py（可测试、结果稳定、可解释）。
    ★ 以前这里叫"切份"，看的是照片顺序（相邻两页像不像）；顺序随机后那条路废了，
      现在与"第几张照片"完全无关。节点名保留是为了不动进度面板的节点表。
    """
    cands = seg.build_candidates(state["records"])
    out = {"cands": cands}
    # seg 需要对外说的那句话（例如"form 覆盖率太低，已回退按顺序切份"）在这里转成
    # 待核对项。★ 不能直接写 issues：build_mats 稍后会整体重写 issues，这里写了会丢，
    # 所以走 orphan_notes 这条"先攒着、由 build_mats 合并进 issues"的通道。
    if seg.LAST_NOTE:
        out["orphan_notes"] = [{"level": "warn", "code": "C2-归组降级",
                                "message": seg.LAST_NOTE,
                                "seq": [c["raw_seqs"][0] for c in cands if c.get("raw_seqs")][:1]}]
    return out


def node_resolve(state):
    """残页裁决：把"认不出属于哪本册子"的页交给文本模型，判断它像下面哪一份。

    为什么单独一步：seg 是确定性的，遇到判不出 form 的页**一律不敢并**（猜错会把两本
    册子并成一本，比拆散更难发现）。剩下的十几页交给模型看一眼"它像哪一份"，
    比硬猜稳，而且输出极短（只回答归属、不复述内容），不会重蹈早期"一次生成 103 条
    材料导致读超时"的覆辙。

    ★ 失败安全（三条都要守住）：
      - 只允许并进【已有】的份，不许模型发明新份；
      - 模型回 null / 非法 JSON / 调用抛异常 → **全部保持原样**，一个字都不改；
      - 凡是并进去的，该份一律标 doubt=True 并进 C2 清单（推断永不静默导出，rules §2.6）。
    """
    cands = state.get("cands") or []
    by_seq = {r["seq"]: r for r in state["records"]}
    orphans = [c for c in cands if c.get("orphan")]
    if not orphans:
        return {}                                    # 没有残页 → 本节点无事可做
    hosts = [c for c in cands if not c.get("orphan")]
    notes: list[dict] = list(state.get("orphan_notes") or [])
    if not hosts:                                    # 没有任何可依附的份 → 不用问了
        notes.append({"level": "warn", "code": "C2-残页待核",
                      "message": f"{len(orphans)} 页认不出所属材料，请人工核对",
                      "seq": sorted(c["seqs"][0] for c in orphans)})
        return {"cands": cands, "orphan_notes": notes}

    def _name(c):
        """给一份起个"人能读、模型也能对上"的名字：优先册名，其次代表页表头。

        信纸抬头不算名字（"XX公司稿纸"是印在信纸上的字）—— 拿它当名字会诱使模型
        把不相干的页"并进那份稿纸"，实测出现过。这种份**不参与裁决**（返回 None）。
        """
        if c.get("form"):
            return c["form"]
        mk = _mark(by_seq.get(c["rep"]) or {})
        t = mk.get("t")
        if t and not seg.is_letterhead(t):
            return t
        return None

    hosts = [c for c in hosts if _name(c)]            # 没有像样名字的份不当宿主
    names = {_name(c): c for c in hosts}             # 名字 → 份（模型按名字回话）
    host_lines = "\n".join("- %s：共%d页，日期%s" % (_name(c), c["pages"], _ymd(c.get("date")))
                           for c in hosts)
    llm = _text_llm(state)
    joined: dict[str, list[int]] = {}                 # 份名 → 并进来的页
    failed = 0
    # 分批 ≤30 页（只喂残页，量与真实卷十几页同数量级；批大是为了将来大卷也不爆）
    for i in range(0, len(orphans), 30):
        batch = orphans[i:i + 30]
        lines = "\n".join(
            f"第{c['seqs'][0]}张：表头《{_mark(by_seq[c['seqs'][0]]).get('t') or ''}》"
            f"；栏目:{_mark(by_seq[c['seqs'][0]]).get('pk') or '无'}"
            f"；{_card(by_seq[c['seqs'][0]])}" for c in batch)
        try:
            got = _extract_json(str(llm.invoke(loader.resolve(lines, host_lines)).content))
        except Exception:                             # noqa：解析失败/调用炸 → 这一批不猜
            failed += len(batch)
            continue
        for it in (got.get("attach") or []):
            if not isinstance(it, dict):
                continue
            try:
                seq = int(it.get("seq"))
            except (TypeError, ValueError):
                continue
            to = it.get("to")
            if not to or str(to) not in names:        # null / 名字对不上 → 不并（失败安全）
                continue
            joined.setdefault(str(to), []).append(seq)

    if not joined:                                    # 一个也没并成 → 保持原样，只留说明
        notes.append({"level": "warn", "code": "C2-残页待核",
                      "message": (f"{len(orphans) + failed} 页认不出所属材料，请人工核对"
                                  if failed else f"{len(orphans)} 页认不出所属材料，请人工核对"),
                      "seq": sorted(c["seqs"][0] for c in orphans)})
        return {"cands": cands, "orphan_notes": notes}

    moved = {s for v in joined.values() for s in v}
    out: list[dict] = []
    for c in cands:
        if c.get("orphan"):
            continue                                  # 残页下面按"并进哪份"重新落位
        add = joined.get(_name(c))
        if add:
            merged = seg.reorder(c["seqs"] + add, state["records"], c.get("form"))
            c = {**c, "seqs": merged, "pages": len(merged),
                 "raw_seqs": sorted(c["raw_seqs"] + add), "doubt": True}
            notes.append({"level": "warn", "code": "C2-残页已并入",
                          "message": f"第{'、'.join(map(str, sorted(add)))}张按内容并入"
                                     f"『{_name(c)}』，请核对",
                          "seq": sorted(add)})
        out.append(c)
    leftovers = [c for c in cands if c.get("orphan") and c["seqs"][0] not in moved]
    out.extend(leftovers)
    if leftovers:
        notes.append({"level": "warn", "code": "C2-残页待核",
                      "message": f"{len(leftovers)} 页认不出所属材料，请人工核对",
                      "seq": sorted(c["seqs"][0] for c in leftovers)})
    out.sort(key=classes.dir_sort_key)                # 与 seg 用同一套目录序
    for i, c in enumerate(out, 1):
        c["idx"] = i                                  # 并完重新编号（份号是 classify 的钥匙）
    return {"cands": out, "orphan_notes": notes}


def node_classify(state):
    """定类：每份取首页卡，分批(≤30/次)文本模型判(小)类；提示词走 skill(含文种口径)。

    结果 {id→分类/名称/日期} 存 state.mat。文本模型读“内容卡”定类，比视觉便宜得多。
    """
    llm = _text_llm(state)                        # 文本模型（懒创建）
    by_seq = {r["seq"]: r for r in state["records"]}   # 方便按首页 seq 找记录拿内容卡
    items: list[dict] = []
    cands = state["cands"]
    # 造一份“类表”给模型看：九-1=工资类材料；… 让模型只能从合法集里选
    subs = "；".join(f"{s}={DEFAULT_SUB_TITLES.get(s, s)}" for s in CATS)
    # 分批：一次最多喂 30 份（太多会让模型输出超长→读超时，见 calibration 记录）。
    # range(0, len, 30) 产生 0,30,60… 步进；每次取 [i:i+30] 这份“切片”。
    for i in range(0, len(cands), 30):
        sub = cands[i:i + 30]
        # 把“这一批每份”压成几行给模型看：份号、页数、一式N份、**册名与栏目**、代表页内容卡。
        # 册名很重要：册子内页的表头只是栏目名（"工作经历"），光看它会定错文种；
        # 有了"册:干部履历表"这一句，模型才知道这份材料整体是什么。
        lines = [f"第{c['idx']}份：共{c['pages']}页"
                 + (f"，另有相同{c['copies'] - 1}份" if c["copies"] > 1 else "")
                 + (f"；册:{c['form']}" if c.get("form") else "")
                 + "；代表页：" + _card(by_seq[c["rep"]])
                 for c in sub]
        prompt = loader.classify("\n".join(lines), subs)   # 渲染定类提示词（口径来自 skill）
        got = _extract_json(str(llm.invoke(prompt).content))  # 模型回 JSON → 解析
        items.extend(got.get("items") or [])   # 攒下所有份的定类结果，下面统一转 mat
    mat = {}
    for it in items:                            # items 是一行行 {id, category, ...}
        try:
            mat[int(it.get("id"))] = it         # 以“份序号”为键存起来（int 转换防模型乱写）
        except (TypeError, ValueError):
            pass                                # id 不是数字就当坏数据跳过
    return {"mat": mat}


def node_build_mats(state):
    """产出材料：把 classify 结果 + 份候选装配成 materials(兼容 export)；未定类页进 C0 清单。

    materials = 一条条“正式材料行”（export 吃它出目录）；issues = 给人工看的待核对项。
    """
    by_seq = {r["seq"]: r for r in state["records"]}
    mat = state.get("mat", {})
    materials, issues = [], []
    covered: set[int] = set()                   # 记“哪些页已经被某份覆盖”，避免重复
    for c in state["cands"]:                    # 逐份候选
        it = mat.get(c["idx"]) or {}            # 取这份的定类结果（没有给空 dict）
        cat = _norm_cat(it.get("category"))     # 规整类别（非法→None）
        # ★ 取"代表页"的内容卡，不是"照片号最小的那页"——照片顺序随机后，最小号那页
        #   可能是册子中间的栏目页（表头写的"工作经历"），拿它当材料名就错了。
        mk = _mark(by_seq[c["rep"]])
        title = _title_of(c, it, mk)
        date = _fmt_date(it.get("date")) or c.get("date") or mk.get("date") or None
        members = list(c["seqs"])               # ★ 已是装订序，别再 sorted()（那等于按照片号重排）
        dup = sorted(s for g in c["dup_groups"] for s in g)   # 一式N份的重复页（拍平）
        covered |= set(members) | set(dup)      # 这些页都算被覆盖了
        if cat is None:                         # 这份没定出类 → C0 未归类，进问题清单
            issues.append({"level": "error", "code": "C0-未归类",
                           "message": f"第{members[0]}张所在份未定类"
                                      f"（{it.get('reason') or ''}）",
                           "seq": members})
            continue                            # 不生成材料行，下一份
        # 正常：组装成一条“材料行”（字段契约见 domain/models.py 的 MaterialRow）
        materials.append({
            "seq": c["idx"], "uid": f"m{c['idx']}", "category": cat,
            "main": SUB_TO_MAIN.get(cat) or (cat.split("-")[0] if "-" in cat else cat),
            "title": title, "title_src": "engine" if title else "unknown",
            "date": date, "copies": c["copies"], "pages": len(members),
            "members": members, "dup_pages": dup,
            "assigned_pages": members + dup,            # 归属页=本体+重复份
            "raw_seqs": sorted(c.get("raw_seqs") or members),   # 照片号（找实物用）
            "rep": c["rep"], "form": c.get("form"),
            "evidence": (it.get("reason") or mk.get("s") or "").strip()[:200],
            "doubt": bool(it.get("doubt")) or bool(c.get("doubt")),
            "verdict": "ok"})
        if it.get("doubt"):                             # 定类存疑的另加一条 C1 警告
            issues.append({"level": "warn", "code": "C1-归类存疑",
                           "message": f"{cat}『{title or '未命名'}』待核对",
                           "seq": members})
        if c.get("doubt"):                              # 归组存疑（同类册桶/残页并入）
            issues.append({"level": "warn", "code": "C3-归组存疑",
                           "message": f"{cat}『{title or '未命名'}』的页归属需核对",
                           "seq": members})
    # 兜底：还有没被任何份覆盖的页 → 也报 C0（防止漏页不提醒）
    for r in sorted(state["records"], key=lambda x: x["seq"]):
        if r["seq"] not in covered:
            issues.append({"level": "error", "code": "C0-未归类",
                           "message": f"第{r['seq']}张未被覆盖", "seq": [r["seq"]]})
    issues.extend(state.get("orphan_notes") or [])      # seg/resolve 攒下的说明并进来
    # ★ 按目录序（一→十 → 类内按成题时间）排材料行：交互层的 _reseq 会按这个顺序
    #   编"类内序号"，于是 Excel/PDF/全景卡三处天然一致（rules §5.1）。
    materials.sort(key=classes.dir_sort_key)
    return {"materials": materials, "issues": issues}


def _title_of(c, it: dict, mk: dict):
    """定这份材料的【名称】——优先级里藏着一个坑，别改错顺序。

    册子内页的 `t` 是**栏目名**（"工作经历""誓词"），不是材料名；直接抄它会出现
    一堆叫"工作经历"的材料。所以顺序是：
      ① 模型定类时给的名称（它看过册名，最可信）
      ② 代表页表头 —— 但只在这个表头**就是册名**时才用（封面页那种）
      ③ 册子清单里的规范材料名（如"干部履历表"）
      ④ 兜底：这份里第一个非空表头
    """
    t = (it.get("title") or "").strip() or None
    if t:
        return t
    if c.get("form"):
        books = loader.formbooks()
        spec = (books.get(c["form"]) or {}).get("title") or c["form"]
        rt = (mk.get("t") or "").strip()
        if rt and (rt == spec or rt == c["form"]):
            return rt                                   # 封面页：表头就是册名
        return spec                                     # ③ 用规范材料名
    rt = (mk.get("t") or "").strip()
    if seg.is_letterhead(rt):                           # 信纸抬头不是材料名
        rt = ""
    return rt or None                                   # ④ 散页兜底


def reocr_page(record: dict, channel: str | None = None, force: bool = False) -> dict:
    """换一家 OCR（视觉通道）读一页，返回新旧两份内容卡供对比。

    用途：某页建档结果可疑时（用户说"这页读得不对/换一家读读看"），用另一家重读。

    ★ 09-12 起**优先用那一家已有的缓存**（除非 force=True）：
      以前这里是无条件真调模型 —— 可"换一家对比"往往只是想看看**别家早就读过、
      已经存在缓存里**的那份结果，为看一眼再烧一次钱没有道理。
      没缓存时才真读，读完照常写缓存（键带通道名，两家结果天然共存互不覆盖）。
      force=True 走老路（"就是要新鲜读一次"），保留给将来需要"重读再对比"的场景。

    返回 {"old": 旧卡或None, "new": 新卡, "channel": 通道名, "cached": 是否直接用缓存}；
    旧卡取自该页当前 record.ocr.mark。
    """
    from archive.storage import ocr_cache
    p = record.get("path") or ""
    md5 = record.get("md5") or ""
    prompt = loader.mark()
    # 不指定 channel → 自动挑"当前通道以外的那家"（有 key 的里面选）
    if channel is None:
        channel = _other_vision_channel()
    old = (record.get("ocr") or {}).get("mark") or None
    if md5 and not force:
        hit = ocr_cache.lookup(md5, channel, prompt)
        if hit is not None:
            return {"old": old, "new": hit, "channel": channel, "cached": True}
    llm = providers.make_model("vision", channel=channel)
    mime = "image/png" if os.path.splitext(p)[1].lower() == ".png" else "image/jpeg"
    with open(p, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    out = llm.invoke([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}])
    card = _parse_mark(str(out.content))
    if md5:
        ocr_cache.store(md5, channel, prompt, card,
                        model=getattr(llm, "model_name", ""))
    return {"old": old, "new": card, "channel": channel, "cached": False}


def _other_vision_channel() -> str:
    """挑一个"当前视觉通道以外"的通道名（换一家对比用）。

    遍历 PROVIDERS 里有哪些家；每家只挑"配了 key 的"。全没配/只有一家 →
    退回当前通道（等价于"没有第二家可换"，调用方据此提示）。
    """
    cur = providers._pick("vision")
    for name in providers.PROVIDERS:
        if name == cur:
            continue
        # 探一下这家配没配 key：没 key 的家进了也白搭（调用必炸）
        try:
            providers.make_model("vision", channel=name)
            return name                       # 有 key → 就它了
        except Exception:                     # noqa：没 key/不可用 → 看下一家
            continue
    return cur                                # 没有第二家 → 原样返回（调用方提示）


def build_graph():
    """把 5 个节点连成一张“有向图”，编译后返回可调用的执行器。

    执行器被 adapter 用 graph.stream/invoke 驱动；图里数据流的先后顺序就在这定义。

    ★ 09-12 把 consolidate（二次合并）换成了 resolve（残页裁决）：
      归组改成"按身份分桶"后，同一份的页在第二站就已经聚齐，"事后两两问模型要不要并"
      那一步既没必要（还有跨批永不合并的硬伤），真正剩下的尾巴是"认不出身份的那十几页"。
    """
    g = StateGraph(St)                              # 建图：状态类型 St（见上方）
    # 注册 5 个“站点”
    g.add_node("mark", node_mark)                   #   建档(视觉)
    g.add_node("segment", node_segment)             #   归组(规则,按身份)
    g.add_node("resolve", node_resolve)             #   残页裁决(文本,小输出)
    g.add_node("classify", node_classify)           #   定类(文本,分批)
    g.add_node("build_mats", node_build_mats)       #   装配材料
    # 连线：START → mark → segment → resolve → classify → build_mats → END
    g.add_edge(START, "mark")
    g.add_edge("mark", "segment")
    g.add_edge("segment", "resolve")
    g.add_edge("resolve", "classify")
    g.add_edge("classify", "build_mats")
    g.add_edge("build_mats", END)
    return g.compile()                              # 编译成可直接 invoke 的执行器
