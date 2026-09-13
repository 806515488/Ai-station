"""station.core —— 宿主内核：事件 / 工具 / 上下文 / 会话(Thread) / 模型适配 / agent 循环。

新手视角：core 是最“纯技术”的一层——它不关心业务(档案/周报)，只提供
“让一个 AI 能反复调工具干活的通用骨架”。学它 = 学 Agent 本身。
按阅读顺序：tool → session → model → agent（入口）→ events/guards/context 配角。
"""
