"""统一对话的“能力目录 + 通用聊天”外观。

原则：**技能工具不是全局的**。路由阶段只读每个技能的名称/描述（轻量目录），
命中某个技能后才把该技能的 tools 交给模型，其余技能的工具完全不进上下文。
`SkillRef` 就是路由用的“目录条目”；`GenericAgent` 是没有业务工具时的通用聊天。

**全局工具是另一层**（09-12 加）：与业务无关的通用能力（读写文件、长期记忆、
看产物）挂在宿主上，**每个会话都注入**，见 station/tools/。合并发生在
`core/agent.tools_for()` —— 一句话：技能工具按路由给，全局工具人人有。

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
    "你是 station 个人 AI 工作站的主对话助手。当前这句话没有落入某个业务技能，"
    "但**你手上是有工具的**：记和查长期记忆（remember/recall）、列出用户已有的产物"
    "（artifacts）、看这个工作站挂了哪些能力（skills）。"
    "该动手就动手，别只说'我做不到'。\n"
    "- ★ **你没有任何读写文件的工具**（也没有推图、搜索源码的能力）——服务器上的文件"
    "你碰不到。用户问'帮我看下某个文件/某段源码'时，**如实说做不到**，"
    "别答应下来再说「我这就去翻」（09-13 实测踩过：模型照着自己背的旧提示词答应用 ls/read，"
    "而那两个工具早删了）。要看的是**他自己电脑上**的文件，就引导他上传。\n"
    "- 如果用户想做档案整理或写周报，用 station.skills 看一眼有哪些能力，"
    "再提醒他换一种更明确的说法（路由会自动切到对应技能）。\n"
    "- 不要编造尚未执行的进度或产物。\n"
    "- 中文、简洁，像同事聊天。"
)


def build_generic_agent() -> GenericAgent:
    """通用聊天外观：**只带全局工具**，不带 archive./weekly. 的工具。

    为什么要带全局工具（09-12 补）：不带的话，没命中技能时模型手上一件工具都没有
    —— "看下 docs/status.md""把这段记下来"只能干说。全局工具与业务无关，带上是纯赚；
    业务工具仍然严格按路由给（见本文件开头的原则）。

    （approve 类全局工具挂起后的补执行也依赖这里：server 的 `_skill_for_tool`
    按 ns 反查时，`station.` 就落到这个 agent 上。）
    """
    from station.tools import all_tools
    return GenericAgent(tools=all_tools(), system=GENERIC_SYSTEM)


def build_skill_catalog() -> list[SkillRef]:
    """列出可路由的 agent 技能描述（只读 name/desc，不给模型工具 schema）。"""
    from station.skills.registry import get_registry
    reg = get_registry()
    out = []
    for s in reg.list():
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
        return ("帮我写周报/本周总结/整理周报/把笔记和git提交写成周报/周报导出Word")
    return skill.description


def get_skill(skill_id: str):
    """按 id 取真正可执行技能（含 tools）——命中后才按需加载使用。

    路由阶段不调它：只有确认用户意图属于该技能，才把该技能的 tools 给模型。
    """
    if not skill_id:
        return None
    from station.skills.registry import get_registry
    return get_registry().get(skill_id)
