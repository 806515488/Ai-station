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

每个人的需求清单都会一直长：写周报、整理档案、生视频、一个 coding 小助手……

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
- **模型只经一层适配** —— 换模型、接本地 Ollama、接 MCP，只动 `src/station/core/model.py`。

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
    │             └── 全局工具（每个会话都注入）      │
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

配置优先级：**环境变量 > `src/.env` > 界面「⚙ 模型配置」（存库、按用户分）**。

| 变量 | 说明 |
|---|---|
| `GLM_API_KEY` / `QWEN_API_KEY` / `DEEPSEEK_API_KEY` | 三家 OpenAI 兼容端点的密钥，任配其一即可 |
| `TEXT_CHANNEL` / `VISION_CHANNEL` | 文本槽 / 视觉槽默认走哪家 |
| `STATION_FAKE` | `1` = 用离线假模型冒烟，不烧 key |
| `STATION_AUTO_APPROVE` | `1` = 危险工具免确认（**仅本机调试**） |
| `STATION_MAX_STEPS` | agent 一轮最多走多少工具步 |
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
├── files/            产物区
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

测试夹具会把数据目录与 SQLite 都指到临时目录，所以**可以随便跑**，不会污染你的真实数据。

---

## 🐳 部署

仓库自带 `Dockerfile` 与 `docker-compose.yml`：

```bash
docker compose up -d --build
```

要点（都是踩过的坑，写在文件注释里）：

- 容器里必须绑 `0.0.0.0`（绑 `127.0.0.1` 则容器外连不进来，且**不报错**）
- `data/` 是唯一持久状态（SQLite + 用户文件 + 产物），**必须挂卷** —— 不挂的话容器一删数据全没，而且是静默的
- `mem_limit` 给一个 cgroup 硬上限：超了死的是容器、一眼看得出；没有它内核会在宿主机上**随机挑受害者**
- `.dockerignore` 把 `data/`、`samples/` 挡在镜像外（镜像层删不干净，数据绝不能烤进去）

---

## 📄 License

[MIT](LICENSE)
