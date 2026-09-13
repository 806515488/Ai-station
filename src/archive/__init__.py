"""archive —— 干部人事档案数字化整理业务包（现为个人 AI 工作站的 skill#1）。

layers: domain / skill / engine / service / storage / export / web / imaging / config

新手视角（Java 朋友版）：archive 是一个“领域包”（≈一个业务 module/子工程）。
它的顶层 __init__.py 只作说明，真正能力分布在各子包：
  外部入口是 src/archive/station_adapter.py（被站内 skills/archive 的 build_runner 挂载 ——
  工具面与识别执行器同属那一个技能，见 docs/工作站架构设计.md §2 的 09-12 修订）。
本包与 station 宿主刻意互相解耦：宿主不认识档案，archive 不认识通用 agent。
"""
