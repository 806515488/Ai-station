"""模型调用策略：瞬时错误判定、总预算、通道顺序与整链退避。

不依赖 SDK 或协议。一个请求只取一次起点，所有通道和重试轮次共享它。
"""
from __future__ import annotations
import time

RETRY_WAITS = (1.0, 2.0, 4.0, 8.0, 16.0)   # 最多重试 5 次，合计约 31 秒


_TRANSIENT_NAMES = frozenset({
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "TransportError", "NetworkError",
    "APIConnectionError", "APITimeoutError", "InternalServerError",
})


def _is_transient(err) -> bool:
    """这个失败值不值得退避重试？True = 等一会儿再试，False = 立刻报错。

    判定顺序（先看 HTTP 状态码，最准）：
      · 状态码 ≥500 或 408 → 对端的锅，重试
      · 状态码 4xx（含 401/403 鉴权、404 模型名、**429 配额**）→ 重试没用，立刻报
        ★ 429 不重试是本仓既定口径，先例见 skills/video 的 CREATE_RETRY_WAITS：
          "按次限流，越撞越死"。
      · 没有状态码就看异常类名（**走整条继承链**，见下）/ 是不是内置的连接与超时异常
      · 最后兜一条**导入竞态**：`partially initialized module 'httpx' …` —— 那是
        服务器刚起后第一次调模型时，多个线程同时执行同一个 import 撞出来的，
        几百毫秒后必然已经好了（详情见 import_chat_openai）。
    """
    code = getattr(err, "status_code", None)
    if not isinstance(code, int):
        # openai/langchain 的异常有的把状态码挂在 .response 上，两个位置都认
        code = getattr(getattr(err, "response", None), "status_code", None)
    if isinstance(code, int):
        return code >= 500 or code == 408
    # ★ 比的是**整条继承链**，不是光是叶子的类名：实测真抛出来的是
    #   `OpenAIConnectionError`（它的爹才是 `APIConnectionError`）—— 只比叶子名会漏掉
    #   最常见的那个"连不上"，退避就永远不生效。这个漏是 09-15 端到端真跑才抓到的，
    #   单测抓不到（假的异常类名是我自己起的，正好起对了）。
    if any(k.__name__ in _TRANSIENT_NAMES for k in type(err).__mro__):
        return True
    if isinstance(err, (ConnectionError, TimeoutError)):
        return True
    return "partially initialized module" in str(err)


class CallPolicy:
    def __init__(self, timeout: float, total_budget: float | None, retries: int | None):
        self.timeout = timeout
        self.total_budget = total_budget
        self.retries = len(RETRY_WAITS) if retries is None else max(0, int(retries))

    def attempts(self, entries: list, t0: float | None = None):
        """按降级顺序产出 (entry, 这次尝试能用的 timeout)。

        带总预算时：每次尝试的 timeout = min(单次 timeout, 剩余预算)；预算耗尽就停止
        尝试并报错 —— 这才是"整条链一共 N 秒"的正确语义。

        ★ t0 由调用方传进来（不是这里取）：退避重试会**多轮**调用本函数，如果每轮
          都重新取一次 t0，预算就被无限续命、永远耗不尽 —— L2 的"整条链一共 10 秒"
          会当场失效。所以时钟只在进入轮次循环之前取一次。
        """
        t0 = time.monotonic() if t0 is None else t0
        for i, entry in enumerate(entries):
            budget = None
            if self.total_budget:
                left = self.total_budget - (time.monotonic() - t0)
                if left <= 0:
                    tried = [str(e.get("id")) for e in entries[:i]]
                    raise RuntimeError(
                        f"降级链总预算 {self.total_budget}s 已用完（试过 {tried}）")
                budget = min(self.timeout, left)
            yield entry, budget


    def retry_wait(self, rnd: int) -> float | None:
        """第 rnd 轮（从 0 数）全挂之后该退避几秒；None = 不该重试了。

        两种情况返回 None：轮次用完（self.retries），或 RETRY_WAITS 被截短到不够
        （单测会把它 patch 成短的，得防越界）。
        """
        if rnd >= self.retries or rnd >= len(RETRY_WAITS):
            return None
        return RETRY_WAITS[rnd]


def notify_retry(cb, attempt: int, total: int, wait: float, err) -> None:
    """告诉外面"正在重连"（对话侧由 stream 直接 yield，别处走这个回调）。

    ★ 整段包 try/except：**通知坏掉绝不许影响调用本身** —— 重连是为了让用户
      拿到结果，不能因为一个显示用的回调把正经事带崩。
    """
    if cb is None:
        return
    try:
        cb({"attempt": attempt, "total": total, "wait": wait,
            "error": f"{type(err).__name__}: {err}"})
    except Exception:                      # noqa：显示层的锅，不该影响调用
        pass


def failure_message(slot: str, entries: list, err) -> str:
    """所有通道都挂时给一句能照着修的话（说清是哪个槽位、试过谁）。"""
    where = f"槽位「{slot}」" if slot else "模型"
    tried = " → ".join(str(e.get("id")) for e in entries)
    return (f"{where}所有通道都失败了（试过 {tried}）："
            f"{type(err).__name__}: {err}")
