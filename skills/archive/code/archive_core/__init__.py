"""archive_core —— archive 技能内部的干部档案业务实现。

layers: domain / skill / engine / service / storage / export / imaging / config

新手视角（Java 朋友版）：archive 是一个“领域包”（≈一个业务 module/子工程）。
它的顶层 __init__.py 只作说明，真正能力分布在各子包：
  外部入口是 skills/archive/code/archive_core/station_adapter.py（被站内 skills/archive 的 build_runner 挂载 ——
  工具面与识别执行器同属那一个技能，见 docs/工作站架构设计.md §2 的 09-12 修订）。
宿主通过技能入口调用能力；本包使用宿主提供的配置、数据库和模型条目等基础设施。
"""
