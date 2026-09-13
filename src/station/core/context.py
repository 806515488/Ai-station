"""Context —— 一次“宿主替一个 skill 干活”的运行上下文，跨 agent 循环/tool/job 传递。

承载：所属 thread、skill、key 白名单（不发全局）、递归深度、事件收集、数据目录。
tool 的 run(ctx, **args) 拿到的第一个参数就是它。

新手视角：Context = 干活时的“随身包/工牌”。
  - 每个工具函数都先收 ctx —— 它告诉你“我属于哪次会话、数据放哪、允许用哪些 key”。
  - allowed_keys：技能要用云 key 时走 ctx.key("GLM_API_KEY")，只有白名单内的名字才放行
    —— 防止技能乱读环境里所有密钥（权限最小化，安全第一课）。
"""
from __future__ import annotations

from dataclasses import dataclass, field    # field(default_factory=...) 给可变默认值用
from pathlib import Path                    # Path 是“路径对象”，比字符串拼路径好用
from typing import Any


@dataclass
class Context:
    """一次运行的上下文。

    用 dataclass 声明，字段类型写在后边（类型注解），作用像文档，IDE 也能提示。
    thread/skill_id 等谁创建谁填（一般是 server.py 的 _ctx() 或 manager 创建）。
    """
    thread: Any = None                      # 属于哪次对话（core.session.Thread），可为空(job)
    skill_id: str = ""                      # 正在给哪个技能干活（技能的 id，如 weekly-report）
    data_dir: Path | None = None            # 这个技能自己的数据目录（宿主按 skill 分目录）
    allowed_keys: list[str] = field(default_factory=list)  # 允许该技能读取的 env key 名单
    depth: int = 0                          # 递归深度：agent 委派给子 agent 用（防无限嵌套）
    auto_approve: bool = False              # 是否自动放行危险工具（仅本机调试）
    user_id: str = ""                       # 归属用户（job 场景由 manager 从 job 带过来；工具落库归属用）
    job_id: str = ""                        # 后台任务号（job 场景由 manager 带上，落现场/审计用）
    system: str = ""                        # 技能系统提示词（server 从 system.md 读来挂上）
    events: list = field(default_factory=list)   # 事件收集桶（也可由 agent 生成器直接 yield）

    def key(self, name: str) -> str:
        """只在白名单内取 key；越权返回 ''，不把 secret 泄露给技能。

        因为技能代码在“宿主进程里”跑，理论上能读 os.environ 全部变量；
        所以凡是要用云 key 的地方，都必须走 ctx.key(...) 这道“门卫”，
        宿主检查 name 在不在 allowed_keys 里才给值。
        """
        import os                            # 用到再 import，Python 惯例
        if name in self.allowed_keys:
            # 找不到就返回 ""（不是 None），调用方好判断“没配 key”
            return os.environ.get(name, "") or ""
        return ""
