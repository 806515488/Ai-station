"""Tool —— 单个可被模型调用的动作（叶子），带命名空间与风险等级。

与 v1 旧实现的差异：
  1) name 一律是完整命名空间 `skill.tool`（如 demo.now），不再全局裸绑；
  2) 增加 risk（auto/approve）：approve 类工具过批准闸（见 core.agent/guards）；
  3) 仍以“人话参数清单 → OpenAI schema”翻译，便于换 MCP 时映射 tool schema。

新手视角：把 Tool 想成「给 AI 用的一张说明书 + 一个真函数」。
  - AI 不会自己执行 now()，它只能“点名”：我想用 demo.now，参数是 {}。
  - schema() = 把这张说明书翻译成模型认识的 JSON（告诉它工具名/作用/参数要啥）。
  - 真正干活的是 run(ctx, **args) —— 由 agent 循环替你调用（见 core/agent.py）。
加“一个能力”最常见的动作，就是 new 一个 Tool 并写它的 run（看 skills/demo-agent 的练习）。
"""
from __future__ import annotations

from dataclasses import dataclass, field      # 数据“盒子”语法；见 events.py 里讲过
from typing import Any, Callable

# 人话类型 → OpenAI schema 类型（schema 只认 string/integer/number/…，不认 str/int）
# 所以在声明工具参数时你写 "str"/"int"，翻译时会查这张表换成模型认识的 "string"/"integer"
_T = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
      "list": "array", "dict": "object"}


@dataclass
class Tool:
    """一个可被 AI 调用的动作。

    run 的签名固定：第一个参数永远是 ctx（Context），后面才是自己声明的参数，
    因为 agent 循环调用时是 run(ctx, **用户让模型填的参数)。
    """
    name: str                       # 完整名：skill.tool（如 demo.now），全局唯一
    description: str                # 一句话给模型看：什么时候该用它、它干嘛
    run: Callable[..., Any]         # 真正干活的函数：run(ctx, **args) -> Any
    args: list[dict] = field(default_factory=list)   # 参数清单，每项是 {name,type,desc,required}
    risk: str = "auto"              # auto | approve（approve=危险，要过批准闸）
    owner: str = ""                 # 所属 skill id（登记用）
    # 可选：**给人看**的一句话（如"查看有哪些识图模型"）。前端在对话流里显示"正在做什么"
    # 时用它 —— description 是写给**模型**看的（含"用户说 XX 时用"这类提示），拿给用户看
    # 又啰嗦又难懂；而 `archive.list_vision_models` 这种名字用户完全看不懂。
    # 留空则前端退回显示工具叶子名（新技能可以先不写，不会崩）。
    label: str = ""

    # 可选：**approve 类工具**在挂起问用户之前，由它自己算一段"这一步要做什么"给用户看
    # （签名与 run 一样：preview(ctx, **args) -> str）。
    # ★ 为什么是工具自己算、而不是宿主算：宿主不认识业务 —— 它只知道"要调 archive.xxx"，
    #   说不出"要往《文种对照》里加哪一条、顺便摘掉哪个词"。只有技能自己讲得清。
    # ★ 尤其是"算出来的东西和参数里没写的东西"：`apply_learning` 的参数只有"哪一条"，
    #   而真正要写进文件的条文是它现算的 —— 光看参数用户根本不知道在批准什么。
    # 留空/抛异常都不影响流程（前端退回只显示 label）。
    preview: Callable[..., Any] | None = None

    # 可选：**参数表已经是标准 JSON Schema**（外部来源的工具，如 MCP）时原样用它。
    # 为什么需要这个口子：上面那套 `args`（人话类型 str/int/…）翻译出来只有
    # `{type, description}` 两项 —— **丢 enum、丢嵌套对象**。而 MCP server 给的
    # `inputSchema` 本来就是一份完整的 JSON Schema，硬压成 args 会把
    # "这个参数只能填这几个值"这类约束丢掉，模型就容易填错参数。
    # 有它 → `schema()` 原样用；没有 → 照旧从 `args` 翻译（既有技能零改动）。
    # 内容应当是 **parameters 那一层**（`{"type":"object","properties":{…},"required":[…]}`），
    # 不是整个 function object。
    input_schema: dict | None = None

    def leaf(self) -> str:
        """去掉命名空间，只要最后一段：demo.now → now。

        给 registry/前端显示短名字用；模型那边永远用完整名 demo.now。
        """
        return self.name.split(".")[-1]      # "demo.now".split(".") → ["demo","now"]，取 [-1]

    def schema(self) -> dict:
        """把“人话说明”翻译成 OpenAI 认识的 JSON（function calling schema）。

        这段是给模型看的“工具使用说明”：模型据此知道有哪些工具、每个参数要不要、类型是啥。
        结构是一个很标准的嵌套 dict，别被吓到，跟着注释读一遍就会了。
        """
        # 外部来源的工具（MCP）参数表本来就是 JSON Schema → 原样用，别翻译（会丢信息）
        if self.input_schema:
            params = dict(self.input_schema)
        else:
            params = self._params_from_args()
        # 最外层组装成 OpenAI 要求的 function object
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": params}}

    def _params_from_args(self) -> dict:
        """把「人话参数清单」翻译成 JSON Schema 的 parameters 那一层。"""
        props: dict = {}             # props = 每个参数名 → 它的类型和描述
        required: list[str] = []     # required = 必填参数名列表
        # 遍历我们声明的参数清单（比如 [{name:"text", type:"str", desc:"…", required:True}]）
        for a in self.args:
            # 组装的参数说明：type 要换成模型认识的英文（查 _T 表）
            props[a["name"]] = {
                "type": _T.get(a.get("type", "str"), "string"),   # .get(k,默认) 防键缺失
                "description": a.get("desc", ""),
            }
            # 若声明了必填（默认必填），就把参数名加进 required 列表
            if a.get("required", True):
                required.append(a["name"])
        return {"type": "object",        # 参数整体是一个 JSON 对象
                "properties": props,     # 每个参数的具体定义
                "required": required}
