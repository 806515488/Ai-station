"""统一对话的能力目录与通用助手；不依赖 HTTP 层。

原则：**技能工具不是全局的**。路由阶段只读每个技能的名称/描述（轻量目录），
命中某个技能后才把该技能的 tools 交给模型，其余技能的工具完全不进上下文。
`SkillRef` 就是路由用的“目录条目”；`GenericAgent` 是没有业务工具时的通用聊天。

**全局工具是另一层**：长期记忆、看产物及按用户配置启用的网页检索挂在宿主上，
见 station/tools/。合并发生在
`chat/tool_sources.tools_for()`：技能工具按路由给，基础工具常驻，网页工具按用户配置给。

新手视角：把每个技能看成一本工具手册。入口只放“书名和目录”让路由翻，
真要做某本书里的操作时，才把那本书的“工具章”打开递给模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillRef:
    """路由层看到的“技能目录条目”：只有元信息，不携带工具实现。"""
    id: str = ""
    ns: str = ""
    name: str = ""
    description: str = ""
    hints: str = ""          # 用户常见话术锚点（由工具用途提炼，路由判词用）


@dataclass
class GenericAgent:
    """没命中任何技能时的通用聊天 agent（**带全局工具，不带业务工具**）。"""
    id: str = "station"
    ns: str = "station"
    name: str = "对话"
    description: str = "station 通用对话"
    model: str = "text"
    keys: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    system: str = ""


GENERIC_SYSTEM = (
    "【当前角色：通用助手】\n"
    "本轮负责日常问答、写作、技术讨论和工作站介绍。"
    "需要执行操作时，使用本轮实际提供的工具。"
)


def build_generic_agent() -> GenericAgent:
    """通用聊天外观：**只带全局工具**，不带 archive./weekly. 的工具。

    为什么要带全局工具（09-12 补）：不带的话，没命中技能时模型手上一件工具都没有
    —— "把这段记下来""看看我的产物"只能干说。全局工具与业务无关，带上是纯赚；
    业务工具仍然严格按路由给（见本文件开头的原则）。

    （approve 类全局工具挂起后的补执行也依赖这里：routing.agent_for_tool
    按 ns 反查时，`station.` 就落到这个 agent 上。）
    """
    from station.tools import all_tools
    return GenericAgent(tools=all_tools(), system=GENERIC_SYSTEM)


def build_skill_catalog() -> list[SkillRef]:
    """列出可路由的 agent 技能描述（只读 name/desc，不给模型工具 schema）。

    ★ 目录里**只有磁盘上的技能**：外部 MCP 工具**不走路由**（09-16 用户拍板）——
      路由判的是"这句话属于哪个业务领域"，而"要不要搜索"该由模型自己判断。
      它们直接进 `chat/tool_sources.tools_for()` 的工具面，见 mcp/bridge.py 文件头。
    """
    from station.skills.registry import get_registry
    out = []
    for s in get_registry().list():
        if s.type != "agent":
            continue                       # 非对话形态的技能（如 demo-pipe 纯后台）不进对话目录
        if s.id == "demo-agent":
            continue                       # demo 仅宿主冒烟，不作为正式能力
        if not s.tools:
            continue                       # 没有工具的技能不用列进目录
        out.append(SkillRef(id=s.id, ns=s.ns,
                            name=s.name, description=s.description,
                            hints=_routing_hints(s)))
    return out


def _routing_hints(skill) -> str:
    """把技能的“操作面”压成用户话术锚点。

    只用来帮小模型判断“这句话像哪个技能”，不包含工具参数 schema；
    archive 的锚点覆盖改类/页/OCR/进度/出件等工具 description 里的高频说法，
    weekly 覆盖写周报相关说法。新增技能时应补一组自己的锚点。
    """
    if skill.id == "archive":
        return ("上传照片/整理档案/开始识别/识别进度/全景/材料清单/看第N页/"
                "第N张改成九-x/并份/拆份/换一家OCR/采用新读法/出件/4件套/产物文件")
    if skill.id == "weekly-report":
        return ("帮我写周报/本周总结/整理周报/本周完成了什么/周报导出Word")
    if skill.id == "video":
        # 这一组**必须包含「视频」两个字**，因为草稿卡上的按钮预填的话术就是
        # "…生成视频吧" / "换个风格的视频提示词" —— 按钮也是一条路由输入，
        # 锚点里没有它，用户点了按钮会掉进通用聊天（那边没有 video.make）。09-13 踩过同类。
        return ("生成视频/做个视频/来一段视频/短视频/把这段描述生成视频/"
                "换个风格的视频提示词/视频好了吗/到哪了/放出来看看")
    if skill.id == "interview-coach":
        return ("面试复盘/面试视频/面试录音/面试音频/提取音频/视频转音频/"
                "FFmpeg/整理面试问答/逐题复盘/改进面试回答/面试表现总结")
    return skill.description


def get_skill(skill_id: str):
    """按 id 取真正可执行技能（含 tools）——命中后才按需加载使用。

    路由阶段不调它：只有确认用户意图属于该技能，才把该技能的 tools 给模型。
    """
    if not skill_id:
        return None
    from station.skills.registry import get_registry
    return get_registry().get(skill_id)
