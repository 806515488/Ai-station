"""共用回答流程：解释批准回复→路由→装配→Agent 执行（批准接续与 Loop）。

输入已经准备好，服务只组织路由与能力装配，不直接保存业务状态。
从这里进入 core.AgentExecution，顺着读完批准接续和模型—工具循环。

上游 conversation/service.py 负责保存；下游 core/agent.py 负责循环。
本文件只把准备好的 ReplyInput 变成可执行回答，向上交还展示事件或保存检查点。
"""
from __future__ import annotations
from station.core.agent import run_agent
from station.core.contracts import APPROVAL_REMINDER, CheckpointRequest, Outcome, identity
from station.core.events import Event
from station.core import guards
from station.core.router import log_route
from station.chat import assembly, routing
from station.chat.catalog import build_generic_agent, build_skill_catalog
from station.chat.model_execution import ModelAdapter
from station.chat.models import ActiveSkillUpdate, PreparedAnswer


class AnswerExecution:
    """一轮回答的执行对象：创建时不调模型，迭代 events() 时才开始工作。"""

    def __init__(self, reply):
        self.reply = reply
        self.execution_id = identity()
        self._prepared = None  # 路由/装配完成后的固定结果，供会话层保存 active_skill。
        self.route_ms = 0.0
        self._outcome = None  # 未进入 Loop 就结束时，由本层记录终态。
        self._agent = None  # 进入 Loop 后的 AgentExecution，终态可以从它读取。
        self._iterator = None  # 同一次回答复用同一个生成器，避免重复执行工具。
        self._closed = False

    @property
    def prepared(self):
        return self._prepared

    @property
    def outcome(self):
        return self._outcome or (self._agent.outcome if self._agent else None)

    def events(self):
        """返回同一个事件迭代器；第一次 next 才真正进入 _drive → _answer。"""
        if self._closed and self._iterator is None:
            return iter(())
        if self._iterator is None:
            self._iterator = self._drive()
        return self._iterator

    def close(self):
        """释放回答及内部模型流；重复关闭无影响，已完成的终态不会改成中断。"""
        if self._closed:
            return
        if self._iterator:
            self._iterator.close()
        if self._agent:
            self._agent.close()
        if self.outcome is None:
            self._outcome = Outcome("interrupted")
        self._closed = True

    def _drive(self):
        try:
            # yield from 会逐项转交子生成器的输出，同时把 close 传给子生成器。
            yield from self._answer()
        finally:
            try:
                if self._agent:
                    self._agent.close()
            finally:
                if self.outcome is None:
                    self._outcome = Outcome("interrupted")
                self._closed = True

    def _answer(self):
        reply, history = self.reply, self.reply.history
        # ① pending 是上一轮等待批准的操作，与后台任务状态无关。
        pending = history.get_pending()
        decision = guards.classify_answer(reply.message) if pending else None
        if pending and not decision:
            yield Event("delta", {"text": APPROVAL_REMINDER})
            self._outcome = Outcome("waiting_approval", "approval_reminder", {})
            return
        # ② 准备候选技能和通用助手；选出哪一个，不代表已调用主模型。
        catalog, generic = build_skill_catalog(), build_generic_agent()
        # 批准时沿用原工具的技能，其他请求通过意图识别选择；Loop 不参与路由。
        agent = routing.agent_for_tool(pending.get("tool")) if pending else None
        selection = routing.Selection(agent or generic, level="pending") if pending else routing.select_agent(reply, catalog, generic)
        agent = selection.agent
        self.route_ms = selection.duration_ms
        # ③ 装配工具、消息构造器、执行策略。Context 只给工具适配器使用，不传入 Loop。
        context, tools, builder, policy = assembly.assemble(reply, agent)
        checkpoint = CheckpointRequest("prepared", history.revision)
        active = ActiveSkillUpdate("set", agent.id) if agent.id in {s.id for s in catalog} else ActiveSkillUpdate("clear")
        self._prepared = PreparedAnswer(self.execution_id, checkpoint.checkpoint_id, history.revision, agent.id, active)
        # 暂停在这里：conversation 会保存用户消息、当前技能等；成功后才继续模型/工具。
        yield checkpoint
        log_route({"text": reply.message[:200], "agent": agent.id, "level": selection.level,
                   "skill": selection.skill_id, "route_ms": selection.duration_ms, "l2": selection.diagnostics or None})

        # ④ 工厂只保存在 chat 适配器中，创建对象时不构建真实模型。
        # AgentExecution 先补执行/拒绝原操作并等待保存，再初始化模型、进入 Loop。
        model = ModelAdapter(factory=lambda: assembly.make_model(agent, reply.user_id))
        self._agent = run_agent(model=model, history=history, tools=tools, message_builder=builder,
                                policy=policy, approval=decision)
        yield from self._agent.events()
