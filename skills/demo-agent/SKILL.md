# demo-agent

agent 型技能的冒烟样例：宿主把一个对话交给它，它通过绑定的工具（`demo.now` / `demo.echo` / `demo.fake_risky`）完成用户请求。

- 类型：`agent`（通用工具循环）
- 入口：`station.demo_agent`（`tools()` 返回工具清单）
- 可试：“现在几点？” / “echo 你好” / “执行 fake_risky”（触发批准闸）

改/加 agent 型技能时照此目录放一份 `manifest.json` + `SKILL.md`。
