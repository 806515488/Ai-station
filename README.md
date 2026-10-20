<div align="center">

# station

**模型是商品，harness 是护城河。**

一个可持续挂能力的 **agent 宿主**：通用 agent 运行时 + 技能注册表 + 异步任务 + 文件产物 + Web UI。
业务能力以「技能」形式热插拔 —— 宿主不认识业务，只认识技能。

[![CI](https://github.com/806515488/Ai-station/actions/workflows/ci.yml/badge.svg)](https://github.com/806515488/Ai-station/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Dependencies](https://img.shields.io/badge/frontend-zero--dependency-orange)

</div>

---

## 为什么做这个

每个人的需求清单都会一直长：写周报、整理档案、面试复盘、生视频、一个 coding 小助手……

**每个需求都做一个独立 App 是死路** —— 对话、工具调用、任务进度、文件产物、权限确认，这些东西每个 App 都要重写一遍。

station 的做法是把它们抽成**宿主基建**，需求侧只写一个**技能**：

```
新需求  →  skills/<id>/ 加一个目录（清单 + 说明 + 可选自带代码）
        →  宿主自动发现、装载、路由、注入工具、跑长任务、渲染结果
```

宿主不认识任何业务，只认识技能。所以加一个能力的成本，从「做一个 App」降到「写一个目录」。

---

## ✨ 特性

### 自研 agent harness

- **工具命名空间作用域** —— 工具全名是 `<ns>.<tool>`。路由命中某个技能后，**只有该技能的工具对模型可见**，其他技能的工具根本不出现在 tool schema 里。技能多了也不会互相污染、不会白烧 token。
- **in-band 批准闸** —— 危险工具（写文件、对外动作）被调用时**跨回合挂起**：agent 停下来向用户请求批准，用户下一条消息才决定执行还是拒绝。不是弹窗，是对话流里的一张卡片。
- **渲染即工具** —— 工具返回 `{text, render}`：`text` 进模型的上下文，`render` 变成对话流里的卡片（表格 / 图 / 文件 / 待办…）。**UI 不是预先铺好的页面，而是 agent 的输出通道。**
- **模型适配** —— 通用聊天走 `src/station/core/model.py`；意图识别支持普通聊天接口及 Jev System One 专用决策接口。
- **MCP 客户端** —— 接外部 MCP server 的工具。MCP 工具直接进工具的常驻面，和内置全局工具并排。
  - **安全边界**：用户能配的只有 `http` / `sse`（填个地址），**配不了 `stdio`** —— 那等于让服务器起任意进程，而这是个公网可达的站。内置服务才允许 stdio，且命令写死在代码里。

### 技能开放格式

```
skills/<id>/
├── manifest.json     # id / ns / type / entry / keys / model
├── SKILL.md          # 给人看的能力说明
├── system.md         # 可选：给模型的岗位须知（每轮注入 system）
└── code/             # 可选：技能自带代码
```

一个技能 = 一个 entry 模块，**暴露什么就挂什么**：

| 模块里有 | 宿主怎么用 | 适合 |
|---|---|---|
| `tools()` | 进对话：模型可以调用 | 开放探索、需要和用户来回 |
| `build_runner()` | 挂后台：宿主当 job 跑 | 长活（识别、生成、渲染） |
| 两个都有 | 对话面 + 执行器同属一个技能 | 有交互也有长任务 |

> 长活**不要**挤进一次工具调用（HTTP 会超时），也别为它单开一个技能目录 —— 同一个技能里再导出一个 `build_runner()` 即可，由工具内部起 job。

### 运行时

- **三层路由漏斗** —— L1 关键词秒判 → L2 小模型（带墙钟预算，超时降级）→ L3 通用 agent 兜底。只有命中的技能才加载工具面。
- **对话执行追踪** —— 消息内查看执行时间线，关联路由、模型尝试、降级重试、工具和后台任务；刷新后可回看，只保存有界元数据。已有 PostgreSQL 部署需先停站运行 `station-upgrade-postgres`，再启动新代码。
- **按模型配置能力** —— 「来源与模型」统一保存地址和 Key，一个来源可添加多个模型；「功能分配」按对话、意图、识图、语音、视频选择具体模型及主备顺序。参数按能力显示，逐模型测试，连接设置默认折叠。
- **可选 Jev 意图识别** —— 在模型配置中添加 OpenCode Zen · Jev，填 Key 后加入意图识别顺序。使用 `jev-1.13-free` 和专用 `/systemone` 接口，保留原模型作备用；免费模型限时提供，不自动切换付费版本。
- **原生网页检索** —— 全局 `web.search` / `web.fetch` 直接调用官方 DeepSeek Search/Fetch，复用本人来源的 Key，支持连接复用、短期缓存和长文分页。模型配置中可开关、选择来源并测试搜索；旧 Bing 等 HTML 搜索 MCP 默认停用，其他 MCP 不受影响。
- **通用文档附件** —— LangChain Loader → Unstructured 基础清洗 → 完整解析与独立切块；支持 TXT/MD、CSV/TSV、文字 PDF、DOCX、基础 XLSX/HTML。结果按 `orignChunk / 日期 / 文档 / 版本` 保存，可预览和下载；首版不包含 OCR、复杂表格重建或向量库写入。
- **异步任务** —— `queued → running → done | failed`，进度事件落盘、前端轮询、产物进文件区。崩了重启能找回。
- **重活闸** —— 全站同一时刻只跑 N 件吃内存的任务（默认 1）。把 OOM 从「服务无告警消失」变成可控排队，且排队原因对用户可见。
- **产物区 + 预览** —— 生成的文件按产出分组，对话里可点开、可下载。

---

## 🏗 架构

```
                      浏览器（单文件前端 · 零外部依赖）
                             │  SSE / fetch
                             ▼
    ┌──────────────────  FastAPI  ──────────────────┐
    │  会话 · 上传 · 产物 · 页面图 · 任务轮询         │
    └───────────────────────┬───────────────────────┘
                            ▼
                  路由漏斗  L1 关键词 → L2 小模型 → L3 通用 agent
                            │
                            ▼
    ┌───────────────  agent 循环  ──────────────────┐
    │  工具调用 ──┬── 技能工具（按技能作用域隔离）    │
    │             ├── 全局工具（每个会话都注入）      │
    │             └── MCP 工具（外部服务，不走路由）  │
    │                                               │
    │  批准闸    approve 类工具跨回合挂起 / 恢复      │
    │  任务     长活交给 JobManager 后台跑            │
    │  渲染     返回值带 render → 卡片进对话流        │
    └───────────────────────┬───────────────────────┘
                            ▼
                   技能注册表（扫目录即发现）
```

---

## 🚀 快速开始

```bash
git clone https://github.com/806515488/Ai-station.git && cd Ai-station

cp .env.example src/.env     # 填任意一家的 API Key
pip install -e ".[dev]"      # 需要 Python ≥ 3.11

station-web                  # → http://127.0.0.1:8001
```

**没有 key 也能跑。** 用离线假模型冒烟整条 harness 链路（对话 → 工具 → 渲染，不烧一分钱）：

```bash
STATION_FAKE=1 station-web
```

开发模式（不安装，直接在仓库根跑）：

```bash
PYTHONPATH=src python -m station.app.server
```

启用文档附件需在联网准备阶段安装组件并准备本地资源，之后解析、清洗和切块可离线运行：

```bash
pip install -e ".[documents]"
python scripts/prepare_document_resources.py
```

---

## 📖 使用

打开页面就是**统一主对话** —— 说需求即可，路由会自动切到对应技能。

```
你：帮我写本周周报
    → 自动收集笔记目录 + 本仓 git 提交 → 生成 .md（可下载）

你：上传照片
    → 弹出上传卡片，在你自己的电脑上选整卷翻拍照片
你：开始识别
    → 后台跑，进度面板逐张刷新
你：把 xx 改成 九-2 / 这两份并了 / 第 41 页换一家 OCR 对比
    → 逐个核对
你：出件
    → 批准卡片确认 → 4 件套以文件卡片出现在对话里，可下载

你：我想做一次面试复盘，视频应该怎么处理？
    → 给出 FFmpeg 官方下载入口和本地提取音频命令
你：上传面试音频
    → 上传本地提取出的音频，后台调用语音识别，返回带说话人和时间戳的转写
你：这是我整理好的问答/文字稿
    → 逐题讨论，先听你的判断，再结合原回答给出改进建议
```

---

## 🧩 写一个技能

**1.** 建 `skills/my-tool/manifest.json`：

```json
{
  "id": "my-tool", "ns": "my", "name": "我的技能",
  "description": "一句话说明它能干什么",
  "type": "agent", "entry": "my_skill",
  "keys": [], "model": "text", "system": "system.md"
}
```

**2.** 同目录放 `SKILL.md`（给人看）和可选的 `system.md`（给模型的流程与守则）。

**3.** 写入口代码 `code/my_skill.py`：

```python
from station.core.tool import Tool

def t_hello(ctx, name: str = "world") -> dict:
    # 返回 {text, render}：text 给模型，render 给前端渲染成卡片
    return {"text": f"你好，{name}",
            "render": {"type": "text-card", "body": f"你好，{name}"}}

def tools() -> list[Tool]:
    return [Tool(name="hello", label="打个招呼",
                 description="向用户问好。",
                 run=t_hello,
                 args=[{"name": "name", "type": "str", "desc": "称呼",
                        "required": False}])]
```

写完重启即可，宿主扫目录自动装载。

---

## ⚙️ 配置

模型来源及备用顺序在界面「⚙ 模型配置」按用户保存。来源共享连接与密钥，下设多个模型；各功能选择具体模型及备用顺序。密钥优先使用本人保存的值；未填时仅站长可回落到环境变量／`src/.env`。环境变量优先于 `.env`。

旧配置可直接读取；新界面保存后使用 version 2 JSON，无数据库表变更。共用同一数据库的所有实例应先升级再保存；旧程序无法读取多模型配置，回退前需恢复旧配置备份。

| 变量 | 说明 |
|---|---|
| `GLM_API_KEY` / `QWEN_API_KEY` / `DEEPSEEK_API_KEY` | 三家 OpenAI 兼容端点的密钥，任配其一即可 |
| `TEXT_CHANNEL` / `VISION_CHANNEL` / `VIDEO_CHANNEL` / `SPEECH_CHANNEL` | 通用 / 识图 / 视频 / 语音识别槽默认走哪家；语音识别没有内置服务商，需先添加自己的兼容来源 |
| `STATION_FAKE` | `1` = 用离线假模型冒烟，不烧 key |
| `STATION_AUTO_APPROVE` | `1` = 危险工具免确认（**仅本机调试**） |
| `STATION_MAX_STEPS` | agent 一轮最多走多少工具步 |
| `STATION_MCP_TIMEOUT` | 单次 MCP 调用最多等几秒（默认 30） |
| `STATION_HOST` / `STATION_PORT` | 监听地址与端口（默认 `127.0.0.1:8001`） |
| `STATION_SSL_KEYFILE` / `STATION_SSL_CERTFILE` | 两个都给才开 HTTPS |

---

## 📂 目录结构

```
src/station/          宿主
├── core/             agent 循环 · 工具 · 会话 · 事件 · 路由 · 模型适配
├── skills/           技能注册表（扫目录装载）
├── jobs/             异步任务（状态机 + 落盘 + 进度事件）
├── tools/            全局工具（每个会话都注入）
├── mcp/              外部工具接入；保留的旧搜索 server 默认停用
├── search/           原生 DeepSeek Search/Fetch 适配、缓存和预算
├── files/            产物区
├── documents/        附件解析、清洗、质量检查、切块及持久化
└── app/              FastAPI + static/index.html（单文件前端）

src/archive/          第一个技能的业务实现
├── domain/           领域模型与分类体系
├── engine/           LangGraph 识别流水线
├── service/ storage/ export/
└── station_adapter.py  与宿主的接缝

skills/               技能开放目录
tests/                离线单测
```

---

## 🧪 测试

```bash
pytest -q                       # 全部离线，不联网、不烧 key、不碰真数据
pytest -q -k 批准               # 按关键字挑
pytest tests/test_deploy.py -q  # 单个文件
```

测试夹具会把数据目录与数据库（SQLite）都指到临时目录，所以**可以随便跑**，不会污染你的真实数据。
真 PostgreSQL 那条路另有集成测试（`tests/test_postgres_integration.py`，默认跳过，给了
`STATION_TEST_DATABASE_URL` 才跑）。

---

## 🐳 部署

仓库自带 `Dockerfile` 与 `docker-compose.yml`：

```bash
docker compose up -d --build
```

要点（都是踩过的坑，写在文件注释里）：

- 容器里必须绑 `0.0.0.0`（绑 `127.0.0.1` 则容器外连不进来，且**不报错**）
- `data/` 装着用户文件与产物，**必须挂卷** —— 不挂的话容器一删数据全没，而且是静默的
- 数据库：**生产用 PostgreSQL**（compose 里的 `postgres` 服务，数据卷 `pgdata`，**同样必须挂卷**），
  应用靠 `STATION_DATABASE_URL` 连它；**留空则回落 SQLite**（本机开发与离线测试走的就是这条路，
  库文件在 `data/station/station.db`）。老的 SQLite 数据用 `station-migrate-postgres` 一次性导入
- `mem_limit` 给一个 cgroup 硬上限：超了死的是容器、一眼看得出；没有它内核会在宿主机上**随机挑受害者**
- `.dockerignore` 把 `data/`、`samples/` 挡在镜像外（镜像层删不干净，数据绝不能烤进去）
- 附件 NLTK 资源需要预先准备到宿主机 `/srv/station/nltk`，由 Compose 只读挂到 `/opt/station-nltk`。可运行 `python scripts/prepare_document_resources.py --output-dir /srv/station/nltk`；离线服务器需从同平台准备环境复制资源。
- 既有 PostgreSQL 缺表时，先停掉所有应用实例，再执行 `docker compose run --rm --no-deps station station-upgrade-postgres`，最后起站；应用启动不会隐式升级已有库。仅新增 Jev 配置不需要升级数据库。

---

## 📄 License

[MIT](LICENSE)
