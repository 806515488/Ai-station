"""demo-agent —— agent 型示例技能：对话→工具→答复，验证宿主 agent 循环。

tools() 返回的都是叶子 Tool（name 不带命名空间，registry 会补成 demo.*）。
fake_risky 是 approve 类，用来演示批准闸（in-band 询问）。

新手视角：这是“加一个 agent 工具”的最小样板，最适合动手练。
  - 每个工具 = 一个函数(run) + 一张说明书(Tool)。函数签名固定：先收 ctx，再收参数。
  - 练习：照着 now 加一个 Tool(name="plus", run=lambda ctx,a,b: a+b, args=[…a…b…])，
    重启后对 demo 说“3 加 5 等于几”，看它调 demo.plus —— 你就学会给 AI 加能力了。
  - args 清单要写清楚，模型靠 description+args 判断“什么时候该调、参数填啥”。
"""
from __future__ import annotations

import time

from station.core.tool import Tool


# 每个工具的 run 都是一个“函数”：第一个参数永远是 ctx（Context，见 core/context.py）
# —— 这里暂时用不到 ctx，但签名必须留着，因为 agent 循环是 run(ctx, **args) 这样调的。

def _now(ctx, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回当前日期时间。fmt 是 strftime 的格式串，默认精确到秒。"""
    return time.strftime(fmt)


def _echo(ctx, text: str = "") -> str:
    """把一句话原样回显 —— 演示最朴素的“对话→工具→答复”。"""
    return f"收到：{text}（来自 demo.echo）"


def _fake_risky(ctx) -> str:
    # 只在用户批准后才会真正被调用（否则 agent 循环会先停下问用户，见 core/agent.py）
    return "（假）一个需要你批准的“危险”操作已执行 —— 演示批准闸生效。"


def tools() -> list[Tool]:
    """本技能能用的工具清单。宿主(registry)会把它注册成 demo.* 三个工具。"""
    return [
        # 每项 = Tool(名字, 说明, 真正干活的函数, 参数说明书…)
        # 说明(description)和参数(args)是给模型看的“使用说明书”
        Tool(name="now", label="看时间", description="返回当前日期时间",
             run=_now,
             args=[{"name": "fmt", "type": "str", "desc": "strftime 格式(可省)",
                    "required": False}]),       # required=False → 参数可不填
        Tool(name="echo", label="回显", description="把一句话原样回显，用于演示对话闭环",
             run=_echo,
             args=[{"name": "text", "type": "str", "desc": "要回显的文字",
                    "required": True}]),        # required=True → 模型必须填
        Tool(name="fake_risky", label="演示：危险操作",
             description="示例：一个需要用户批准才能执行的“危险”工具，用于演示批准闸",
             run=_fake_risky, risk="approve"),  # risk="approve" → 危险，要批准才能跑
    ]
