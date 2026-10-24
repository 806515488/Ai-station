"""装配当前助手的模型、工具、系统提示词和执行策略。"""

import os

from station import config
from station.core.context import Context
from station.models import fake
from station import observability as obs
from station.core.contracts import CallLimit, ExecutionPolicy
from station.chat.messages import MessageBuilder
from station.chat.catalog import build_skill_catalog
from station.chat.prompts import system_overview
from station.chat.tool_execution import ToolSet
from station.chat.tool_sources import tools_for, jobs_note, GLOBAL_TOOL_CAP, GLOBAL_TOOL_CAPPED


def make_model(agent, user_id: str):
    """测试使用 FakeModel；正式请求只解析当前用户的主模型链。"""
    from station.models import client as model
    if os.environ.get("STATION_FAKE", "0") == "1":
        return fake.FakeModel(agent.ns)
    from station.models import config as modelcfg
    return model.build(agent.model, entries=modelcfg.resolve(user_id, "chat"))


def create_context(thread, agent) -> Context:
    """技能只拿自己的目录和 Key 白名单，避免越过当前用户的权限。"""
    context = Context(
        thread=thread, skill_id=agent.id, data_dir=config.sub("skills", agent.id),
        allowed_keys=list(getattr(agent, "keys", None) or []),
        auto_approve=config.AUTO_APPROVE, user_id=thread.user_id,
        system=(agent.system or "").strip(),
    )
    return context


def assemble(reply, agent):
    """装配顺序集中在这里；新增工具来源不改 Loop。"""
    # ① 工具运行需要的用户、技能目录和批准配置封装成 Context，留在适配器侧。
    context = create_context(reply.thread, agent)
    with obs.span("assembly", "组装本轮工具") as step:
        # 工具来源的合并/去重在 tool_sources，工具白名单由装配结果确定。
        selected = tools_for(agent, reply.user_id)
        step.update(tools=[{"name": t.name, "owner": t.owner} for t in selected], count=len(selected),
                    schema_hash=obs.fingerprint([t.schema() for t in selected]))
    from station.tools import web
    from station.mcp import bridge
    # ② 所有技能共用宿主身份；目录来自内存注册表，工具状态来自本轮 selected。
    # 后面再补当前技能的岗位须知、任务现状和网页/MCP 说明，不把介绍写进历史。
    notes = [system_overview(build_skill_catalog(), selected), context.system, jobs_note(reply.thread)]
    if reply.user_id:
        notes.append(web.system_note(reply.user_id, enabled=any(t.owner == "web" for t in selected)))
        try:
            notes.append(bridge.system_note(reply.user_id))
        except Exception:
            pass
    # ③ 把宿主业务规则翻译成通用策略；Loop 只按工具名查上限，不判断 owner == station。
    policy = ExecutionPolicy(max_steps=config.MAX_STEPS, auto_approve=config.AUTO_APPROVE,
        compact_threshold=config.COMPACT_TOKENS,
        tool_limits={t.name: CallLimit(GLOBAL_TOOL_CAP, GLOBAL_TOOL_CAPPED.format(name=t.name, cap=GLOBAL_TOOL_CAP))
                     for t in selected if t.owner == "station"})
    # ④ ToolSet 绑定真实工具与 Context；MessageBuilder 保存本次系统说明。
    return context, ToolSet(selected, context), MessageBuilder("\n\n".join(n for n in notes if n).strip()), policy
