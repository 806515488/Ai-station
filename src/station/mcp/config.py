"""MCP 配置 —— 「有哪些 MCP server、各怎么连」。

新手视角（Java 朋友版）：
  这个文件 ≈ 一份「数据源清单 + 它的校验/存取」。两类 server：

  · **静态（内置）**：写在本文件的 `BUILTIN_SERVERS` 常量里，改它要改代码。
    它们是我们自己写的 MCP server（`servers/` 目录下），所以**永远加载**、不需要
    用户配置、也不落库 —— 落库就会有"库里那条和代码里那条不一致"的麻烦。
    ★ 只有内置的可以用 **stdio**（在服务器上起一个进程）。

  · **动态（用户配的）**：用户在界面上自己加，每人一份落 DB（`db.mcp_config_*`）。
    ★ 只允许 **http / sse** 两种传输。

★★ 那条安全边界（全方案的命根子，别放松）：
  用户能填的**只有 URL**。stdio 意味着"在服务器上起任意进程"，而这个站是公网可达的，
  放开它等于给任意注册用户一个 RCE 端点。所以 `_validate` 里对 transport 卡了一道
  白名单，用户配置里出现 stdio 一律拒 —— 并且有测试直接钉这条（`tests/test_mcp.py`）。

  为什么动态的那些要"连一次才知道有哪些工具"、而内置的不用：
  内置 server 的代码就在本仓库里（`servers/websearch.py`），它的工具清单可以直接写在
  表里、由测试比对两边别漂移；用户配的 server 我们看不见，只能连上去问（tools/list），
  结果**缓存**下来（因为**工具面每轮对话都要读一遍**，绝不能在那里连网）。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

from station import db

# 每人最多配几个 —— 防止有人往库里灌几百条把界面/重试拖死（对齐 modelcfg.MAX_PROVIDERS）
MAX_SERVERS = 20

# 传输白名单：**用户能配的**只有这两种（见文件头那条安全边界）
USER_TRANSPORTS = ("http", "sse")

# id 的字符集：它会进前端 DOM 的 id 和内联 onclick，卡死字符集是防注入
# （同 modelcfg._validate 的理由：前端 esc() 只兜得住 HTML，兜不住内联事件里的 JS 字符串）
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# 自定义请求头的写法：每行一条 `名字: 值`（值里可以有冒号，按第一个冒号切）
_HEADER_LINE = re.compile(r"^\s*([A-Za-z0-9!#$%&'*+.^_`|~-]+)\s*:\s*(.*)$")


def parse_headers(text: str) -> dict:
    """把「每行 `名字: 值`」的文本框内容解析成 dict（给 HTTP 客户端用）。

    为什么用文本框而不是一堆输入框：远端 MCP 常要 `Authorization: Bearer xxx` 这类头，
    条数不定、名字不定；文本框一行一条最省事，人也能直接粘贴。
    解析不出来/名字非法的行**直接丢掉**（不报错）—— 校验那一步已经拦过一遍了。
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):     # 空行和 # 注释行跳过
            continue
        m = _HEADER_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def mask_headers(text: str) -> str:
    """把请求头文本里的**值**换成 ••••（回显给界面用，别把密钥发回浏览器）。

    名字留着 —— 用户得看得见"我配过 Authorization 这个头"。值一律打掉：
    这是"有没有配"和"配的是什么"的区别，跟 modelcfg 里 key 的掩码是同一条规矩。
    """
    lines = []
    for line in (text or "").splitlines():
        m = _HEADER_LINE.match(line.strip()) if line.strip() else None
        lines.append(f"{m.group(1)}: {_MASK}" if m else line)
    return "\n".join(lines)


_MASK = "••••"


@dataclass
class ServerSpec:
    """一个 MCP server 的"怎么连"（静态与动态共用这一个盒子）。

    这是本包内部的统一形状：`client` 只认它，不关心它是内置的还是用户配的。
    """
    id: str
    label: str
    transport: str                                  # stdio | http | sse
    command: list[str] = field(default_factory=list)  # 只有 stdio 用（[解释器, "-m", "模块"])
    url: str = ""                                   # 只有 http/sse 用
    headers: str = ""                               # 只有 http/sse 用（每行 `名字: 值`）
    builtin: bool = False                           # 内置的（用户删不掉、改不了）
    enabled: bool = True
    # 工具清单：内置的**写在代码里**；用户配的是**上次连上时的缓存**（tools/list 的结果）
    tools: list[dict] = field(default_factory=list)
    tools_at: float = 0.0                           # 缓存时间戳（0 = 从没连上过）
    error: str = ""                                 # 上次连接失败的原因（给人看的一句话）


# ── 内置（静态）的 MCP server ─────────────────────────────────────────
# ★ 这里的 tools 是**手写的**，必须和 servers/websearch.py 里 FastMCP 注册的工具一致。
#   两处漂移不会有任何报错（工具就是不见了/或者列着一个调不通的名字），所以有测试钉着：
#   tests/test_mcp.py::test_builtin_tool_table_matches_the_real_server
#
#   为什么内置的不用"连一次拿清单"：静态加载的意义就是**开箱即用** ——
#   新用户第一次说话就该有搜索工具，不该先让他点一次"测试连接"。
BUILTIN_SERVERS: list[dict] = [
    {
        "id": "websearch",
        "label": "网页搜索",
        "transport": "stdio",
        # sys.executable = 当前这个解释器。子进程用 `python -m station.mcp.servers.websearch`
        # 启动；PYTHONPATH 由 client 注入（本机没 pip install -e 也能跑起来）。
        "command": [sys.executable, "-m", "station.mcp.servers.websearch"],
        "tools": [
            {
                "name": "web_search",
                "title": "网页搜索",
                # ★ "什么时候用它"要写**具体的例子** —— 这段是给主模型看的，
                #   它每轮都读得到（MCP 工具不走路由，直接进工具面，见 bridge.py 文件头）。
                #   09-16 实测：只写"查资料/找最新信息"时模型不知道该不该搜，写法写具体
                #   一点（天气/新闻/某个东西是什么）它才稳。
                "description": ("用关键词搜网页，返回若干条结果的标题/网址/摘要。"
                                "凡是要查资料、找最新信息、确认某个事实的都该用它 —— "
                                "比如今天的天气、最近发生了什么、某个东西是什么、"
                                "某句话是不是真的。"),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "count": {"type": "integer", "description": "要几条结果（默认 5，最多 10）"},
                    },
                    "required": ["query"],
                },
            },
        ],
    },
]


def builtin_specs() -> list[ServerSpec]:
    """把内置表翻译成 ServerSpec 列表（每次现建，免得调用方改到共享对象上）。"""
    out = []
    for s in BUILTIN_SERVERS:
        out.append(ServerSpec(id=s["id"], label=s.get("label", s["id"]),
                              transport=s.get("transport", "stdio"),
                              command=list(s.get("command") or []),
                              builtin=True,
                              tools=[dict(t) for t in (s.get("tools") or [])]))
    return out


# ── 用户配置：读 / 写 / 校验 ──────────────────────────────────────────

def _clean(p: dict) -> dict:
    """把用户发来的一条 server 收拾成我们认的形状（不认识的键一律丢掉）。

    ★ 用 `if k in p` 而不是"缺了就补空串"：`headers` 的三态靠"字段在不在/是不是 null"
      区分（缺失或 null = 不改 / "" = 清空 / 非空 = 覆盖），补空串会把"不改动"
      误判成"清空"、静默丢掉用户存的密钥 —— modelcfg 的 `_KEEP` 注释里记过这个坑。
    """
    out = {"id": str(p.get("id") or "").strip(),
           "label": str(p.get("label") or "").strip(),
           "transport": str(p.get("transport") or "http").strip().lower(),
           "url": str(p.get("url") or "").strip()}
    if "headers" in p and p.get("headers") is not None:
        out["headers"] = str(p["headers"])
    # ★ 工具清单缓存**不在这里取**（下面 _merge 从库里那份搬过来）。
    #   理由：工具清单决定"模型手上有什么武器"，只许由"测试连接"成功时由服务端写入
    #   （config.set_cache）；能从前端提交的话，谁都能凭空捏造一批工具塞给模型。
    return out


def _merge(prev: list[dict], incoming: list[dict]) -> list[dict]:
    """把新提交的清单与库里那份合并：**保缓存** + headers 三态。

    两件事：
      1) 用户没动过的 server，它的工具清单缓存要**原样活下来** —— 否则每次点保存都会
         把"上次连上看到的工具"清空，用户得重新点一次测试连接才又能用（很难查）。
      2) headers 三态：字段缺失/为 null = 沿用库里那份；"" = 清空；非空 = 覆盖。
    """
    old = {s.get("id"): s for s in prev or [] if isinstance(s, dict)}
    out = []
    for p in incoming:
        c = _clean(p)
        o = old.get(c["id"]) or {}
        if "headers" not in c:                     # 没提交这个字段 → 沿用库里的
            c["headers"] = o.get("headers", "")
        if not c.get("tools") and o.get("tools"):  # 保缓存（见上）
            c["tools"] = o["tools"]
            c["tools_at"] = o.get("tools_at") or 0
        out.append(c)
    return out


def _validate(servers: list[dict]) -> list[str]:
    """校验用户配置，返回**人话**问题清单（空 = 通过）。错误文案直接给用户看。"""
    problems: list[str] = []
    if not isinstance(servers, list):
        return ["配置格式不对：servers 必须是一个列表"]
    if len(servers) > MAX_SERVERS:
        problems.append(f"最多只能配 {MAX_SERVERS} 个 MCP 服务")
    builtin_ids = {s["id"] for s in BUILTIN_SERVERS}
    seen: set[str] = set()
    for i, p in enumerate(servers, 1):
        # ★ 报错时怎么称呼这一条：有名字用名字；**没名字就说"第 N 个服务"，别拿 id 顶上**
        #   —— 新建的卡片 id 是随机串（mcp-01f66c9d），把它摆在报错最前面，用户根本对不上
        #   是自己刚加的那张卡（真机上就是这么显示的，看到了才改）。
        name = p.get("label") or f"第 {i} 个服务"
        sid = p.get("id") or ""
        if not _ID_RE.fullmatch(sid):
            problems.append(f"{name}：标识只能用字母数字和 . _ -（1~64 个字符）")
        elif sid in seen:
            problems.append(f"{name}：标识 {sid} 重复了")
        elif sid in builtin_ids:
            problems.append(f"{name}：标识 {sid} 与内置服务同名，换一个")
        seen.add(sid)

        if not p.get("label"):
            problems.append(f"{name}：名字不能为空")

        tr = p.get("transport") or ""
        if tr not in USER_TRANSPORTS:
            # ★ 这条就是那道安全边界。写清楚"为什么"，免得以后有人以为放开是顺手的事。
            problems.append(f"{name}：只支持 http / sse 两种传输"
                            f"（stdio 只能在代码里内置，界面上配不了）")
            continue
        if not re.match(r"^https?://[^\s/]+", p.get("url") or ""):
            problems.append(f"{name}：接口地址要以 http:// 或 https:// 开头")
    return problems


def load_servers(user_id: str) -> list[dict]:
    """读某用户自己配的 server 清单（没配过返回空列表）。"""
    cfg = db.mcp_config_load(user_id) or {}
    servers = cfg.get("servers")
    return [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []


def save_servers(user_id: str, incoming: list[dict]) -> list[dict]:
    """保存用户配的 server 清单；校验不过抛 ValueError（内含人话理由）。

    返回收拾好的那份（路由层拿它回给前端，省得再读一次库）。
    """
    prev = load_servers(user_id)
    merged = _merge(prev, incoming)
    problems = _validate(merged)
    if problems:
        raise ValueError("；".join(problems))
    db.mcp_config_save(user_id, {"version": 1, "servers": merged})
    return merged


def delete_servers(user_id: str) -> None:
    """清空用户配的 server（内置的那几个不受影响，它们不在库里）。"""
    db.mcp_config_delete(user_id)


def set_cache(user_id: str, server_id: str, tools: list[dict], error: str = "") -> None:
    """把"这次连上看到的工具清单"写回缓存（**唯一的写入口**，只由测试连接调用）。

    写口只有这一处是刻意的：工具清单能决定"模型手上有什么武器"，绝不能让前端提交。
    """
    servers = load_servers(user_id)
    hit = False
    for s in servers:
        if s.get("id") == server_id:
            s["tools"] = tools
            s["tools_at"] = _now()
            s["error"] = error
            hit = True
    if not hit:
        return                                  # 不是用户配的（内置的）→ 不落库
    db.mcp_config_save(user_id, {"version": 1, "servers": servers})


def _now() -> float:
    """当前时间戳（秒）。单独成函数是为了测试里好替换。"""
    import time
    return time.time()


# ── 合成：屏幕/客户端真正要用的那一份 ────────────────────────────────

def all_specs(user_id: str) -> list[ServerSpec]:
    """**内置 + 用户配的** 合在一起，翻译成 ServerSpec（client/bridge 认的形状）。

    这就是"静态加载"和"动态加载"合流的地方 —— 往下一层看，两者没有任何区别。
    """
    out = builtin_specs()
    for s in load_servers(user_id):
        out.append(ServerSpec(id=s.get("id") or "",
                              label=s.get("label") or s.get("id") or "",
                              transport=s.get("transport") or "http",
                              url=s.get("url") or "",
                              headers=s.get("headers") or "",
                              builtin=False,
                              tools=[t for t in (s.get("tools") or []) if isinstance(t, dict)],
                              tools_at=float(s.get("tools_at") or 0),
                              error=s.get("error") or ""))
    return out


def find(user_id: str, server_id: str) -> ServerSpec | None:
    """按 id 找一个 server（找不着返回 None）。"""
    for s in all_specs(user_id):
        if s.id == server_id:
            return s
    return None


def view(user_id: str, reveal: bool = False) -> dict:
    """给界面看的那一份（headers 掩码；reveal=True 才回明文）。

    照 modelcfg.mask_config 的规矩：**reveal 默认必须是 False**，日志/调试走默认；
    只有"当前用户自己的那份、且在 require_user 后面"才允许 reveal=True。
    """
    out = []
    for s in all_specs(user_id):
        d = {"id": s.id, "label": s.label, "transport": s.transport,
             "url": s.url, "builtin": s.builtin, "enabled": s.enabled,
             "tools": s.tools, "tools_at": s.tools_at, "error": s.error,
             "has_headers": bool(parse_headers(s.headers))}
        d["headers"] = s.headers if reveal else mask_headers(s.headers)
        out.append(d)
    return {"servers": out,
            "max": MAX_SERVERS,
            "transports": list(USER_TRANSPORTS),
            "python": os.path.basename(sys.executable or "python")}
