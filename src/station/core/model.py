"""模型适配层 —— 把三家 OpenAI 兼容端点收敛成宿主自己的小接口。

“自研薄 harness”的取舍落点：agent 循环/消息历史是宿主自己的；这里只负责
“给一组消息+工具 schema → 返回 {content, tool_calls}”。换模型/接 ollama/
MCP 只改这一个文件。langchain 被隔离在本文件内（唯一 import 点）。

新手视角：
  - 底层模型（GLM/qwen/deepseek）其实都长一样：输入“一串消息”，输出“文字或
    ‘我要调用某个工具+参数’”。三家端点都兼容 OpenAI 格式，所以一个适配层通吃。
  - Model.respond(msgs, tools) 就干一件事：把 msgs 发给模型 → 拿回
    {content: 它说的话, tool_calls: 它想调的工具清单}。agent.py 只认这个壳。
  - Model.stream(msgs, tools) 是同一件事的流式版：文字一小片一小片吐出来，
    前端就能逐字显示。agent.py 走它；路由判词/压缩摘要仍走 respond（要一次拿全）。
  - FakeModel = 一个“假模型替身”，不联网。它用 if 规则假装会调工具，
    让你在没 key / 不烧钱时也能完整看到 harness 循环怎么转（STATION_FAKE=1）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from station import config

# 三家服务商的接入信息：base_url=接口根地址，text/vision=各自用的模型名，key=读哪个环境变量。
# ★ 09-10 起这张表**只是"没给降级链时的老路径兜底"**（`Model(entries=None)`）：正式路径
#   走 station/modelcfg.py 的用户配置（界面「模型配置」里可改、可加自定义 provider，
#   连 ollama 就是加一条 base_url=http://localhost:11434/v1 的条目）。两边的模型名要
#   一起改，别只改一处——历史上就是因为有两份表才开始漂移。
_PROVIDERS = {
    "glm":      {"base_url": "https://open.bigmodel.cn/api/paas/v4",
                 "text": "glm-5.3-flash", "vision": "glm-5.3-flash",
                 "key": "GLM_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1",
                 "text": "deepseek-flash",
                 "vision": "deepseek-flash",
                 "key": "DEEPSEEK_API_KEY"},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "text": "qwen-vl-max", "vision": "qwen-vl-max",
                 "key": "QWEN_API_KEY"},
    "qwen-flash": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                   "text": "qwen-flash", "vision": "qwen-vl-max",
                   "key": "QWEN_API_KEY"},
}


# ── 退避重试（09-15）──────────────────────────────────────────────────────
# 为什么要有它：模型调用失败分两种。一种是"过一会儿自己就好了"（网络抖动、对端
# 5xx、以及下面那条导入竞态），另一种是"再试一百次也一样"（key 填错、模型名写错、
# 配额用光）。前者应该自动重连，后者应该立刻把真话告诉用户 —— 给一个注定失败的
# 错误退避半分钟，用户只会以为界面卡死了。
#
# ★ RETRY_WAITS 必须是**模块顶层常量**：单测要把它 monkeypatch 成 (0.0, …) 才不用
#   真睡 31 秒（同一个理由见 skills/video/code/agnes.py 的 CREATE_RETRY_WAITS）。
# ★ 本文件与 src/archive/engine/providers.py **各有一份同值的常量**，这是刻意的：
#   archive 不许 import station（技能要能搬走），所以合并不了，只能靠一条测试盯着
#   两边别漂移（tests/test_model_retry.py::test_retry_policy_does_not_drift）。
RETRY_WAITS = (1.0, 2.0, 4.0, 8.0, 16.0)   # 最多重试 5 次，合计约 31 秒

# 这些异常类名 = 传输层的"连不上/读不到"，等一会儿多半能好。
# ★ 刻意按**类名字符串**比，而不是 import httpx / openai 来判类型 —— 那两个包正是
#   被懒加载的东西（见下面的 import_chat_openai），为了判错去 import 它们等于本末倒置。
# ★ 比的时候走**整条继承链**（见 _is_transient）：实际抛出来的常常是子类，
#   比如 `OpenAIConnectionError`，而它爹才是这里的 `APIConnectionError`。
_TRANSIENT_NAMES = frozenset({
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "TransportError", "NetworkError",
    "APIConnectionError", "APITimeoutError", "InternalServerError",
})


def _is_transient(err) -> bool:
    """这个失败值不值得退避重试？True = 等一会儿再试，False = 立刻报错。

    判定顺序（先看 HTTP 状态码，最准）：
      · 状态码 ≥500 或 408 → 对端的锅，重试
      · 状态码 4xx（含 401/403 鉴权、404 模型名、**429 配额**）→ 重试没用，立刻报
        ★ 429 不重试是本仓既定口径，先例见 skills/video 的 CREATE_RETRY_WAITS：
          "按次限流，越撞越死"。
      · 没有状态码就看异常类名（**走整条继承链**，见下）/ 是不是内置的连接与超时异常
      · 最后兜一条**导入竞态**：`partially initialized module 'httpx' …` —— 那是
        服务器刚起后第一次调模型时，多个线程同时执行同一个 import 撞出来的，
        几百毫秒后必然已经好了（详情见 import_chat_openai）。
    """
    code = getattr(err, "status_code", None)
    if not isinstance(code, int):
        # openai/langchain 的异常有的把状态码挂在 .response 上，两个位置都认
        code = getattr(getattr(err, "response", None), "status_code", None)
    if isinstance(code, int):
        return code >= 500 or code == 408
    # ★ 比的是**整条继承链**，不是光是叶子的类名：实测真抛出来的是
    #   `OpenAIConnectionError`（它的爹才是 `APIConnectionError`）—— 只比叶子名会漏掉
    #   最常见的那个"连不上"，退避就永远不生效。这个漏是 09-15 端到端真跑才抓到的，
    #   单测抓不到（假的异常类名是我自己起的，正好起对了）。
    if any(k.__name__ in _TRANSIENT_NAMES for k in type(err).__mro__):
        return True
    if isinstance(err, (ConnectionError, TimeoutError)):
        return True
    return "partially initialized module" in str(err)


# ── 首次导入串行化 ────────────────────────────────────────────────────────
# langchain_openai（连带 openai → pydantic → httpx）故意是**用到才 import** 的，
# 这笔账（约 1.35 秒）落在"第一次真要用模型"那一次上。麻烦在于这一次可能同时有
# 好几个线程在跑（对话的线程池线程、L2 判词的 daemon 线程、识别 job 的 6 路并发
# worker），于是两个线程可能同时执行这条 import —— 后进来的那个会读到还没初始化
# 完的 httpx，报 `partially initialized module 'httpx' has no attribute 'URL'`。
# 它的症状很迷惑：**只错一次，之后永远正常**（只要有一个线程导完，sys.modules 里
# 就是完整的了）。下面这把锁把"第一次导入"收成单线程，其余线程等在锁上。
#
# ★ 注意边界：archive 那边（engine/providers.py）有它自己的锁。跨包的那一瞬间
#   （宿主 chat 与识别 visual 同时首次导入）仍可能撞 —— 两个包互不 import 就没法
#   共用一把锁，那一段由上面的退避重试兜住（撞上了退避一次就好）。
_IMPORT_LOCK = threading.Lock()
_chat_openai = None                    # 缓存导入好的类，导一次就够


def _load_chat_openai():
    """真正执行 import 的那一句。单独成函数是为了**好测**（见导入串行化的用例）。"""
    from langchain_openai import ChatOpenAI          # noqa：就是这句要串行化
    return ChatOpenAI


def import_chat_openai():
    """懒加载 ChatOpenAI，且保证**全进程只导一次、其余线程等待**。

    第一次调用会花约 1.35 秒；之后的调用就是读一个模块全局，几乎不要钱。
    """
    global _chat_openai
    if _chat_openai is not None:        # 快路径：导过了直接给（不加锁，省开销）
        return _chat_openai
    with _IMPORT_LOCK:
        if _chat_openai is None:        # 双检：可能刚才有别的线程已经导完了
            _chat_openai = _load_chat_openai()
    return _chat_openai


def _to_llm(msgs: list[dict]) -> list[dict]:
    """把宿主内部消息（dict）翻译成 OpenAI API 认识的格式。

    差异点：宿主内部把 assistant 的 tool_calls.arguments 存成 Python dict，
    但 OpenAI 协议要求它是一段 JSON 字符串，所以这里要 json.dumps 序列化一次；
    并且 tool 消息必须带 tool_call_id（模型拿它和 assistant 的调用配对）。
    """
    out = []
    pending_ids: list[str] = []     # 最近 assistant tool_calls 的 id 队列，给 tool 结果配对用
    for m in msgs:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            # 把 assistant 想调的工具列表翻译成协议格式
            calls = []
            for c in m["tool_calls"]:
                # 有些兼容端点不回 id；host 必须补一个，否则 OpenAI 校验 tool 消息时
                # 会因为 tool_call_id 缺失/为空而抛 KeyError。
                cid = str(c.get("id") or uuid.uuid4().hex[:16])
                if not c.get("id"):
                    c["id"] = cid                # 顺手把缺 id 的历史补回（同一份 Thread 可复用）
                pending_ids.append(cid)
                calls.append({"id": cid, "type": "function",
                              "function": {"name": c.get("name", ""),
                                           "arguments": json.dumps(
                                               c.get("arguments") or {},
                                               ensure_ascii=False)}})
            out.append({"role": "assistant", "content": m.get("content", ""),
                        "tool_calls": calls})
        else:
            # ★ 多模态消息：content 是**结构块列表**时才原样透传，例如
            #     [{"type":"text","text":"这是什么"},{"type":"image_url","image_url":{...}}]
            #   一拍平（str()）图片块就变成一串 Python repr，模型收到的是一堆文本、
            #   图彻底丢失 —— 而报出来的是模型的 400，很难查（09-14 加人像提取时才补上）。
            # ★ 只在 `isinstance(c, list)` 时透传：纯文本调用方传的仍是 str，走下面那支，
            #   行为一字不变。全仓 respond/stream 的调用方（compact/router/agent/降级回退）
            #   传的都是 str，所以这条分支对它们是**不可达**的，零影响。
            # ★ 别顺手把 None→"None" 改成空串 —— 那是另一件事的行为变更，别混进来。
            # ★★ 告诫：图片块现在能穿过这里了，但**绝不要把 base64 图片写进 Thread 历史**
            #    —— 历史落 SQLite 且每轮重发，几 MB 的 base64 会让会话表和 token 一起炸。
            #    图片只该走"一次性调用"，不进对话历史。
            c = m.get("content", "")
            item = {"role": role,
                    "content": c if isinstance(c, list) else str(c)}
            # 工具执行结果消息：必须带 tool_call_id + 名字，模型才知道结果属于哪次调用
            if role == "tool":
                tid = m.get("tool_call_id") or m.get("id")
                if not tid and pending_ids:       # 历史消息丢了 id → 按顺序补回配对
                    tid = pending_ids.pop(0)
                item["tool_call_id"] = tid or ("repair-" + uuid.uuid4().hex[:12])
                if not m.get("tool_call_id") and not m.get("id"):
                    m["tool_call_id"] = item["tool_call_id"]
                item["name"] = m.get("name", "")
            out.append(item)
    return out


def _from_resp(resp) -> dict:
    """把 langchain 返回的响应对象，收敛成宿主自己的 {content, tool_calls}。

    langchain 已经把 tool_calls 解析成对象列表（带 name/id/args），
    这里转成纯 dict，方便存盘(json)。
    """
    content = resp.content
    if not isinstance(content, str):
        # ★ 多模态响应的 content 也可能是**结构块列表**（如 [{"type":"text","text":"…"}]）。
        #   原来这里直接置空 —— 于是"让识图模型看一眼图、回一段文字"的调用会**静默拿到
        #   空字符串**：上层看到的是"模型没给出结果"，而根因在读响应，极难查
        #   （09-14 加人像提取时暴露：提取结果变成空串，用户看到的是"优化失败"）。
        #   只抽 type=="text" 的块拼起来；别的块（图片等）忽略。
        if isinstance(content, list):
            content = "".join(
                str(b.get("text") or "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
        else:
            content = ""

    def _tc(tc, key: str, default=""):
        """兼容两种形态：新版 LangChain 里 tool_call 是 dict（TypedDict）。"""
        if isinstance(tc, dict):
            # 有些 provider 只给原始 function 形态，没有先拍平成 ToolCall
            if key == "name":
                return tc.get("name") or (tc.get("function") or {}).get("name") or default
            if key == "id":
                return tc.get("id") or (tc.get("function") or {}).get("id") or default
            if key == "args":
                return tc.get("args") or tc.get("arguments") or {}
            return tc.get(key, default)
        return getattr(tc, key, default)

    tcs = []
    for tc in getattr(resp, "tool_calls", None) or []:
        # 兼容端点可能不给 id；Host 必须在落盘前补，否则下一轮协议报 KeyError
        cid = _tc(tc, "id") or uuid.uuid4().hex[:16]
        tcs.append({"id": cid,
                    "name": _tc(tc, "name"),
                    "arguments": _tc(tc, "args") or {}})
    return {"content": content, "tool_calls": tcs}


def _stream_parts(chunks):
    """把 langchain 的 chunk 流收敛成宿主的流式小接口（生成器）。

    逐片 yield {"delta": 这一片新增的文字}，最后 yield {"final": 与 respond 同形的 dict}。
    调用方（agent.py）拿到 delta 就实时推给前端，拿到 final 才决定“要不要调工具”。

    新手视角：流式 = 模型一边想一边往外吐字。langchain 每次只给一小片（chunk），
    这里做两件事：
      1) 把这一片的【新增】文字发出去 —— 注意不要发累积值，否则前端文字会一段段重复；
      2) 把片累积起来（chunk1 + chunk2 + …）——langchain 的加法会把工具调用的分片
         （函数名、参数被切成好几段）按 index 拼回完整的一次调用，最后用 _from_resp
         转成和 respond 一样的 {content, tool_calls}。
    """
    acc = None
    for ch in chunks:
        content = getattr(ch, "content", "")
        # 只发“这一片新增的”文字；有些片是空的（心跳片/纯工具调用片），跳过
        if isinstance(content, str) and content:
            yield {"delta": content}
        # langchain 官方聚合方式：AIMessageChunk 支持 + 运算（内含 tool_call 分片合并）
        acc = ch if acc is None else acc + ch
    if acc is None:
        raise RuntimeError("模型流式返回为空（一个 chunk 都没有）")

    # —— 两个防御：分片收到了、却没聚出 tool_calls ——
    # langchain 在流式路径下硬取分片的 index，端点若不回 index 会被它【静默】丢弃
    # （langchain_openai 里是 except KeyError: pass），结果退化成“模型没调工具”，
    # 用户只会看到一个空气泡。宁可在这里报错，也别悄悄少干活。
    if getattr(acc, "tool_call_chunks", None) and not getattr(acc, "tool_calls", None):
        raise RuntimeError("工具调用分片无法聚合（端点未返回 index？）")
    if getattr(acc, "invalid_tool_calls", None) and not getattr(acc, "tool_calls", None):
        raise RuntimeError(f"工具调用参数解析失败：{acc.invalid_tool_calls!r}")
    yield {"final": _from_resp(acc)}


class Model:
    """真实模型：ChatOpenAI 的薄封装。两个入口：

      respond(msgs, tools) → {content, tool_calls}        一次拿全（路由判词/压缩摘要用）
      stream(msgs, tools)  → 逐片 {"delta"} + 末尾 {"final"}（对话用，让回复逐字出现）

    09-10 起支持**降级链**：按 entries 给的顺序逐家尝试，一家不行换下一家。
    四条铁律（改这里之前务必看懂，写错了用户会看到"两家的话拼在一起"）：
      · 构造期失败（没配 key / 未知通道）  → 换下一家（纯本地，廉价）
      · respond() 调用失败                 → 换下一家（invoke 是原子的，没有半成品）
      · stream() **已经往外吐过字**后失败  → **绝不换**，直接抛
      · stream() 还没吐字就失败            → 换下一家
    entries=None 时退化成"单通道"，行为与改造前一模一样（老调用方不用改）。
    """

    def __init__(self, kind: str = "text", channel: str | None = None,
                 timeout: float = 180, max_retries: int = 1,
                 *, entries: list | None = None,
                 total_budget: float | None = None,
                 retries: int | None = None):
        config.load_env()                       # 确保 .env 里的 key 已被读进环境变量
        self.kind = kind
        self._timeout = timeout
        self._max_retries = max_retries
        # 整条链全挂了之后，最多再退避重跑几轮（见 RETRY_WAITS）。
        # None = 用满 RETRY_WAITS 的长度（默认 5 轮）；0 = 不重试。
        # ★ L2 判词必须传 0：它是"加速通道"，有 10 秒总预算，退避会把它彻底堵死。
        self._retries = len(RETRY_WAITS) if retries is None else max(0, int(retries))
        # 整条链的**总预算**（秒）；None = 不限，每次尝试各自拿满 timeout。
        # ★ 判词槽必须给值：ROUTE_TIMEOUT=1.5 的语义是"整条链一共 1.5 秒"，
        #   不是"每条链各 1.5 秒"——按后者实现，3 条链就是 4.5 秒卡顿，
        #   等于把 09-10 刚修好的那个坑原样还回来。
        self._total_budget = total_budget
        if entries is None:
            # 老路径（单通道）：构造时就建连 —— 保持"没配 key 立刻抛"的旧时机，
            # 老调用方（如 L2 判词）依赖它。给了 entries 才懒建（见 _client）。
            self.slot = ""
            self._entries = [self._legacy_entry(kind, channel)]
            self._entries[0]["_llm"] = self._build(self._entries[0])
        else:
            self._entries = [dict(e) for e in entries]
            if not self._entries:
                raise RuntimeError(
                    "没有可用的模型通道（模型配置里这个槽位的降级链是空的）")
            self.slot = self._entries[0].get("slot") or ""

    def _legacy_entry(self, kind: str, channel: str | None) -> dict:
        """老写法（只给通道名）→ 一条 entry。表仍读 _PROVIDERS，与改造前同源。"""
        ch = channel or config.channel_for(kind)
        p = _PROVIDERS.get(ch)
        if p is None:
            raise RuntimeError(f"未知通道 {ch}，可选 {list(_PROVIDERS)}")
        return {"id": ch, "label": ch, "slot": "",
                "base_url": p["base_url"],
                "api_key": (os.environ.get(p["key"]) or "").strip(),
                "key_env": p["key"],
                # 视觉任务用该通道的 vision 模型，否则用 text 模型
                "model": p["vision"] if kind == "vision" else p["text"]}

    def _build(self, entry: dict, timeout: float | None = None):
        """按一条 entry 建一个 ChatOpenAI；没 key / 建不起来就抛（调用方换下一家）。"""
        key = (entry.get("api_key") or "").strip()
        if not key:
            # 文案分两种：站长还能靠 .env 兜底（告诉他变量名，好去核）；普通用户
            # **根本没有 .env 这条路**（modelcfg.resolve 已按 is_owner 把 key 置空），
            # 跟他说 src/.env 只会让他一头雾水 —— 直接指到界面上的齿轮。
            # 缺省 True 是给 entries=None 的老路径（_legacy_entry）用的，行为不变。
            if entry.get("allow_env", True):
                env_name = entry.get("key_env") or ""
                where = (f"（未配置 {env_name}，也未在「模型配置」中填写）"
                         if env_name else "（请在界面「模型配置」中填写）")
            else:
                where = "（请在右上角 ⚙「模型配置」中填写服务商提供的 API Key）"
            raise RuntimeError(f"{entry.get('id')} 未配置 key{where}")
        # 用到才 import（省启动时间）；走 import_chat_openai 是为了串行化首次导入，
        # 别改回裸 import（那个并发竞态见该函数的注释）。
        ChatOpenAI = import_chat_openai()
        return ChatOpenAI(model=entry["model"], api_key=key,
                          base_url=entry.get("base_url") or None,
                          temperature=0,                 # 0=尽量确定，别乱编
                          timeout=self._timeout if timeout is None else timeout,
                          max_retries=self._max_retries)

    def _client(self, entry: dict, timeout: float | None = None):
        """取这条 entry 的客户端（老路径在构造时就建好了，直接复用）。"""
        if entry.get("_llm") is not None:
            return entry["_llm"]
        return self._build(entry, timeout)

    def _attempts(self, t0: float | None = None):
        """按降级顺序产出 (entry, 这次尝试能用的 timeout)。

        带总预算时：每次尝试的 timeout = min(单次 timeout, 剩余预算)；预算耗尽就停止
        尝试并报错 —— 这才是"整条链一共 N 秒"的正确语义。

        ★ t0 由调用方传进来（不是这里取）：退避重试会**多轮**调用本函数，如果每轮
          都重新取一次 t0，预算就被无限续命、永远耗不尽 —— L2 的"整条链一共 10 秒"
          会当场失效。所以时钟只在进入轮次循环之前取一次。
        """
        t0 = time.monotonic() if t0 is None else t0
        for i, entry in enumerate(self._entries):
            budget = None
            if self._total_budget:
                left = self._total_budget - (time.monotonic() - t0)
                if left <= 0:
                    tried = [str(e.get("id")) for e in self._entries[:i]]
                    raise RuntimeError(
                        f"降级链总预算 {self._total_budget}s 已用完（试过 {tried}）")
                budget = min(self._timeout, left)
            yield entry, budget

    def _retry_wait(self, rnd: int) -> float | None:
        """第 rnd 轮（从 0 数）全挂之后该退避几秒；None = 不该重试了。

        两种情况返回 None：轮次用完（self._retries），或 RETRY_WAITS 被截短到不够
        （单测会把它 patch 成短的，得防越界）。
        """
        if rnd >= self._retries or rnd >= len(RETRY_WAITS):
            return None
        return RETRY_WAITS[rnd]

    @staticmethod
    def _notify_retry(cb, attempt: int, total: int, wait: float, err) -> None:
        """告诉外面"正在重连"（对话侧由 stream 直接 yield，别处走这个回调）。

        ★ 整段包 try/except：**通知坏掉绝不许影响调用本身** —— 重连是为了让用户
          拿到结果，不能因为一个显示用的回调把正经事带崩。
        """
        if cb is None:
            return
        try:
            cb({"attempt": attempt, "total": total, "wait": wait,
                "error": f"{type(err).__name__}: {err}"})
        except Exception:                      # noqa：显示层的锅，不该影响调用
            pass

    def _fail_msg(self, err) -> str:
        """所有通道都挂时给一句能照着修的话（说清是哪个槽位、试过谁）。"""
        where = f"槽位「{self.slot}」" if self.slot else "模型"
        tried = " → ".join(str(e.get("id")) for e in self._entries)
        return (f"{where}所有通道都失败了（试过 {tried}）："
                f"{type(err).__name__}: {err}")

    def respond(self, msgs: list[dict], tools=None, on_retry=None) -> dict:
        """把历史发给模型。tools 不为空时“绑定”工具说明 → 模型才知道能调什么。

        两层容错，别搞混：
          · **内层**：按降级链换下一家（invoke 是**原子**的 —— 要么完整拿到、要么
            什么都没有，不存在"半句话"，所以换家是安全的）。
          · **外层**：整条链全挂了，且最后一个错是"过会儿就好"的那种 → 退避几秒
            **整条链再来一轮**（最多 self._retries 轮）。先换家再退避，是因为换家
            只要几百毫秒，而退避起步就 1 秒 —— 能靠换家解决的就别让用户等。
        on_retry 是给调用方的显示回调（收到 {"attempt","total","wait","error"}）。
        """
        last: Exception | None = None
        t0 = time.monotonic()          # ★ 只取一次：多轮之间总预算不被重置
        for rnd in range(self._retries + 1):
            for entry, budget in self._attempts(t0):
                try:
                    # bind_tools([每个工具 schema]) 让模型在对话里能输出 tool_calls
                    llm = self._client(entry, budget)
                    if tools:
                        llm = llm.bind_tools([t.schema() for t in tools])
                    return _from_resp(llm.invoke(_to_llm(msgs)))
                except Exception as e:          # noqa：这一家不行 → 记下换下一家
                    last = e
            wait = self._retry_wait(rnd)
            if wait is None or not _is_transient(last):
                break                           # 注定失败或轮次用完 → 别再让用户等
            self._notify_retry(on_retry, rnd + 1, self._retries, wait, last)
            time.sleep(wait)
        raise RuntimeError(self._fail_msg(last))

    def stream(self, msgs: list[dict], tools=None):
        """流式版 respond：先逐片 yield {"delta": 文本}，最后 yield {"final": {...}}。

        final 与 respond 的返回值同形，调用方据此判断“要不要调工具”。
        逃生阀：环境变量 STATION_STREAM=0 时直接退回非流式 —— 万一某家的流式端点
        不兼容（工具调用被吞/报错），不用改代码就能恢复旧行为。

        除了 {"delta"} / {"final"}，退避等待时还会 yield 一种 **{"retry": {...}}**
        （含第几次、共几次、等几秒），好让前端显示"正在重新连接"。全仓只有
        core/agent.py 消费本函数的产出，加这一种 part 是安全的。

        ★ 降级铁律就在下面的 sent_any：**已经往外吐过字就绝不再换家**。继续换的话，
          用户看到的是"上一家的半句话 + 下一家的整句话"，是胡话，宁可报错。
          这条同样管住外层的退避重试 —— 那句话一抛就直接穿出两层循环，不会再等。
        """
        if os.environ.get("STATION_STREAM", "1") == "0":
            yield {"final": self.respond(msgs, tools)}
            return
        last: Exception | None = None
        t0 = time.monotonic()          # ★ 只取一次：多轮之间总预算不被重置
        for rnd in range(self._retries + 1):
            for entry, budget in self._attempts(t0):
                sent_any = False                  # 这一家已经往外吐过字没有
                try:
                    # 绑了工具才能流式收到 tool_calls（langchain 的 RunnableBinding 会转发流）
                    llm = self._client(entry, budget)
                    if tools:
                        llm = llm.bind_tools([t.schema() for t in tools])
                    # 注意：_stream_parts 是【生成器】，异常是在消费到那一片时才抛出来的，
                    # 所以 try 必须把整个 for 包住，不能只包它的构造。
                    for part in _stream_parts(llm.stream(_to_llm(msgs))):
                        if "delta" in part:
                            sent_any = True
                        yield part
                    return                        # 整段跑完 = 这次成功
                except Exception as e:            # noqa：不能捕 BaseException，
                    # 否则会把客户端断连的 GeneratorExit 也吃掉、生成器停不下来。
                    if sent_any:
                        raise RuntimeError(
                            f"{entry.get('id')} 输出到一半失败，且已经向外发过内容，"
                            f"不再切换通道：{type(e).__name__}: {e}") from e
                    last = e                      # 还没吐字 → 可以安全换下一家
            wait = self._retry_wait(rnd)
            if wait is None or not _is_transient(last):
                break                             # 注定失败或轮次用完 → 别再让用户等
            # 先把"要重连"告诉前端，再睡 —— 否则用户面对的是没有任何解释的长时间静默
            yield {"retry": {"attempt": rnd + 1, "total": self._retries, "wait": wait}}
            time.sleep(wait)
        raise RuntimeError(self._fail_msg(last))


class FakeModel:
    """离线确定性“模型”：只服务 demo 冒烟/单元测试，不联网不烧 key。

    规则（按优先级）：
      - 刚有工具结果：若来源是 collect_highlights → 接着调 render_report（多步串联演示）；
        否则直接给一句最终答复。
      - 用户话里带“fake_risky/批准” → 调危险工具（验证批准闸）
      - 带“周报” → 调 collect_highlights（写周报链第一步）
      - 带“几点/时间” → 调 now
      - 否则调 echo 回显
    stream() 是 respond() 的流式外壳：决策还是上面这套规则，只是把文字切片发出去，
    让离线路径也能验证“逐字显示 / 边说话边调工具”的界面行为。
    一句话：FakeModel 是给你“看清楚 harness 循环本身”的替身，不是真 AI。
    """

    def __init__(self, skill_hint: str = "demo"):
        self._skill = skill_hint
        self._n = 0                       # 自增计数，生成不重复的假 tool_call_id

    def _prev_leaf(self, msgs: list[dict]) -> str:
        """往回找最近一次 assistant 调过的工具名（叶子），用于决定“下一步调谁”。"""
        for m in reversed(msgs):
            calls = m.get("tool_calls") if m.get("role") == "assistant" else None
            if calls:
                return str((calls[0] or {}).get("name", "")).split(".")[-1]
        return ""

    def respond(self, msgs: list[dict], tools=None) -> dict:
        by_leaf = {t.leaf(): t for t in tools or []}   # 叶子名 → 工具对象
        last = msgs[-1] if msgs else {}

        # 情形一：历史最后一条是“工具结果”→ 看要不要接着调下一个工具
        if last.get("role") == "tool":
            prev = self._prev_leaf(msgs)
            if prev == "collect_highlights" and "render_report" in by_leaf:
                # 素材到手了 → 把素材喂给 render_report 成稿（多工具串联演示）
                return self._call(by_leaf["render_report"],
                                  {"content": str(last.get("content", ""))[:600]})
            return {"content": f"(fake) 已得到工具结果：{last.get('content')}",
                    "tool_calls": []}

        # 情形二：用户刚说话 → 按关键词“假装思考”决定调哪个工具
        # （只看【最后一条】用户消息——历史里出现过"照片已上传"不代表这轮还在说它）
        text = next((m.get("content", "") for m in reversed(msgs)
                     if m.get("role") == "user"), "")
        if tools and ("fake_risky" in text or "批准" in text) and "fake_risky" in by_leaf:
            return self._call(by_leaf["fake_risky"], {})
        if tools and "周报" in text and "collect_highlights" in by_leaf:
            return self._call(by_leaf["collect_highlights"], {})
        if tools and ("几点" in text or "时间" in text) and "now" in by_leaf:
            return self._call(by_leaf["now"], {})
        # 口径学习：用户确认后调 apply_learning 落盘（不用传内容 —— 条文由技能自己算）。
        # 离线把这一跳跑通，整条链（改类→提案→确认→写盘）才算真通。
        # ★ 匹配的是**学习卡按钮预填的那句话**（index.html 的 learnGo）—— 它改过一次
        #   （原来带"确认"，会被 classify_answer 误判成对**别的**挂起操作的批准，09-13 修掉），
        #   两边必须一起改，否则离线链路会静默断在这里。
        if tools and "apply_learning" in by_leaf and "写进《文种对照》" in text:
            return self._call(by_leaf["apply_learning"], {})
        # archive 技能的离线规则（端到端冒烟：上传卡→建卷→全景→改类→出件批准）
        # ★ 对账规则必须排在 ask_photos **前面**：上传终版目录的消息里带着
        #   "…人事档案目录.xlsx" 这种路径，含"档案"二字，会被下面那条泛化规则接走。
        if tools and "reconcile" in by_leaf and "对账" in text:
            import re as _re
            m = _re.search(r"终版目录已上传：(.+?)（", text)
            return self._call(by_leaf["reconcile"],
                              {"path": m.group(1)} if m else {})
        if tools and "ask_photos" in by_leaf and "照片已上传" not in text \
                and ("上传" in text
                     or ("照片" in text and "改成" not in text)
                     or ("档案" in text and "改成" not in text)):
            return self._call(by_leaf["ask_photos"], {})
        if tools and "scan_photos" in by_leaf and "照片已上传" in text:
            import re as _re
            # 消息由上传卡片拼，两种形态（见 index.html 的 upGo）：
            #   照片已上传：<目录>（126 张，姓名：张三）
            #   照片已上传：<目录>（126 张，未填姓名，文件夹名：李明）
            # → dir 取"（"之前那段；只有"姓名：X"才当 person，"未填姓名"就**不传**
            #   （让 scan_photos 走"先问一句"的分支 —— 这条规则链在离线冒烟里也要一致）。
            m = _re.search(r"照片已上传：(.+?)（", text)
            p = _re.search(r"张\s*[，,]\s*姓名：\s*(.+?)\s*）", text)
            args = {"dir": m.group(1)} if m else {}
            if p:
                args["person"] = p.group(1)
            return self._call(by_leaf["scan_photos"], args)
        if tools and "recognize" in by_leaf and ("开始识别" in text or "识别" in text):
            return self._call(by_leaf["recognize"], {})
        # 换识图模型：报菜单 / 切到某一家（离线冒烟用；真模型下由 description 驱动）
        if tools and "list_vision_models" in by_leaf and (
                "有哪些模型" in text or "换个模型" in text or "换一家" in text):
            return self._call(by_leaf["list_vision_models"], {})
        if tools and "use_model" in by_leaf:
            import re as _re
            m = _re.search(r"用\s*([A-Za-z0-9_.\-]+)\s*(?:读的|那家|的)", text)
            if m:
                return self._call(by_leaf["use_model"], {"provider": m.group(1)})
        if tools and "show_overview" in by_leaf and ("全景" in text or "核对" in text):
            return self._call(by_leaf["show_overview"], {})
        if tools and "set_category" in by_leaf and "改成" in text:
            import re as _re
            m = _re.search(r"把\s*(.+?)\s*改成\s*([一二三四五六七八九十]+(?:-\d+)?)", text)
            if m:
                return self._call(by_leaf["set_category"],
                                  {"target": m.group(1), "category": m.group(2)})
        if tools and "export" in by_leaf and "出件" in text:
            return self._call(by_leaf["export"], {})
        if tools and "echo" in by_leaf:
            arg = text.strip()[:40] or "你好"
            return self._call(by_leaf["echo"], {"text": arg})
        return {"content": "(fake) 我没有可用的工具来回应", "tool_calls": []}

    _CHUNK = 4          # 离线流式把文字切成几字一片（只为让“逐字出现”肉眼可见）

    def stream(self, msgs, tools=None):
        """离线流式：决策完全交回 respond()，只把它的文字切片发出去。

        这样假模型和真模型走【同一套】流式协议（agent.py 不用分叉），
        而“这一步该调哪个工具”的规则只在 respond 里写一遍，不会两边走样。
        STATION_STREAM_DELAY=0.05 可放慢切片速度，方便肉眼确认 SSE 真在流。
        """
        resp = self.respond(msgs, tools)          # 唯一的决策源（含 _n 计数）
        text = resp.get("content") or ""
        delay = float(os.environ.get("STATION_STREAM_DELAY", "0") or 0)
        for i in range(0, len(text), self._CHUNK):
            if delay:
                time.sleep(delay)                 # 默认 0：不拖慢离线单测
            yield {"delta": text[i:i + self._CHUNK]}
        yield {"final": resp}

    def _call(self, tool, args: dict) -> dict:
        """伪造一条“模型想调工具”的返回（真实模型会基于语义选工具，这里按规则选）。

        带一句固定前言：真模型常常先说一句“我来看一下”再调工具，离线也照此模拟——
        否则“边说话边调工具”的界面行为（气泡怎么切、前言要不要进历史）离线测不到。
        """
        self._n += 1
        return {"content": "（fake）我来看一下。", "tool_calls": [
            {"id": f"fake-{self._n}", "name": tool.name, "arguments": args}]}


def build(kind: str = "text", channel: str | None = None,
          entries: list | None = None,
          total_budget: float | None = None,
          retries: int | None = None) -> Model:
    """对外统一入口：造一个真实模型。

    - 老用法（`build("text")` / `build("text", channel="glm")`）：走 _PROVIDERS，行为不变。
    - 新用法（`build(entries=modelcfg.resolve(uid, "chat"))`）：按降级链逐家尝试。
    - retries=None 用满 RETRY_WAITS（默认最多重试 5 次）；0 = 不重试。
    """
    return Model(kind, channel, entries=entries, total_budget=total_budget,
                 retries=retries)
