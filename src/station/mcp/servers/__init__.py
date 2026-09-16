"""我们自己写的**内置** MCP server 放这里。

它们和本仓库同生共死（代码就在旁边），所以：
  · 只有它们能用 **stdio** 启动（用户配不了 stdio，见 `station/mcp/config.py` 的说明）；
  · 它们的工具清单**手写在** `config.BUILTIN_SERVERS` 里，有测试比对两边别漂移。

启动方式统一是 `python -m station.mcp.servers.<名字>`（父进程会注入 PYTHONPATH）。
每个文件都能**单独跑**来手工调试，比如：

    PYTHONPATH=src <conda python> -m station.mcp.servers.websearch
    # 然后手工往里敲一行 JSON-RPC（或者干脆用 MCP 官方的 inspector）
"""
