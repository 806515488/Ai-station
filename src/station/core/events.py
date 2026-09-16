"""宿主事件流：所有跨层信号（agent 步进、进度、产物、报错、批准请求）统一成 Event。

事件流是 UI(SSE) / 日志 / 可观测共用的脊柱：agent 与 job 每步都 emit。

新手视角：Event 就是一句“刚才发生了什么”的广播。
  - agent 调了工具 → Event(EV_TOOL, …)
  - 模型说了一句话 → Event(EV_DELTA, …)（前端逐字显示）
  - 危险工具要批准 → Event(EV_APPROVAL, …)
理解“事件”后，看 app/server.py 就知道：这些 Event 被一行行转成 SSE 推到浏览器（to_sse）。
"""
from __future__ import annotations          # 让类型注解能用 `X | None`（Python 3.10+）写法

import json
from dataclasses import dataclass, field    # dataclass: 自动生成 __init__ 的数据“盒子”

# 事件类型常量（P1 子集；随 lifecycle-hooks 演进再扩）。
# 用常量而不是到处写字符串，是为了防手滑打错（写错常量的地方会直接报错/可被 IDE 检查）。
EV_APPROVAL = "approval_request"     # 请求用户批准一个危险工具
EV_TOOL = "tool"                     # 某一步调用了工具
EV_DELTA = "delta"                   # 模型最终回复里的一段文字（可多次，前端拼起来）
EV_RENDER = "render"                 # 渲染卡片（工具返回了卡片 → 前端按 type 渲染）
EV_DONE = "done"                     # 本轮结束
EV_ERROR = "error"                   # 出错
# 模型调用失败、正在退避重连（09-15）：data = {"attempt","total","wait","error"}。
# 前端拿它在等待态里显示"连接失败，N 秒后重试（第 k/5 次）"——见 core/model.py 的 RETRY_WAITS。
EV_RETRY = "retry"

# 渲染卡片事件 data 的统一外壳键：
#   EV_RENDER data = {"card": {"type": "page-card", ...}} —— card.type 对应前端渲染器注册表
RENDER_KEY = "card"


@dataclass
class Event:
    """一个事件 = (类型, 附带数据)。

    dataclass 自动帮我们写了 __init__，所以 Event("tool", {"names":[...]}) 就能用。
    type 决定“是什么事”，data 放这件小事的具体信息（任意 dict）。
    """
    type: str
    data: dict = field(default_factory=dict)   # default_factory: 不传 data 时给个空 dict

    def to_dict(self) -> dict:
        """把事件展开成一个扁平的 dict，例如 {"type":"tool", "names":[...]}。"""
        # **self.data 把 data 里的键展开拼到前面 —— 前端就不用套一层 data 了。
        return {"type": self.type, **self.data}

    def to_sse(self) -> str:
        """转成 SSE（Server-Sent Events）格式：浏览器能一行行实时读的文本流。

        SSE 约定：每条消息以 `data: ` 开头、`\n\n` 结尾。前端 readSSE 就是按这个切的。
        """
        return "data: " + json.dumps(self.to_dict(), ensure_ascii=False) + "\n\n"
        # ensure_ascii=False → 中文字符原样输出（否则会变成 \uXXXX 转义，难读）
