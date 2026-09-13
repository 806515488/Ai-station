"""station.tools —— 宿主级**全局工具**（与技能无关、每个会话都挂）。

## 为什么要有"全局工具"这一层

本宿主的既定原则是「**技能工具不是全局的**」（见 app/unified.py）：路由阶段只读
技能的名称/描述，命中谁才把谁的工具给模型。这条原则是对的 —— 不然每个技能的工具
都往上下文里塞，模型会被一堆用不上的工具搞糊涂。

但它留了个真空：**没命中任何技能时，模型手上一件工具都没有**（`GenericAgent` 的
tools 是空的）。于是"看下 docs/status.md"、"把这段记下来"、"我刚才导出的东西呢"
这类**不属于任何业务**的请求，它只能干说。

所以这一层补的就是这个真空：一批和业务无关、**任何对话都用得上**的能力。
命名空间固定是 `station.`，跟技能工具（`archive.` / `weekly.`）天然不撞名。

## 装了什么（共 4 个，09-13 精简过）

| 文件 | 工具 | 干什么 |
|---|---|---|
| `memory.py` | `remember` `recall` | 长期记忆（跨会话，落 `data/station/memory/`） |
| `selfview.py` | `skills` `artifacts` | 我会干啥 / 我产出过什么 |

**★ 09-13 删掉了原来的"档一 文件底座"（`fs.py` 的 `ls`/`read`/`grep`/`write`）
和 `selfview.show`。** 理由是它们属于同一类：**让 AI 看"工作站自己"**（仓库里的
文档/源码/图片），而实际的业务场景（档案整理）用不到 —— 用户的文件是**上传**上来的
（`POST /api/photos`），跟"服务器仓库"没关系。删掉它们之后：

- `tools/guard.py` 的路径闸**整个删了**（它只为这几个工具存在）
- `/api/station/file` 只读端点也删了（它只为 `show` 推图存在）—— 少一条对外可读仓库的口子
- `STATION_FS_WRITE=0` 这个"公网部署一键关写"的开关**从这一层挪走了**（这儿没有东西可关）；
  但"写仓库里的文件"这件事后来又在技能里出现（archive 的口径文件），所以开关**没作废**
  —— 它现在挂在那个唯一写口上（`archive.service.kouju.write_text`）。公网实例照旧配个
  环境变量就能彻底关写
- 全局工具从 9 个降到 4 个，每轮省下约 0.8k token 的 schema

★ 教训（别急着加回来）：**"AI 能读写服务器上的文件"不是通用能力，是场景依赖的。**
将来真要做 coding 助手那类技能，文件工具应该挂在**那个技能**下（或做成客户端执行器，
见 `docs/工作站架构设计.md`），而不是提成"人人都有"的全局工具 —— 对档案用户它是
纯成本，还多开一个写服务器仓库的口子。

## 上下文成本（加东西之前先想这条）

全局工具的 schema **每一轮对话都在烧**（`core/agent.py` 的循环每步都把 tools 发出去）。
现在 4 个约 0.4k token/轮。再往上加就该学 Claude Code 做**按需加载** ——
先只给工具名，模型要用哪个再展开哪个的 schema。
"""
from __future__ import annotations

from station.core.tool import Tool
from station.tools import memory, selfview

# 命名空间固定是 station. —— 跟技能工具（archive. / weekly.）区分开
NS = "station"


def _namespace(raw: list[Tool]) -> list[Tool]:
    """把工具名从叶子补成**完整名**（`ls` → `station.ls`），并记下归属。

    为什么全局工具要自己补前缀：技能那套是在 `skills/registry._load_tools` 里补的
    （`t.name = f"{ns}.{t.leaf()}"`），而全局工具**不走注册表**（它不属于任何技能），
    所以这一步得自己做 —— 少做的话模型看到的是裸名，而 `_tool_label` 按全名回放
    也查不到（静默失败）。

    这一步幂等：`leaf()` 取的是最后一段，重复调用不会变成 `station.station.recall`。
    """
    for t in raw:
        t.name = f"{NS}.{t.leaf()}"
        t.owner = NS
    return raw


# 全局工具清单 —— 模块顶层算一次，进程内共用（Tool 是无状态的说明书+函数，可共享）
GLOBAL_TOOLS = _namespace([
    *memory.tools(),
    *selfview.tools(),
])

# 叶子名 → 归属：remember / recall → tools/memory.py；skills / artifacts → tools/selfview.py


def all_tools() -> list:
    """返回全局工具清单（拷贝一份，防止调用方就地改到全局那份）。"""
    return list(GLOBAL_TOOLS)


def find(name: str):
    """按**完整名**（`station.read`）找一个全局工具；没有返回 None。

    宿主的批准补执行要用它：危险工具挂起后，下一轮要按名字把它找回来执行
    （见 app/server.py 的 `_skill_for_tool`）。
    """
    for t in GLOBAL_TOOLS:
        if t.name == name:
            return t
    return None
