# archive（干部档案整理）

`agent` 型技能，用对话完成一位干部人事档案卷的整理：上传翻拍照片、整卷识别、人工核对、批准出件。它同时暴露工具面和后台执行器；用户不需要进入固定表单或分拣页面。

## 能力与工作流

1. 用户在对话中要求上传照片，技能发出上传卡片；上传后以姓名建立一卷档案现场。
2. 用户说“开始识别”，`archive.recognize` 创建后台任务。执行器按 `mark → segment → resolve → classify → build_mats` 识别整卷，进度由任务卡片展示。
3. 识别结束后，用户可看十类全景、查看原页、修改类别或标题、合并/拆分材料、换一家模型重读某页、调整照片方向，或退回检查点。
4. 用户确认无误后请求出件。`archive.export` 经过宿主批准闸，生成目录 Excel、按目录排序的原件 PDF、待核对清单和自动判定报告四件套。
5. 用户也可上传自己改过的最终目录 Excel 进行对账。系统仅提出可学习的口径建议；只有用户明确确认并通过批准闸后，才会写回口径。

## 数据与模型边界

- 每卷现场按“项目 × 识图模型”保存；切换到已跑过的模型只切换结果，不重复识别。
- OCR 缓存键包含图片内容、模型通道和提示词指纹。除非用户明确要求刷新，不应重跑缓存。
- 识别口径、文种对照和提示词在本目录的 `口径/` 与 `prompts/`，不是写死在引擎里。改识别规则优先修改这些文件，并同步遵守 `docs/rules.md`。
- 档案的中间现场、任务和产物由宿主分别管理；本技能不应直接把真实档案数据写入仓库。

## 人工确认与安全

- 导出四件套与写入学习后的口径都是 `approve` 操作。模型可以提出建议，但不能绕过用户批准。
- `apply_learning` 只接收要采纳的文种和类别，不接收任意文件路径或文件内容；实际条文由代码计算，且只写入固定的 `口径/文种对照.md`。
- 归类、页方向、材料名称和合并/拆分是可核对的现场编辑；编辑前会保存检查点，可用 `checkpoints` 回退。

## 工具面概览

- 建卷与识别：`ask_photos`、`scan_photos`、`recognize`、`job_status`。
- 模型与查看：`list_vision_models`、`use_model`、`list_state`、`show_page`、`show_overview`、`show_files`。
- 核对与修正：`set_orientation`、`set_category`、`merge`、`split`、`reocr`、`checkpoints`。
- 出件与学习：`export`、`reconcile`、`list_learning`、`apply_learning`。

工具实现位于 `code/archive_tools.py`；耗时识别由同一模块暴露的 `build_runner()` 转发至 `skills/archive/code/archive_core/station_adapter.py`，由宿主任务管理器运行。

## 维护入口

- 分类体系和材料契约：`skills/archive/code/archive_core/domain/`。
- 识别图：`skills/archive/code/archive_core/engine/graph.py`。
- 现场编辑与口径学习：`skills/archive/code/archive_core/service/`。
- 四件套导出：`skills/archive/code/archive_core/export/`。
- 产品规则与当前校准结论：`docs/rules.md`、`docs/ARCHITECTURE.md`、`docs/识别引擎现状与校准记录.md`。

修改口径后，应确认代码侧分类常量与口径没有脱节，并运行相关离线测试；不要以修改引擎代码替代可配置的提示词或口径改动。
