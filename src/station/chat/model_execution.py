"""模型观测适配：内核不导入 observability，也能保留逐步模型/摘要追踪。"""
from station import observability as obs
from station.core.contracts import ModelInitializationError
from station.core.events import Event


class ModelAdapter:
    """包装真实模型：调用接口不变，在外围加执行追踪并保证流关闭。"""
    def __init__(self, model=None, *, factory=None):
        if (model is None) == (factory is None):
            raise ValueError("提供已构建模型或延迟工厂中的一种")
        self.model = model
        self._factory = factory
        self._initialization_error = None

    def initialize(self):
        """普通方法：批准结果保存后才调用工厂，成功返回一次 stage；不产生网络流。"""
        if self.model is not None:
            return []
        if self._initialization_error is not None:
            raise self._initialization_error
        try:
            self.model = self._factory()
            if self.model is None:
                raise ValueError("模型工厂未返回模型")
        except Exception as exc:
            # 只缓存普通配置错误；ExecutionCancelled 等 BaseException 交给执行器处理。
            self._initialization_error = ModelInitializationError(f"模型不可用：{exc}")
            self._factory = None
            raise self._initialization_error from exc
        self._factory = None
        return [Event("stage", {"text": "正在思考…"})]

    def stream(self, messages, tools=None):
        # 一个 span 对应一次主模型请求；同一用户回答可能调多次，因此会出现多个 span。
        with obs.span("model", "主模型请求", message_count=len(messages),
                      tools=[t.name for t in tools or []], **obs.input_info(messages)) as step:
            stream = self.model.stream(messages, tools)
            try:
                for part in stream:
                    if "final" in part:
                        # 只在完整响应处记录用量和工具名；缺失的 usage 不自行估造。
                        final = part["final"]
                        step.update(tool_calls=[c.get("name", "") for c in final.get("tool_calls", [])],
                                    output_chars=len(final.get("content", "")), usage=final.get("usage"))
                    yield part  # delta/retry/final 原样交给 Loop，由 Loop 决定下一步。
            finally:
                # 用户断开或上层提前停止迭代，也要释放 SDK/HTTP 的流连接。
                close = getattr(stream, "close", None)
                if close:
                    close()

    def respond(self, messages, tools=None):
        """压缩用非流式接口：一次拿回摘要，不把摘要过程当成回答文字发给用户。"""
        with obs.span("model", "压缩历史摘要", purpose="summary", message_count=len(messages)):
            return self.model.respond(messages, tools)
