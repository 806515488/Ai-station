"""MCP 客户端 —— 把外部 MCP server 的工具，像技能一样挂进本宿主的对话。

新手视角（Java 朋友版）：
  MCP（Model Context Protocol）是一套「AI 应用 ↔ 工具服务」之间的通用协议。
  一个 MCP server = 一堆工具的提供方；客户端连上它、问「你有哪些工具」(tools/list)、
  再点名调用 (tools/call)。本包就是那个**客户端**。

  「像技能一样挂进对话」不是比喻：`bridge.py` 真的会把一个 MCP server 合成成一个
  `Skill` 对象塞进技能注册表（`station.skills.registry`），于是路由、工具面、前端
  卡片……宿主这一整套设施一行都不用改，MCP 的工具就进了对话。

本包的四块（各管一件事，互相不绕）：
  config.py   —— 「有哪些 MCP server」：代码里写死的静态表 + 用户自己配的（落 DB）
  client.py   —— 「怎么连、怎么调」：官方 mcp SDK 的**同步桥**（重点看这个文件的注释）
  bridge.py   —— 「连出来的工具怎么变成技能」：合成 Skill（只读缓存，不连网）
  servers/    —— 我们自己写的**内置** MCP server（stdio 启动，如 websearch）

★ 一条安全地基（别放松）：
  **用户能配的传输只有 http / sse，配不了 stdio。** stdio 意味着「在服务器上起一个进程」，
  而这个站是公网可达的 —— 放开 stdio 等于给任意注册用户一个 RCE 端点。
  所以 stdio 只留给**代码里写死的**那几个（`config.BUILTIN_SERVERS`）。
  这条边界在 `config._validate` 里卡着，并且有测试直接钉它。
"""
from __future__ import annotations

__all__ = ["config", "client", "bridge"]
