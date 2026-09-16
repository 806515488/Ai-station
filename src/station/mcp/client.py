"""MCP 客户端 —— 把官方 `mcp` SDK（异步）包成宿主能直接调的**同步**函数。

新手视角（Java 朋友版）：
  MCP 客户端要干的事其实只有三件：**连上去 → 握手 → 调工具**。
  连上去（transport）有三种法令，按 `ServerSpec.transport` 选：
    · stdio —— 起一个子进程，用它的标准输入/输出收发 JSON（只有**内置**的能用，见 config.py）
    · http  —— Streamable HTTP，就是普通的 HTTP 请求（远端 MCP 的主流形态）
    · sse   —— 老的 Server-Sent Events 形态（有些服务还在用）

★ 本文件最关键的一件事：**异步转同步**。
  官方的 `mcp` SDK 全是 `async def` / `async with`（Python 的 asyncio 异步模型，类比
  Java 的 CompletableFuture / 协程）。而本宿主的对话循环是**同步**的（跑在线程池的
  工作线程里，见 app/server.py 的 gen()）。两者不能直接调：同步函数里 `await` 不了，
  而在工作线程里现开一个事件循环（`asyncio.run`）又会和"连接是异步对象"打架。

  标准解法：**专门开一条后台线程，在那条线程里跑一个常驻的事件循环**（event loop =
  asyncio 的调度中枢）。别的线程想干活，就把协程丢过去排队，然后自己阻塞等结果：

      asyncio.run_coroutine_threadsafe(协程, loop).result(超时秒数)
      └── "把这件活交给那条线程"        └── "我在这儿等它，最多等 N 秒"

  这就是本文件的全部机关。`_run()` 那两行是核心，其余都是包装。

★ 第二件事：**自己卡墙钟超时**，不信 SDK 的分阶段超时。
  httpx 的 timeout 是"连接/读取/写入各自计时"的，单次调用实测能跑过头（本仓在路由
  那条链上踩过：设了 1.5 秒实际跑了 3.3 秒，见 core/router.py 的 _with_deadline）。
  所以这里用 `future.result(timeout)` 自己兜一层**总时长**，超时就取消。

★ 第三件事：**每次调用开一个新会话**（连接 → 握手 → 调用 → 关掉）。
  不养长连接是因为 SDK 的 transport 是 anyio 的"任务组"包出来的，跨取消域存活很脆，
  收尾容易留下幽灵任务。"先要正确、再要快" —— 常驻会话留作以后的优化。
  代价：每次调用多一次握手往返（HTTP 上是几十毫秒；stdio 上要重启一次子进程，几百毫秒）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from contextlib import asynccontextmanager

from station.mcp.config import ServerSpec, parse_headers

# 一次 MCP 调用最多等几秒（自己卡的墙钟，见文件头第三条）。
# 30 秒对"搜个网页/查个库"够用了；真遇到慢服务，用户可以在 .env 里调大。
DEFAULT_TIMEOUT = float(os.environ.get("STATION_MCP_TIMEOUT", "30") or "30")

# 一次 MCP 调用最多往回带多少字符。★ 别删这个上限：MCP 工具的返回**直接进模型上下文**，
# 一个话痨服务返回几十 KB 会当场把这一轮对话顶爆（本仓在 job 事件上吃过同类的亏）。
MAX_TEXT = 12000


class McpError(RuntimeError):
    """MCP 调用失败（连不上 / 握手失败 / 工具报错）。

    刻意**不**复用内置的 TimeoutError 等异常：调用方要能一眼分清"对端说不行"和
    "我们自己等不下去了"（同 core/router.py 的 RouteDeadline 的理由）。
    """


class McpTimeout(McpError):
    """超过我们自己卡的墙钟（不是对端报的超时）。"""


# ── 后台事件循环（异步转同步的机关）──────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()          # 保护上面两个变量的锁（多个线程可能同时第一次用）


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """拿到那条后台事件循环；没有就现起一条（懒加载，全进程只有一条）。

    为什么要锁：第一次调用时可能好几个线程同时进来，不加锁会起出两三条循环线，
    每条都有一部分连接 —— 那种 bug 极难查（同 db.py 里 `with _lock` 的理由）。
    """
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        loop = asyncio.new_event_loop()
        # daemon=True：这是条"后台服务线程"，主程序退出时不用等它（否则会挂住进程）
        t = threading.Thread(target=loop.run_forever, name="mcp-loop", daemon=True)
        t.start()
        _loop, _loop_thread = loop, t
        return loop


def unwrap(err: BaseException) -> BaseException:
    """剥掉 anyio/SDK 包在外面的 `ExceptionGroup`，拿到**真正那个**异常。

    ★ 为什么要这一步（09-16 真跑一次才发现的）：
      SDK 的传输是 anyio 的"任务组"跑起来的，里面一出错就往上抛一个
      `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`
      —— 这句话对用户/对模型**毫无信息量**（真正的原因藏在 `.exceptions` 里，
      比如"连接被拒绝"）。不剥开的话，界面上就会显示这行天书，
      本仓那条"错误文案要让人知道下一步干嘛"的规矩当场作废。
      循环剥是因为包可能不止一层。
    """
    for _ in range(10):                        # 上限防病态的死循环结构
        if isinstance(err, BaseExceptionGroup) and err.exceptions:
            err = err.exceptions[0]
        else:
            break
    return err


def _run(coro, timeout: float):
    """把协程丢给后台循环跑，自己阻塞等结果（超时抛 McpTimeout）。

    `coro` 是个协程对象 —— 注意：**协程只能被 await 一次**，所以这个函数是个"一次性"的
    转发口，别把同一个 coro 传两次。
    """
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError:
        # 取消它：让异步那边把连接/子进程收干净。不等结果 —— 我们已经决定不等了。
        fut.cancel()
        raise McpTimeout(f"MCP 调用超过 {timeout:.0f} 秒没有返回")
    except McpError:
        raise                                  # 已经是我们自己的类型，原样往外抛
    except Exception as e:                     # noqa：把 SDK 的异常统一翻译成我们自己的一种
        real = unwrap(e)                       # 剥掉 TaskGroup 那层壳（见 unwrap 的注释）
        raise McpError(f"{type(real).__name__}: {real}") from e


# ── 三条传输：怎么"连上去" ────────────────────────────────────────────

def _stdio_env() -> dict:
    """stdio 子进程的环境变量。

    ★ 关键是 PYTHONPATH：子进程要能 `import station.mcp.servers.*`。Docker 镜像里是
      `pip install -e .` 装过的，天然能找到；但本机那套 conda 环境**没有** -e 装本项目
      （见 docs/conventions.md「工具链与命令」），不注入这条就起不来。
    """
    from station import config as st_config
    env = dict(os.environ)
    cur = env.get("PYTHONPATH") or ""
    src = str(st_config.SRC)
    env["PYTHONPATH"] = src + (os.pathsep + cur if cur else "")
    env.setdefault("PYTHONUNBUFFERED", "1")    # 别让子进程的输出卡在缓冲区里
    return env


@asynccontextmanager
async def _connect(spec: ServerSpec):
    """按 transport 连上去，交出 (读流, 写流) 这一对。

    三种传输的返回值长得不一样（streamable http 会多给一个"拿 session id 的函数"），
    所以这里统一只取前两个 —— 上层 `ClientSession` 正好只要这两个。
    """
    headers = parse_headers(spec.headers) or None

    if spec.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        if not spec.command:
            raise McpError(f"内置服务 {spec.id} 没有配启动命令")
        params = StdioServerParameters(command=spec.command[0],
                                       args=list(spec.command[1:]),
                                       env=_stdio_env())
        async with stdio_client(params) as rw:
            yield rw[0], rw[1]

    elif spec.transport == "sse":
        from mcp.client.sse import sse_client
        async with sse_client(spec.url, headers=headers,
                              timeout=DEFAULT_TIMEOUT, sse_read_timeout=DEFAULT_TIMEOUT) as rw:
            yield rw[0], rw[1]

    elif spec.transport == "http":
        # ★ 用新 API（streamable_http_client）：老的 streamablehttp_client 在 1.27 已标
        #   deprecated，只是还留着。新 API 不再自己造 HTTP 客户端，而是**要我们给一个**
        #   —— headers / 超时因此都挂在 httpx 客户端上。给了就得自己负责关它，
        #   所以用 `async with` 包着（它自己的上下文管理器只管 MCP 会话，不管这个客户端）。
        import httpx
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(headers=headers,
                                     timeout=httpx.Timeout(DEFAULT_TIMEOUT),
                                     follow_redirects=True) as http_client:
            async with streamable_http_client(spec.url, http_client=http_client) as rw:
                yield rw[0], rw[1]

    else:
        # 走不到这儿：config._validate 卡过白名单了。留着是为了万一以后加传输时漏了分支，
        # 报错要能一眼看出是"没实现"，而不是一个莫名其妙的 KeyError。
        raise McpError(f"不认识的传输方式：{spec.transport}")


async def _with_session(spec: ServerSpec, fn):
    """连上 → 握手 → 把 session 交给 fn 用 → 收工。三个动作绑在一起，别处不用重复。"""
    from mcp.client.session import ClientSession
    async with _connect(spec) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()        # ★ 必须显式握手，SDK 不会替你做
            return await fn(session)


# ── 返回值：MCP 的结果 → 一段给人/给模型看的文本 ──────────────────────

def _content_to_text(res) -> str:
    """把 MCP 的返回压成纯文本。

    MCP 的返回是"内容块列表"（content blocks）：文本块、图片块、资源块……宿主这边
    只把**文本**送进模型上下文，别的块写成一行占位说明 ——
    ★ 图片绝不内联成 base64：那会把对话上下文和前端都压垮（同 conventions 坑区
    「往 job 事件里塞图片字节」的教训）。
    """
    parts: list[str] = []
    for c in (getattr(res, "content", None) or []):
        kind = getattr(c, "type", "")
        if kind == "text":
            parts.append(getattr(c, "text", "") or "")
        elif kind == "image":
            parts.append("（一张图片，这里不显示）")
        elif kind == "resource":
            r = getattr(c, "resource", None)
            parts.append(f"（资源：{getattr(r, 'uri', '') or ''}）")
        else:
            parts.append(f"（{kind or '未知类型'}内容块）")
    text = "\n".join(p for p in parts if p).strip()

    # 有些 server 只回结构化的 structuredContent、没有文本块 → 退化成 JSON 文本
    if not text:
        sc = getattr(res, "structuredContent", None)
        if sc:
            import json
            text = json.dumps(sc, ensure_ascii=False, indent=2)

    if getattr(res, "isError", False):
        # 对端说"这次调用失败了"。别抛异常 —— 让模型看见失败原因，它才好换个法子再试。
        return f"（工具报告失败）{text or '对端没有说明原因'}"
    if not text:
        return "（对端返回了空结果）"
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + f"\n…（内容过长，已截断，原文 {len(text)} 字符）"
    return text


def _tool_to_dict(t) -> dict:
    """MCP 的工具对象 → 纯 dict（要落库缓存，必须是能 JSON 化的普通数据）。"""
    ann = getattr(t, "annotations", None)
    try:
        ann = ann.model_dump(exclude_none=True) if ann is not None else {}
    except Exception:                          # noqa：SDK 改过这里的类型，取不到就算了
        ann = {}
    return {"name": getattr(t, "name", "") or "",
            "title": getattr(t, "title", None) or "",
            "description": getattr(t, "description", None) or "",
            "inputSchema": dict(getattr(t, "inputSchema", None) or {"type": "object"}),
            "annotations": ann}


# ── 对外三个函数 ──────────────────────────────────────────────────────

def list_tools(spec: ServerSpec, timeout: float | None = None) -> list[dict]:
    """连上去问一句"你有哪些工具"，返回工具清单（纯 dict 列表）。

    只有两个地方会调它：用户点「测试连接」、用户点「刷新工具」。
    **对话路径绝不调它** —— 那边读的是缓存（见 bridge.py），否则每轮对话都要连一次网。
    """
    async def _do(session):
        return await session.list_tools()

    res = _run(_with_session(spec, _do), timeout or DEFAULT_TIMEOUT)
    return [_tool_to_dict(t) for t in (getattr(res, "tools", None) or [])]


def call_tool(spec: ServerSpec, name: str, args: dict | None = None,
              timeout: float | None = None) -> str:
    """连上去调一个工具，把结果压成一段文本返回。

    失败一律抛 `McpError`（含超时的 `McpTimeout`）—— 由调用方决定怎么讲给用户听。
    """
    async def _do(session):
        return await session.call_tool(name, args or {})

    res = _run(_with_session(spec, _do), timeout or DEFAULT_TIMEOUT)
    return _content_to_text(res)


def probe(spec: ServerSpec, timeout: float | None = None) -> tuple[list[dict], str]:
    """「测试连接」用的探针：连一次、拿工具清单。**不抛异常**，把结果和错误分开还。

    为什么不抛：调用方（HTTP 端点）要的是一句能给用户看的话，而不是一份栈。抛异常
    到那一层还得再 `except` 一遍，不如在这里就分好。返回 `(工具清单, 错误文案)`，
    错误文案为空 = 成功。
    """
    try:
        return list_tools(spec, timeout), ""
    except McpError as e:
        return [], explain(e)
    except Exception as e:                     # noqa：兜底，别让探针把端点带崩
        return [], explain(e)


def explain(err) -> str:
    """把异常翻译成**人话**（给界面上的「测试连接」用）。

    照本仓的规矩：错误文案要让人知道"下一步该干嘛"，而不是甩一个类名。
    认不出来就把类名带上（至少能搜）。
    """
    if isinstance(err, McpTimeout):
        return f"连接超时（超过 {DEFAULT_TIMEOUT:.0f} 秒没响应）。地址可能不通，或者这个服务很慢。"
    err = unwrap(err)                          # 剥掉 ExceptionGroup 那层壳再认（见 unwrap）
    text = str(err)
    low = text.lower()
    if any(k in low for k in ("connecterror", "connection refused", "connectionerror",
                              "name or service not known", "nodename nor servname",
                              "temporary failure in name resolution", "getaddrinfo")):
        return "连不上：地址不通、域名解析不了，或者服务没在跑。"
    if "401" in text or "403" in text or "unauthorized" in low or "forbidden" in low:
        return "被拒绝了：多半是请求头里的密钥不对或已过期。"
    if "404" in text or "not found" in low:
        return "地址不对：这个路径上没有 MCP 服务（注意有些服务的地址要以 /mcp 结尾）。"
    if "certificate" in low or "ssl" in low:
        return "证书有问题：对端的 HTTPS 证书验不过（自签证书会这样）。"
    if "method not allowed" in low or "405" in text:
        return "对端不接受这种请求：可能这个地址是它的网页，而不是 MCP 端点。"
    return f"{type(err).__name__}：{text}"
