"""运行前装配全局/MCP/技能工具；内核不认识工具来源。"""
from station.core.tool import Tool
from station.tools import GLOBAL_TOOLS

GLOBAL_TOOL_CAP = 3
GLOBAL_TOOL_CAPPED = (
    "（`{name}` 这一轮已经调用过 {cap} 次，先停下 —— 它**不会改变你手上的工具**。"
    "如果用户要的能力不在你的可用工具里，就**直接说明并建议他换个说法**，别继续查。）")

def tools_for(skill, user_id: str = "") -> list[Tool]:
    """本次会话能用的工具 = **全局工具 + 外部 MCP 工具 + 该技能的工具**（按全名去重）。

    为什么全局的排在前面：模型看 schema 的顺序会影响它选工具的偏好，日常小事
    （读个文件、记一条）应该比技能特有的重操作更容易被想到。MCP 工具排在全局工具
    之后、技能工具之前 —— 它们也是"通用能力"（搜索、查库），只是来自外部。

    ★ **MCP 工具不走路由**（09-16 用户拍板）：路由判的是"这句话属于哪个业务领域"，
      而"要不要搜索"是模型自己的判断。第一版把它们做成技能去路由，实测用户问
      "今天北京的天气"判词判不出来、工具当场从模型手上消失（见 mcp/bridge.py 文件头）。

    为什么要去重：通用聊天（GenericAgent）的 tools 本身就是那份全局工具
    （chat.routing.agent_for_tool 反查时要靠它），合并时不判重就会出现两份同名 schema。
    """
    out: list[Tool] = []
    seen: set[str] = set()
    extra: list[Tool] = []
    try:
        from station.mcp import bridge
        extra = bridge.mcp_tools(user_id)   # 没有 user_id 时它自己返回空（见该函数注释）
    except Exception:                       # noqa：MCP 那侧坏了绝不拖垮对话（顶多少几个工具）
        extra = []
    from station.tools import web
    web_enabled = web.available(user_id)
    for t in [*GLOBAL_TOOLS, *extra, *(getattr(skill, "tools", None) or [])]:
        if t.owner == "web" and not web_enabled:
            continue
        if t.name in seen:
            continue
        seen.add(t.name)
        out.append(t)
    return out

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
        from station.tasks.executor import get_manager
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
