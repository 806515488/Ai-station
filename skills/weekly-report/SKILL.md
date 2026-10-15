# weekly-report（写周报）

agent 型示例技能，验证“宿主扩展 + 多工具串联 + 产物落盘”。

- 类型：`agent`（通用工具循环）
- 代码：本目录 `code/weekly_report.py`（**skill 自带代码**，registry 自动把 `code/` 放上 import 路径）
- 工具（命名空间 `weekly.*`）：
  - `collect_highlights`：整理当前对话中用户已经提供的工作要点
  - `render_report`：内容渲染成《工作周报》.md → 落 station 文件区，返回下载链接
  - `export_docx`：把 md 转 .docx（可选，需 python-docx）
- 真实使用：直接说“帮我写周报”，再在对话里补充本周完成、数据/进展、问题和下周计划；它不会读取你电脑文件、服务器目录或 Git 提交。

在宿主里新建一个能对话干活的能力 = 在 `skills/<id>/` 放 manifest.json + SKILL.md + 代码，宿主自动发现。
