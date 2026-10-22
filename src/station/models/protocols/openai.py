"""普通聊天协议：SDK 懒加载、客户端构造、工具/消息转换及流分片聚合。

正式调用和连接测试共用 build_client；本模块不决定主备顺序或退避重试。
"""
from __future__ import annotations
import json
import re
import threading
import uuid

_IMPORT_LOCK = threading.Lock()
_chat_openai = None
_WIRE_BAD_CHARS = re.compile(r"[^A-Za-z0-9_-]")

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


def wire_name(name: str) -> str:
    """内部工具名 → **发给模型的名字**（不合规的字符一律换成下划线）。

    `station.remember`          → `station_remember`
    `mcp.websearch.web_search`  → `mcp_websearch_web_search`
    """
    return _WIRE_BAD_CHARS.sub("_", name or "")


def _wire_tools(tools) -> tuple[list[dict], dict[str, str]]:
    """把工具表转成「发给模型的 schema」+「上线名 → 原名」的反查表。

    ★ 反查表是必需的，不是可选优化：模型回来的 `tool_call.name` 是**上线名**，
      宿主必须换回原名才能 `by_name` 查表 —— 只做正向替换而不反查，
      模型的每一次工具调用都会落空（"说要调工具，然后什么都没有"）。
    """
    back: dict[str, str] = {}
    schemas: list[dict] = []
    for t in tools or []:
        w = wire_name(t.name)
        if w in back and back[w] != t.name:
            # 换名是**有损**的（`.` 和 `_` 都变成 `_`），两个工具撞成同一个上线名时
            # 模型无法区分它们。本仓命名（`ns.工具` / `mcp.服务.工具`）下不会发生；
            # 真发生说明命名该改，明说比悄悄少干活强（见 pitfalls「静默失败」那条）。
            raise RuntimeError(
                f"工具名归一化后撞名：{back[w]!r} 与 {t.name!r} 都会变成 {w!r}，"
                f"模型没法区分这两个工具")
        back[w] = t.name
        s = t.schema()
        fn = dict(s.get("function") or {})
        fn["name"] = w                       # 只动名字，参数表原样保留
        s = dict(s)
        s["function"] = fn
        schemas.append(s)
    return schemas, back


def reasoning_kwargs(ChatOpenAI, effort: str) -> dict:
    """构造"带上推理强度"的 ChatOpenAI 关键字参数（空串 = 什么都不带）。

    ★ 为什么要看版本：`langchain-openai` ≥1.6 把 `reasoning_effort` 收成了**正式字段**
      （注解是 `str | None`，**不是**白名单 Literal，所以本仓允许的 "max" 也收），
      显式传它就没有那条 `UserWarning: Parameters {'reasoning_effort'} should be
      specified explicitly`。而依赖只钉了 `>=1`，更老的版本没这个字段，硬传会撞
      pydantic 的未知字段校验 —— 所以退回 `model_kwargs`：那条路照样把它原样塞进
      请求体，行为一致，只是多一行警告。
    ★ 定义只有这一处：`Model._build`（真对话）和「测试连接」探针都调它。两处各写
      一份的话，"探针说能用、真对话却发了另一套参数"这种偏差迟早发生。
    """
    effort = (effort or "").strip()
    if not effort:
        return {}
    if "reasoning_effort" in getattr(ChatOpenAI, "model_fields", {}):
        return {"reasoning_effort": effort}
    return {"model_kwargs": {"reasoning_effort": effort}}


def _to_llm(msgs: list[dict]) -> list[dict]:
    """把宿主内部消息（dict）翻译成 OpenAI API 认识的格式。

    差异点：宿主内部把 assistant 的 tool_calls.arguments 存成 Python dict，
    但 OpenAI 协议要求它是一段 JSON 字符串，所以这里要 json.dumps 序列化一次；
    并且 tool 消息必须带 tool_call_id（模型拿它和 assistant 的调用配对）。

    ★ 工具名在这里**转成上线名**（见上面 wire_name）：历史里存的是原名（带点），
      可模型当初看到的是上线名，回放时必须换过去 —— 否则同一段对话里
      前半段是 `station_remember`、后半段是 `station.remember`，模型会当成两个工具。
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
                              "function": {"name": wire_name(c.get("name", "")),
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
                item["name"] = wire_name(m.get("name", ""))   # 同 assistant：必须是上线名
            out.append(item)
    return out


def _from_resp(resp, back: dict | None = None) -> dict:
    """把 langchain 返回的响应对象，收敛成宿主自己的 {content, tool_calls}。

    langchain 已经把 tool_calls 解析成对象列表（带 name/id/args），
    这里转成纯 dict，方便存盘(json)。

    back = 「上线名 → 原名」的反查表（见 _wire_tools）。模型回的是上线名
    （`station_remember`），宿主内部要的是原名（`station.remember`）。
    传 None 时原样返回 —— 单测直接调本函数时不需要它，认得名字的调用方才传。
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
        name = _tc(tc, "name")
        if back:
            # ★ 换回内部原名。查不到就**原样留着**，别猜：上层会按"工具不存在"报出来，
            #   比在这里悄悄丢一次调用好（那种失败没有任何提示）。
            name = back.get(name, name)
        tcs.append({"id": cid,
                    "name": name,
                    "arguments": _tc(tc, "args") or {}})
    result = {"content": content, "tool_calls": tcs}
    usage = getattr(resp, "usage_metadata", None)
    if isinstance(usage, dict):
        result["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")
                           if isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0}
    return result


def _stream_parts(chunks, back: dict | None = None):
    """把 langchain 的 chunk 流收敛成宿主的流式小接口（生成器）。

    back = 「上线名 → 原名」的反查表，转交给 _from_resp（见其注释）。

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
    yield {"final": _from_resp(acc, back)}


def build_client(entry: dict, *, timeout: float = 180, max_retries: int = 1, max_tokens: int | None = None):
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
    # ── 「推理强度」（09-20）──────────────────────────────────────────
    # 有些模型是**推理模型**：收到问题先想一大段才开口。实测 glm-5.3-flash 答
    # 「列 50 首歌」要**50.5 秒**才写第一个正文字（思考走 reasoning_content
    # 字段，langchain 只认 content，于是宿主看到的是"首字 50 秒、界面全白"）；
    # 带上 `reasoning_effort="low"` 后同一题 **0.57 秒**。
    # ★ 空串 = **一个字都不发**，绝不"发个空的"。
    # ★★ 也绝不能改成无条件发：**不是每家都认** —— 实测 qwen 直接 400
    #    （它要的是 thinking_budget），而 400 会让降级链**静默换家**，
    #    跟"工具名带点号"是同一个坑（见 docs/pitfalls.md）。认不认，由配置里
    #    那一栏说了算，用户可以点「测试连接」当场验。
    # ★ 传参形状交给 reasoning_kwargs（版本差异都在它的注释里）；空串不传。
    return ChatOpenAI(model=entry["model"], api_key=key,
                      base_url=entry.get("base_url") or None,
                      temperature=0,                 # 0=尽量确定，别乱编
                      timeout=timeout,
                      max_retries=max_retries,
                      **({"max_tokens": max_tokens} if max_tokens is not None else {}),
                      **reasoning_kwargs(ChatOpenAI,
                                         entry.get("reasoning_effort") or ""))
