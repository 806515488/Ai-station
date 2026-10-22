"""统一模型客户端：组织通道尝试与流式输出，向 agent 返回普通结果。

配置负责选条目，policy 决定预算/退避，协议模块负责请求/响应；输出正文后禁止换家。
"""
from __future__ import annotations

import os
import time

from station import config, observability as obs
from station.models import config as model_config, policy
from station.models.protocols import openai


class Model:
    """真实模型：ChatOpenAI 的薄封装。两个入口：

      respond(msgs, tools) → {content, tool_calls}        一次拿全（路由判词/压缩摘要用）
      stream(msgs, tools)  → 逐片 {"delta"} + 末尾 {"final"}（对话用，让回复逐字出现）

    09-10 起支持**降级链**：按 entries 给的顺序逐家尝试，一家不行换下一家。
    四条铁律（改这里之前务必看懂，写错了用户会看到"两家的话拼在一起"）：
      · 构造期失败（没配 key / 未知通道）  → 换下一家（纯本地，廉价）
      · respond() 调用失败                 → 换下一家（invoke 是原子的，没有半成品）
      · stream() **已经往外吐过字**后失败  → **绝不换**，直接抛
      · stream() 还没吐字就失败            → 换下一家
    entries=None 时退化成"单通道"，行为与改造前一模一样（老调用方不用改）。
    """

    def __init__(self, kind: str = "text", channel: str | None = None,
                 timeout: float = 180, max_retries: int = 1,
                 *, entries: list | None = None,
                 total_budget: float | None = None,
                 retries: int | None = None):
        config.load_env()                       # 确保 .env 里的 key 已被读进环境变量
        self.kind = kind
        # 策略保存单次超时、整链总预算和重试次数；每轮不会重置总预算。
        self._policy = policy.CallPolicy(timeout, total_budget, retries)
        self._max_retries = max_retries
        if entries is None:
            # 老路径（单通道）：构造时就建连 —— 保持"没配 key 立刻抛"的旧时机，
            # 老调用方（如 L2 判词）依赖它。给了 entries 才懒建（见 _client）。
            self.slot = ""
            self._entries = [model_config.legacy_entry(kind, channel)]
            self._entries[0]["_llm"] = self._build(self._entries[0])
        else:
            self._entries = [dict(e) for e in entries]
            if not self._entries:
                raise RuntimeError(
                    "没有可用的模型通道（模型配置里这个槽位的降级链是空的）")
            self.slot = self._entries[0].get("slot") or ""


    def _client(self, entry: dict, timeout: float | None = None):
        """取这条 entry 的客户端（老路径在构造时就建好了，直接复用）。"""
        if entry.get("_llm") is not None:
            return entry["_llm"]
        return self._build(entry, timeout)


    def respond(self, msgs: list[dict], tools=None, on_retry=None) -> dict:
        """把历史发给模型。tools 不为空时“绑定”工具说明 → 模型才知道能调什么。

        两层容错，别搞混：
          · **内层**：按降级链换下一家（invoke 是**原子**的 —— 要么完整拿到、要么
            什么都没有，不存在"半句话"，所以换家是安全的）。
          · **外层**：整条链全挂了，且最后一个错是"过会儿就好"的那种 → 退避几秒
            **整条链再来一轮**（最多 self._policy.retries 轮）。先换家再退避，是因为换家
            只要几百毫秒，而退避起步就 1 秒 —— 能靠换家解决的就别让用户等。
        on_retry 是给调用方的显示回调（收到 {"attempt","total","wait","error"}）。
        """
        # ★ 工具名归一化放在**重试循环外**，两个理由：① "归一化后撞名"这种错误该当场
        #   抛给用户看，不该被下面的 except 当成"这家不行"去换家重试 5 遍；
        #   ② 同一份 schema 没必要每一轮都重建一遍字符串。
        schemas, back = openai._wire_tools(tools) if tools else (None, None)
        last: Exception | None = None
        t0 = time.monotonic()          # ★ 只取一次：多轮之间总预算不被重置
        for rnd in range(self._policy.retries + 1):
            for entry, budget in self._policy.attempts(self._entries, t0):
                try:
                    with obs.span("model_attempt", "模型通道尝试", round=rnd+1, **obs.model_info(entry)) as attempt_step:
                        # bind_tools([每个工具 schema]) 让模型在对话里能输出 tool_calls
                        llm = self._client(entry, budget)
                        if schemas:
                            llm = llm.bind_tools(schemas)
                        response = openai._from_resp(llm.invoke(openai._to_llm(msgs)), back)
                        attempt_step.update(usage=response.get("usage"), output_chars=len(response.get("content", "")))
                        return response
                except Exception as e:          # noqa：这一家不行 → 记下换下一家
                    last = e
            wait = self._policy.retry_wait(rnd)
            if wait is None or not policy._is_transient(last):
                break                           # 注定失败或轮次用完 → 别再让用户等
            policy.notify_retry(on_retry, rnd + 1, self._policy.retries, wait, last)
            with obs.span("backoff", "等待重试", attempt=rnd+1, wait_seconds=wait):
                time.sleep(wait)
        raise RuntimeError(policy.failure_message(self.slot, self._entries, last))

    def stream(self, msgs: list[dict], tools=None):
        """流式版 respond：先逐片 yield {"delta": 文本}，最后 yield {"final": {...}}。

        final 与 respond 的返回值同形，调用方据此判断“要不要调工具”。
        逃生阀：环境变量 STATION_STREAM=0 时直接退回非流式 —— 万一某家的流式端点
        不兼容（工具调用被吞/报错），不用改代码就能恢复旧行为。

        除了 {"delta"} / {"final"}，退避等待时还会 yield 一种 **{"retry": {...}}**
        （含第几次、共几次、等几秒），好让前端显示"正在重新连接"。全仓只有
        core/agent.py 消费本函数的产出，加这一种 part 是安全的。

        ★ 降级铁律就在下面的 sent_any：**已经往外吐过字就绝不再换家**。继续换的话，
          用户看到的是"上一家的半句话 + 下一家的整句话"，是胡话，宁可报错。
          这条同样管住外层的退避重试 —— 那句话一抛就直接穿出两层循环，不会再等。
        """
        if os.environ.get("STATION_STREAM", "1") == "0":
            yield {"final": self.respond(msgs, tools)}
            return
        # 同 respond：归一化一次、放在循环外（理由见那边的注释）
        schemas, back = openai._wire_tools(tools) if tools else (None, None)
        last: Exception | None = None
        t0 = time.monotonic()          # ★ 只取一次：多轮之间总预算不被重置
        for rnd in range(self._policy.retries + 1):
            for entry, budget in self._policy.attempts(self._entries, t0):
                sent_any = False                  # 这一家已经往外吐过字没有
                try:
                    with obs.span("model_attempt", "模型通道尝试", round=rnd+1, **obs.model_info(entry)) as attempt_step:
                        # 绑了工具才能流式收到 tool_calls（langchain 的 RunnableBinding 会转发流）
                        llm = self._client(entry, budget)
                        if schemas:
                            llm = llm.bind_tools(schemas)
                        # 注意：_stream_parts 是【生成器】，异常是在消费到那一片时才抛出来的，
                        # 所以 try 必须把整个 for 包住，不能只包它的构造。
                        for part in openai._stream_parts(llm.stream(openai._to_llm(msgs)), back):
                            if "delta" in part:
                                sent_any = True
                                if "first_text_ms" not in attempt_step.record["attributes"]:
                                    attempt_step.update(first_text_ms=round((time.monotonic()-attempt_step.started)*1000, 2))
                            if "final" in part:
                                attempt_step.update(usage=part["final"].get("usage"), output_chars=len(part["final"].get("content", "")))
                            yield part
                        return                        # 整段跑完 = 这次成功
                except Exception as e:            # noqa：不能捕 BaseException，
                    # 否则会把客户端断连的 GeneratorExit 也吃掉、生成器停不下来。
                    if sent_any:
                        raise RuntimeError(
                            f"{entry.get('id')} 输出到一半失败，且已经向外发过内容，"
                            f"不再切换通道：{type(e).__name__}: {e}") from e
                    last = e                      # 还没吐字 → 可以安全换下一家
            wait = self._policy.retry_wait(rnd)
            if wait is None or not policy._is_transient(last):
                break                             # 注定失败或轮次用完 → 别再让用户等
            # 先把"要重连"告诉前端，再睡 —— 否则用户面对的是没有任何解释的长时间静默
            yield {"retry": {"attempt": rnd + 1, "total": self._policy.retries, "wait": wait}}
            with obs.span("backoff", "等待重试", attempt=rnd+1, wait_seconds=wait):
                time.sleep(wait)
        raise RuntimeError(policy.failure_message(self.slot, self._entries, last))


    def _build(self, entry: dict, timeout: float | None = None):
        """只负责把本次策略参数交给协议层；客户端构造失败由外层选择下一通道。"""
        return openai.build_client(entry, timeout=self._policy.timeout if timeout is None else timeout,
                                   max_retries=self._max_retries)

def build(kind: str = "text", channel: str | None = None,
          entries: list | None = None,
          total_budget: float | None = None,
          retries: int | None = None) -> Model:
    """对外统一入口：造一个真实模型。

    - 只给 kind/channel：config.legacy_entry 解析旧通道，保留构造期报错时机。
    - 给 entries：使用 models.config.resolve 解析的有序主备链。
    - retries=None 使用 policy 的默认重试次数；0 = 不退避重试。
    """
    return Model(kind, channel, entries=entries, total_budget=total_budget,
                 retries=retries)
