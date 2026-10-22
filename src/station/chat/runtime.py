"""准备执行环境：模型选择、技能上下文、附件资料与基础系统提示词。

service 只调用这里准备执行，不关心模型配置字段或附件正文的加载细节。
通用/技能基础提示词在这里确定；任务、网页及 MCP 动态须知仍由 core.agent 追加。
"""

import os

from station import config
from station.core.context import Context
from station.models import fake


def make_model(agent, user_id: str):
    """测试使用 FakeModel；正式请求只解析当前用户的主模型链。"""
    from station.models import client as model
    if os.environ.get("STATION_FAKE", "0") == "1":
        return fake.FakeModel(agent.ns)
    from station.models import config as modelcfg
    return model.build(agent.model, entries=modelcfg.resolve(user_id, "chat"))


def create_context(thread, agent, *, reply_only: bool = False) -> Context:
    """技能只拿自己的目录/Key 白名单；附件只加载当前会话已固定的版本。"""
    context = Context(
        thread=thread, skill_id=agent.id, data_dir=config.sub("skills", agent.id),
        allowed_keys=list(getattr(agent, "keys", None) or []),
        auto_approve=config.AUTO_APPROVE, user_id=thread.user_id,
        system=(agent.system or "").strip(), reply_only=reply_only,
    )
    if any(message.get("attachment_turn_id") for message in thread.msgs):
        from station.documents.inputs import collect
        context.document_inputs = collect(thread)
    if reply_only:
        context.system += "\n本次仅根据已保存的附件与处理结果继续回答，不执行或重放任何工具。资料不足时明确说明。"
    return context
