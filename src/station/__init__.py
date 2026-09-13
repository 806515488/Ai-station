"""station —— 个人 AI 工作站宿主（宿主内核，不依赖任何业务 skill）。

分层：core(agent/tool/context/session/model/events) < skills(注册表) < jobs/files < app(Web)。
业务能力以 skill 形式挂进 skills/ 注册表，宿主不认识业务。

新手视角（Java 朋友版）：这个文件让 station 成为一个“包”（可以 import）。
  - __init__.py 好比 Java 的 package-info / 包声明：目录里放它，Python 才认这是个包。
  - 层次从下往上看：core(最底层抽象) → 上层宿主功能 → app(最外层 Web)。
"""
__version__ = "0.1.0"
