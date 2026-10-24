"""工具执行适配：用户/任务工牌、观测及文字与卡片协议。

调用链：Loop → ToolSet.execute → _execute → _run_tool_impl → 真实 Tool.run。
外部 MCP 工具也已经在 bridge 层包装成 Tool，所以走同一条执行链。
"""
from station import observability as obs
from station.core.context import Context
from station.core.tool import Tool
from station.core.events import Event, EV_RENDER, RENDER_KEY
from station.core.contracts import Presentation, ToolResult, ToolSpec, ToolErrorInfo


class ToolSet:
    """给内核的一次性工具快照；上下文和来源都在这个外部适配器中绑定。"""
    def __init__(self, tools, context):
        self._tools = {t.name: t for t in tools}  # 实际可执行对象，包含 run 函数。
        self._specs = {t.name: ToolSpec(t.name, t.label or "", t.risk, t.owner, t.schema()) for t in tools}  # 交给内核的描述快照。
        self.context = context
        if getattr(context, "depth", 0) == 0:
            # 顶层回答重置网页预算；子级工具共用原预算，防止嵌套调用绕开限制。
            from station.search.service import Budget
            context._search_budget = Budget()

    def specs(self):
        return list(self._specs.values())

    def spec(self, name):
        return self._specs.get(name)

    def preview(self, name, arguments):
        """批准前只生成操作预览，不调用 run；预览失败不应提前执行真实操作。"""
        tool = self._tools[name]
        try:
            return str(tool.preview(self.context, **arguments) or "") if tool.preview else ""
        except Exception:
            return ""

    def execute(self, invocation):
        # 旧工具可能把卡片追加进 ctx.events；只取本次新增部分，避免上一工具的卡片重发。
        start = len(self.context.events)
        text, error_info = _execute(self.context, self._tools[invocation.name], invocation.arguments)
        events = self.context.events[start:]
        del self.context.events[start:]
        # 卡片带上调用方预留的历史锚点；此处只返回，等外层检查点保存后再展示。
        return ToolResult(text, tuple(Presentation(e, invocation.presentation_anchor)
                                      for e in events if e.type == "render"), error_info)

def _run_tool_impl(ctx: Context, tool: Tool, args: dict) -> str:
    """真正执行一个工具，返回 (给模型的文字, 可选错误信息)，卡片暂存 ctx.events。

    模型只会“点名工具+给参数”，不自己执行 —— 这里由宿主替你调用 tool.run。
    普通 Exception 转成文字回给模型（模型能据此调整）；协作取消等 BaseException 继续向外传播。

    渲染契约（对话式改造 09-07）：工具想“给人看东西”（页图/全景/对比卡/文件）
    时，返回 {"text": 给模型的话, "render": {"type": "page-card", ...}} 这样的 dict
    —— render 先暂存，再由 ToolSet 转成 Presentation；检查点保存后才能转发前端。
    text 部分作为工具结果回给模型。工具只管返回，宿主负责交给两种消费者。

    ★ render 可以是**一张卡（dict）或一串卡（list[dict]）**（09-13 加的 list 形式）：
      一次改动常常要同时刷好几张卡（如"改了类别"的全景卡 + "学到的口径待确认"卡）。
      普通 Loop 与 core.pending 的批准补执行共用 ToolSet，均支持多张卡片。
      顺带一提：MCP 的 content blocks 本来就是一串，
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
                return str(out.get("text", "")), None
        return str(out), None
    except Exception as e:                   # noqa  # 出错也要给模型一个可读结果
        obs.fail(type(e).__name__)
        return f"工具执行出错: {type(e).__name__}: {e}", ToolErrorInfo(type(e).__name__)

def _execute(ctx: Context, tool: Tool, args: dict):
    """记录工具边界；异常状态在转换为业务文本之前采集。"""
    from station.tasks.runtime import bind_context
    # bind_context 让工具创建的后台任务拿到当前用户/会话身份；span 记录本次工具耗时。
    # 这些都是宿主适配职责，因此留在 Loop 外面。
    with bind_context(ctx),obs.span("tool", tool.label or tool.name, tool=tool.name, owner=tool.owner,
                  **obs.input_info(args)) as step:
        text, error = _run_tool_impl(ctx, tool, args)
        step.update(output_chars=len(text), output_hash=obs.fingerprint(text))
        return text, error


def run_tool(ctx: Context, tool: Tool, args: dict) -> str:
    """一次性工具调用入口，复用同一执行/观测与卡片归一化，不驱动 Agent。"""
    return _execute(ctx, tool, args)[0]
