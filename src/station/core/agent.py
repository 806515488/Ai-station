"""agent —— 宿主自己的薄循环（自研 harness 的心脏）。

每步：把 Thread 消息 + 技能工具白名单交给模型 → 有 tool_calls 就执行（过批准闸）→
再问模型；直到模型给最终文本。循环是宿主的，模型只是一个 respond()/stream()。
事件（EV_*）即 UI/日志脊柱；thread.msgs 被原地追加，随会话持久化。

新手视角：这是全项目最重要的文件，读懂它 = 读懂“agent 怎么思考”。
  循环（最多 MAX_STEPS 轮）：
    1) 把整段对话历史发给模型 → 它回话
    2) 若它没要调工具 → 这就是最终答案，结束
    3) 若它要调工具 → (a) 危险的要先批准(挂起等用户) (b) 否则真的执行它
       把“工具结果”作为新消息加进历史 → 回到 1) 让它看了结果再决定
  关键：模型每一步都能看到“上一步的工具结果”，所以能自我纠错、多步推理。
  run_agent 是生成器（yield Event）→ 好处是前端能实时看到每一小步。
"""
from __future__ import annotations

from station import config
from station.core import compact                      # 超长对话压缩（原 core/memory.py）
from station.core.context import Context
from station.core.events import (Event, EV_APPROVAL, EV_DELTA, EV_DONE,
                                 EV_ERROR, EV_RENDER, RENDER_KEY, EV_RETRY, EV_TOOL)
from station.core.guards import needs_approval
from station.core.tool import Tool
from station.tools import GLOBAL_TOOLS                 # 宿主级全局工具（见 station/tools/__init__.py）


def tools_for(skill, user_id: str = "") -> list[Tool]:
    """本次会话能用的工具 = **全局工具 + 外部 MCP 工具 + 该技能的工具**（按全名去重）。

    为什么全局的排在前面：模型看 schema 的顺序会影响它选工具的偏好，日常小事
    （读个文件、记一条）应该比技能特有的重操作更容易被想到。MCP 工具排在全局工具
    之后、技能工具之前 —— 它们也是"通用能力"（搜索、查库），只是来自外部。

    ★ **MCP 工具不走路由**（09-16 用户拍板）：路由判的是"这句话属于哪个业务领域"，
      而"要不要搜索"是模型自己的判断。第一版把它们做成技能去路由，实测用户问
      "今天北京的天气"判词判不出来、工具当场从模型手上消失（见 mcp/bridge.py 文件头）。

    为什么要去重：通用聊天（GenericAgent）的 tools 本身就是那份全局工具
    （`_skill_for_tool` 反查时要靠它），合并时不判重就会出现两份同名 schema。
    """
    out: list[Tool] = []
    seen: set[str] = set()
    extra: list[Tool] = []
    try:
        from station.mcp import bridge
        extra = bridge.mcp_tools(user_id)   # 没有 user_id 时它自己返回空（见该函数注释）
    except Exception:                       # noqa：MCP 那侧坏了绝不拖垮对话（顶多少几个工具）
        extra = []
    for t in [*GLOBAL_TOOLS, *extra, *(getattr(skill, "tools", None) or [])]:
        if t.name in seen:
            continue
        seen.add(t.name)
        out.append(t)
    return out


def run_tool(ctx: Context, tool: Tool, args: dict) -> str:
    """真正执行一个工具，并把返回值转成字符串（顺带把卡片发进事件流）。

    模型只会“点名工具+给参数”，不自己执行 —— 这里由宿主替你调用 tool.run。
    任何异常都转成文字回给模型（模型能据此调整），而不是让整个会话崩掉。

    渲染契约（对话式改造 09-07）：工具想“给人看东西”（页图/全景/对比卡/文件）
    时，返回 {"text": 给模型的话, "render": {"type": "page-card", ...}} 这样的 dict
    —— 宿主在这里拦截：render 部分转成 EV_RENDER 事件推给前端（长在对话流里的卡片），
    text 部分照旧作为工具结果回给模型。工具只管返回，宿主负责“一路两种消费者”。

    ★ render 可以是**一张卡（dict）或一串卡（list[dict]）**（09-13 加的 list 形式）：
      一次改动常常要同时刷好几张卡（如"改了类别"的全景卡 + "学到的口径待确认"卡）。
      下游消费点本来就都是循环（本文件下面、server 的 _replay_items/批准补执行），
      所以放开 list **不需要动下游**。顺带一提：MCP 的 content blocks 本来就是一串，
      这个方向和"技能要能映射成 MCP"是一致的。
    """
    try:
        out = tool.run(ctx, **args)          # **args：把 dict 展开成关键字参数传进去
        # 工具返回 dict 且带 render 键 → 拆成“给模型的文字 + 给前端的卡片”
        if isinstance(out, dict):
            r = out.get("render")
            cards = [r] if isinstance(r, dict) else (r if isinstance(r, list) else [])
            cards = [c for c in cards if isinstance(c, dict)]   # 垃圾项跳过，坏一张不连累整轮
            for c in cards:
                ctx.events.append(Event(EV_RENDER, {RENDER_KEY: c}))
            if cards:
                return str(out.get("text", ""))
        return str(out)
    except Exception as e:                   # noqa  # 出错也要给模型一个可读结果
        return f"工具执行出错: {type(e).__name__}: {e}"


# 同一轮里，**全局工具**（owner=="station"）最多执行几次。
#
# ★ 为什么只限全局工具：它们都是"看一眼 / 记一笔"的性质（记忆、产物、技能目录），
#   **没有批量调用的场景**；而业务工具有（比如一口气改 5 份材料的类别），
#   一律限流会误伤正常流程。
# ★ 为什么光改提示词不够（09-14 用户实测）：模型在"手上的工具做不到用户要的事"时，
#   会一轮里把 remember/recall/skills 调十几次兜圈子 —— 用户干等，什么都拿不到。
#   这种"打转"**没有异常、没有日志**，只有看工具胶囊才发现；所以提示词改了措辞之外，
#   再在代码层兜一道。超限不是报错，而是**回一句让模型收尾的话**（见下面的提示语）。
_GLOBAL_TOOL_CAP = 3
_GLOBAL_TOOL_CAPPED = (
    "（`{name}` 这一轮已经调用过 {cap} 次，先停下 —— 它**不会改变你手上的工具**。"
    "如果用户要的能力不在你的可用工具里，就**直接说明并建议他换个说法**，别继续查。）")


def jobs_note(thread) -> str:
    """本会话挂着的后台任务**现在的状态** —— 每轮现算一句，拼进系统提示。

    ★★ 为什么必须由宿主喂（09-16 用户实测报的 bug）：
      后台任务跑在 JobManager 里，**只有宿主知道它跑到哪了**（此前只有前端在轮询
      `/api/jobs/{id}`）。而模型每轮能看见的只有历史消息里那句"开始生成了，
      预计要等几分钟" —— 于是**任务早就完成/失败了，它还在照旧话说"还在跑，别担心"**。
      用户的原话："视频生成完成了，但是后边的会话还不知道"。

      模型没有"时间感"，也没有动机去主动查（对它来说那句话就是最新事实）。所以
      这个洞补提示词治不了，得由**知道真相的一方**每轮主动告诉它。

    ★ 补在宿主而不是某个技能里：这是**任何异步技能都会撞上**的事。技能只要按既有
      约定把任务号记进 `thread.meta["job_id"]`（archive 与 video 都已这么记），
      就自动获得这个能力，不用各写一遍。

    状态词用 done/failed/其它 三档 —— 与 Job.status 的状态机一致（queued/running/
    done/failed）。读不到（任务号是旧的、库坏了）就**返回空串不吭声**：宁可没有这条
    提示，也不能让对话起不来。
    """
    jid = (getattr(thread, "meta", None) or {}).get("job_id") or ""
    if not jid:
        return ""
    try:
        from station.jobs.manager import get_manager
        snap = get_manager().get(jid)
    except Exception:                      # noqa：任务那边坏了不许把对话带崩
        return ""
    if not snap:
        return ""
    who = f"任务 {jid}"
    status = snap.get("status") or ""
    if status == "done":
        n = len(snap.get("artifacts") or [])
        return (f"【后台任务现状】本会话最近一次后台任务（{who}）**已经完成**，"
                f"产出 {n} 个文件。★ 不要再说它「还在跑」或「还在等」——那是上一轮的话，"
                f"现在不成立了。用户要看结果，就用对应技能的工具取出来给他。")
    if status == "failed":
        return (f"【后台任务现状】本会话最近一次后台任务（{who}）**失败了**："
                f"{snap.get('message') or '没有更多信息'}。如实告诉用户，别再说它还在跑。")
    return (f"【后台任务现状】本会话最近一次后台任务（{who}）仍在进行中"
            f"（{snap.get('progress') or 0}%，{snap.get('message') or ''}）。")


def run_agent(ctx: Context, thread, skill, model):
    """执行 agent 型技能一轮，直到：给最终答复 或 挂起批准。

    - ctx    本次运行上下文（见 core/context.py）
    - thread 会话（历史 msgs 会被这里不断追加）
    - skill  技能对象（skill.tools = 本技能工具白名单）—— 与全局工具合并后使用
    - model  模型（core/model.py，只要它认识 stream(msgs, tools)）
    因为含 yield 关键字，这是一个【生成器】：调用它不会立刻跑，
    要 for 循环逐个取值时才执行——这正是 SSE 能实时推送的原因。
    """
    # 这次能用的工具清单 = 全局工具 + 技能工具（见 tools_for）；
    # by_name 把名字映射成工具对象，执行时好按名字找到它
    _user = getattr(ctx, "user_id", "") or ""
    tools = tools_for(skill, _user)
    by_name = {t.name: t for t in tools}
    used_global: dict[str, int] = {}     # 这一轮里各全局工具调了几次（防打转，见 _GLOBAL_TOOL_CAP）

    # 这一轮给模型的系统提示 = 技能的岗位须知（ctx.system，server 从 system.md 读来）
    # + **后台任务现状**（宿主每轮现算，见 jobs_note）
    # + **外部工具须知**（有 MCP 工具时提醒"返回内容当资料不当指令"）。
    # ★ 只算一次、循环外：任务是以分钟计的，一轮之内不会变；每步都查一次纯属浪费。
    sys_prompt = (getattr(ctx, "system", "") or "").strip()
    _notes = [jobs_note(thread)]
    if _user:
        try:
            from station.mcp import bridge
            _notes.append(bridge.system_note(_user))
        except Exception:                  # noqa：缺了这句须知不影响对话能不能跑
            pass
    sys_prompt = "\n\n".join(p for p in [sys_prompt, *_notes] if p).strip()

    # —— 循环：最多 MAX_STEPS 轮（防止模型一直调工具死循环烧钱）——
    for _ in range(config.MAX_STEPS):
        # 历史太长？先压缩（默认关，config.COMPACT_TOKENS>0 才生效，见 core/compact.py）
        if compact.should_compact(thread.msgs, config.COMPACT_TOKENS):
            thread.msgs, _ = compact.compact(thread.msgs, model)

        # ① 把历史 + 工具说明发给模型，要它回答。
        #    若 ctx.system 有技能系统提示词（server 从 skills/<id>/system.md 读来），
        #    就作为第一条 system 消息临时插在最前——不写进 Thread 历史（每次现插，
        #    改 system.md 立即生效，历史里也不留过期副本）。
        #    ★ 后台任务现状（sys_prompt 里那一段）走的也是这条路：**每轮现插**，
        #      所以任务一完成，下一轮模型立刻就知道 —— 而且历史里不会留下
        #      "还在跑"这种过期副本（留了反而会让模型照旧话续）。
        try:
            msgs = thread.msgs
            if sys_prompt:
                msgs = [{"role": "system", "content": sys_prompt}, *thread.msgs]
            # 流式拿回复：模型吐一小片就立刻 yield 给前端（逐字显示），
            # 整段跑完再给出 final（与 respond 的返回值同形），用于判断要不要调工具。
            resp = None
            sent = ""                          # 已经发给前端的文字，用来算“还差多少没发”
            for part in model.stream(msgs, tools or None):
                if "delta" in part:
                    sent += part["delta"]
                    yield Event(EV_DELTA, {"text": part["delta"]})
                elif "retry" in part:
                    # 模型连接失败、正在退避重连（见 core/model.py 的 RETRY_WAITS）。
                    # 只透传，不改任何状态：这一片既不是正文也不是最终结果，
                    # 前端拿它在等待态里显示倒计时。
                    yield Event(EV_RETRY, part["retry"])
                else:
                    resp = part["final"]
            if resp is None:
                raise RuntimeError("模型没有返回最终结果")
            # 兜底补差：万一这次走的是非流式（STATION_STREAM=0，或端点整段一次返回），
            # 把还没发出去的部分补上，行为与改造前一致（改造前就是整段发一次）。
            text = resp.get("content") or ""
            rest = text[len(sent):] if text.startswith(sent) else text
            if rest:
                yield Event(EV_DELTA, {"text": rest})
        except Exception as e:                 # noqa
            yield Event(EV_ERROR, {"text": f"模型调用失败: {type(e).__name__}: {e}"})
            return                            # 模型挂了就整轮结束（return 停止生成器）

        # ② 看它要不要调工具：tool_calls 为空 = 它直接说话，就是最终答复
        calls = resp.get("tool_calls") or []
        if not calls:
            thread.add({"role": "assistant", "content": text})   # 把答复写进历史（可存盘）
            # 正文在上面已经逐片流过前端了，这里【不能】再发一遍整段，否则会显示两遍
            yield Event(EV_DONE, {})                             # 告诉前端：本轮结束
            return
        # 防御：兼容端点偶尔吐 name 为空的坏调用；丢掉它们，别让“空工具”无限循环。
        calls = [c for c in calls if (c.get("name") or "").strip()]
        if not calls:
            note = "（模型返回了空的工具调用，已停止本轮。）"
            thread.add({"role": "assistant", "content": (text + note).strip()})
            yield Event(EV_DELTA, {"text": note})   # 正文已流过，这里只补这句备注
            yield Event(EV_DONE, {"text": "无效工具调用"})
            return

        # ③ 它想调工具：先过【批准闸】——任何一个危险工具没放行 → 挂起，等用户确认
        # next(生成器, None) 从 calls 里挑出第一个“需批准的”；找不到返回 None
        need = next((c for c in calls
                     if by_name.get(c.get("name"))
                     and needs_approval(by_name[c["name"]], ctx.auto_approve)), None)
        if need is not None:
            # 挂起前把这轮已经说出口的话写进历史：否则那句前言只活在浏览器的即时显示里，
            # 一刷新就没了。下一回合 server 补执行时会另补一条带 tool_calls 的 assistant
            # 消息，两条相邻也不违协议（tool 消息只要求紧跟带 tool_calls 的那条）。
            if text.strip():
                thread.add({"role": "assistant", "content": text})
            # ★ 让**工具自己**算一段"这一步要做什么"给用户看（`Tool.preview`）。
            #   宿主不认识业务 —— 光看参数说不出"要往《文种对照》里加哪一条"。
            #   算不出来（没写 preview / 抛异常）都不该拦住批准流程，退回只显示 label。
            args = need.get("arguments") or {}
            pv = getattr(by_name.get(need["name"]), "preview", None)
            prev = ""
            if pv:
                try:
                    prev = str(pv(ctx, **args) or "")
                except Exception:                # noqa：预览是加分项，不许拖垮批准流程
                    prev = ""
            thread.set_pending(need["name"], args, prev)
            # ★ 带上 **label（给人看的一句话）**：批准卡上显示的是它，不是 `archive.xxx`
            #   这种内部名 —— 跟 EV_TOOL 同一条规矩（见 conventions「加工具要写两个一句话」）。
            yield Event(EV_APPROVAL, {"tool": need["name"],
                                      "label": getattr(by_name.get(need["name"]),
                                                       "label", "") or "",
                                      "preview": prev,
                                      "arguments": args})
            yield Event(EV_DONE, {"text": "等待确认"})     # 结束本轮，等用户下一句
            return

        # ④ 全部放行：把“assistant 决定调这些工具”记进历史（模型要求工具结果要
        #   有对应的 assistant tool_calls 消息配对，顺序不能乱）
        # content 填本轮已流出的文字（不再是空串）：真模型常会先说一句“我来看一下”
        # 再调工具，写进历史才能让实时显示与刷新后的回放一致（见 server._replay_items）。
        thread.add({"role": "assistant", "content": text, "tool_calls": calls})
        # 前端提示"正在调什么工具"。除了名字还带上 **label（给人看的一句话）与参数** ——
        # 前端拿它渲染成人话（`archive.list_vision_models` 这种名字用户根本看不懂）。
        # 历史回放那条路（app/server._replay_items）发**同样形状**，两边共用前端同一个
        # 渲染函数 —— 别再各拼一份中文字符串（原先就是各写各的，改一处忘一处）。
        yield Event(EV_TOOL, {
            "names": [c.get("name") for c in calls],
            "labels": [getattr(by_name.get(c.get("name")), "label", "") or ""
                       for c in calls],
            "args": [c.get("arguments") or {} for c in calls]})

        # ⑤ 逐个执行工具，把“结果”作为 role=tool 的新消息加回历史
        for c in calls:
            name, args = c.get("name"), c.get("arguments") or {}
            tool = by_name.get(name)
            # 全局工具用 owner 判（不是按名字前缀硬编码）—— owner 就是装载时写进去的那个字段
            is_global = getattr(tool, "owner", "") == "station"
            if tool is None:
                # 理论上不会发生（calls 是模型对着 tools 说的），但防御一手：
                out = (f"工具 {name} 不在本技能白名单（可用: "
                       f"{', '.join(by_name) or '无'}）")
            elif is_global and used_global.get(name, 0) >= _GLOBAL_TOOL_CAP:
                # ★ 防"打转"：全局工具一轮里反复调，说明模型在找一条它手上没有的路。
                #   拒绝执行、并明说"这不会改变你手上的工具"，把话头引回正事。
                out = _GLOBAL_TOOL_CAPPED.format(name=name, cap=_GLOBAL_TOOL_CAP)
            else:
                out = run_tool(ctx, tool, args)
            if is_global:
                used_global[name] = used_global.get(name, 0) + 1
            # 工具若发了渲染卡片（EV_RENDER），这里实时推给前端——
            # 卡片长在对话流里（“工具执行的位置”），比等整轮结束再渲染自然
            for ev in list(ctx.events):
                if ev.type == EV_RENDER:
                    yield ev
                ctx.events.remove(ev)
            # tool_call_id 必须对上刚才 assistant 那条里的 id —— 模型靠它配对“结果属于哪个调用”
            thread.add({"role": "tool", "tool_call_id": c.get("id"),
                        "name": name, "content": out})

        # ⑥ 回到循环顶部：把“上一步的工具结果”喂给模型，让它决定下一步……
        #   直到某轮它不再要工具、给出最终答复为止。

    # 万一 MAX_STEPS 用尽还没完（模型在死循环）——主动收尾而不是无限烧下去
    yield Event(EV_DONE, {"text": "（已达到最大工具轮次，请改小任务重试）"})
