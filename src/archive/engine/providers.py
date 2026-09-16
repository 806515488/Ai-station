"""模型工厂（archive 自己的）：一律走 LangChain(langchain_openai.ChatOpenAI) 调 OpenAI 兼容端点。

通道：text 默认 glm（deepseek 曾不稳留备用）、vision 默认 qwen-vl-max。
key 读取：src/.env（含三家 key），经 archive.config 注入 os.environ。

新手视角（Java 朋友版）：
  - 这个文件 = archive 的“RestTemplate/HTTP 客户端工厂”：给你一个已经配好 base_url、
    key、模型名的“可调模型的对话客户端”。
  - 与 station 的 model.py 分工：两边都是薄封装、都读同一份 src/.env 的 key；
    station 服务宿主 agent，archive 服务识别链，各自独立、互不 import 对方（解耦）。
"""
from __future__ import annotations

import os
import sys
import threading
import time

_PKG = os.path.dirname(os.path.abspath(__file__))           # .../engine
_PLATFORM = os.path.abspath(os.path.join(_PKG, ".."))       # .../archive
_SRC = os.path.abspath(os.path.join(_PLATFORM, ".."))       # .../src
_REPO = os.path.abspath(os.path.join(_SRC, ".."))           # 仓库根
# 把自己可能要用到的几个目录都塞进 sys.path（≈Java classpath），保证能 import 到 archive/config 等
for _p in (_PLATFORM, _SRC, _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def load_env():
    """读 .env（key 都在 src/.env）→ 注入环境变量。失败也别崩（没有 key 后面会再报）。"""
    try:
        import archive.config as cfg
        cfg.load_env()                        # 默认读 app_dir(.env) → src/.env
    except Exception:                          # noqa
        pass


def _key(name: str) -> str:
    """取某个环境变量并去空格；空返回 ''。"""
    return (os.environ.get(name) or "").strip()


# 三家服务商接入表（OpenAI SDK 用“根地址”，SDK 会自动补 /chat/completions）
PROVIDERS = {
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "text": "glm-5.3-flash", "vision": "glm-5.3-flash",
        "key": "GLM_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "text": "deepseek-v4-flash", "vision": "deepseek-v4-flash-vision-exp",
        "key": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "text": "qwen-vl-max", "vision": "qwen-vl-max",
        "key": "QWEN_API_KEY",
    },
}


# ── 退避重试（09-15）──────────────────────────────────────────────────────
# ★★ 下面这一小段（RETRY_WAITS / _TRANSIENT_NAMES / _is_transient / import_chat_openai）
#    与 `src/station/core/model.py` 里那份**是同值的一份拷贝**，这是**刻意**的：
#    archive 不许 import station（技能要能搬走，见 docs/conventions.md「代码组织」里
#    PROVIDERS 两表"刻意不合并"的先例），所以合并不了，只能靠一条测试盯着两边别漂移
#    —— tests/test_model_retry.py::test_retry_policy_does_not_drift。
#    **改这里就得改那边，反之亦然。**
RETRY_WAITS = (1.0, 2.0, 4.0, 8.0, 16.0)   # 最多重试 5 次，合计约 31 秒

_TRANSIENT_NAMES = frozenset({              # 传输层"连不上/读不到"的异常类名
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "TransportError", "NetworkError",
    "APIConnectionError", "APITimeoutError", "InternalServerError",
})


def _is_transient(err) -> bool:
    """这个失败值不值得退避重试？True = 等一会儿再试，False = 立刻报错。

    详见 station 侧同名函数的注释（两边判定规则必须一致）。要点：5xx/408 重试；
    4xx 一律不重试（鉴权错、模型名错、**429 配额**都是"再试也没用"）；没有状态码
    就看异常类名；最后兜住"首次并发 import"那个 `partially initialized module`。
    """
    code = getattr(err, "status_code", None)
    if not isinstance(code, int):
        code = getattr(getattr(err, "response", None), "status_code", None)
    if isinstance(code, int):
        return code >= 500 or code == 408
    # ★ 走**整条继承链**（理由见 station 侧同名函数的注释：真抛的是子类
    #   OpenAIConnectionError，光比叶子名会漏掉最常见的"连不上"）。
    if any(k.__name__ in _TRANSIENT_NAMES for k in type(err).__mro__):
        return True
    if isinstance(err, (ConnectionError, TimeoutError)):
        return True
    return "partially initialized module" in str(err)


# 首次导入串行化：识别链的 node_mark 是 **6 路并发**，若它们同时执行下面这句
# import，后来者会读到还没初始化完的 httpx（报 partially initialized module）——
# 症状是"只错一次、之后永远正常"，极难查。这把锁把首次导入收成单线程。
_IMPORT_LOCK = threading.Lock()
_chat_openai = None


def _load_chat_openai():
    """真正执行 import 的那一句。单独成函数是为了**好测**（见导入串行化的用例）。"""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI


def import_chat_openai():
    """懒加载 ChatOpenAI，且保证**本进程内只导一次、其余线程等待**（见 station 侧同名函数）。"""
    global _chat_openai
    if _chat_openai is not None:
        return _chat_openai
    with _IMPORT_LOCK:
        if _chat_openai is None:
            _chat_openai = _load_chat_openai()
    return _chat_openai


def _pick(kind: str) -> str:
    """选通道：环境变量可指定（TEXT_CHANNEL/VISION_CHANNEL），否则用默认 text=glm、vision=qwen。

    .get(kind) 里的 kind 是 "TEXT"/"VISION"，所以我们拼 "{kind.upper()}_CHANNEL" 找变量。
    """
    name = _key(f"{kind.upper()}_CHANNEL") or {"text": "glm", "vision": "qwen"}[kind]
    # 名字不在 PROVIDERS 里就回退默认（防 .env 写错）
    return name if name in PROVIDERS else {"text": "glm", "vision": "qwen"}[kind]


class _Chain:
    """按降级顺序逐家尝试的薄客户端（识别链专用）。

    为什么不用 station 那边的 Model：`graph.node_mark` 调的是
    `llm.invoke([{type:text}, {type:image_url}])` —— **多模态内容块**；而 station 的
    `Model.respond` 会把 content 一律 `str()`，图片块会变成一串 Python repr 字符串，
    建档必炸。所以这里刻意保持"原样把消息交给 langchain"，只多一层降级重试。

    只暴露两样：`.invoke(msgs)`（给 graph 用）和 `.channel`（OCR 缓存键用的标签）。
    """

    def __init__(self, kind: str, entries: list,
                 temperature: float = 0.0, timeout: int = 120,
                 retries: int | None = None, on_retry=None):
        self._kind = kind
        self._entries = [dict(e) for e in entries]
        self._temperature = temperature
        self._timeout = timeout
        # 整条链全挂了之后，最多再退避重跑几轮（见 RETRY_WAITS）；None = 用满 5 轮。
        self._retries = len(RETRY_WAITS) if retries is None else max(0, int(retries))
        # on_retry：可选的显示回调（收到 {"attempt","total","wait","error"}）。
        # ★ 这一层**不认识 station** —— 回调由调用方注入，上站桥 station_adapter
        #   把它接到 job 的进度条上。engine 只负责"叫一声"，不关心叫给谁听。
        self._on_retry = on_retry
        if not self._entries:
            raise RuntimeError("没有可用的模型通道（模型配置里这个槽位的降级链是空的）")

    @property
    def channel(self) -> str:
        """这条链的标签（链首 provider 的 id）—— OCR 缓存的键之一。

        语义是"哪个**配置好的链**读了这一页"：同一条链读出来的结果可以复用；
        配置一改（换 provider）标签就变，不会拿上一家的结果冒充这一家的。
        """
        return str(self._entries[0].get("id") or "")

    def _client(self, entry: dict):
        """按条目建客户端；没 key 就抛（调用方换下一家）。"""
        if entry.get("_llm") is not None:      # 测试注入用
            return entry["_llm"]
        key = (entry.get("api_key") or "").strip()
        if not key:
            env_name = entry.get("key_env") or ""
            raise RuntimeError(
                f"{entry.get('id')} 未配置 key"
                + (f"（{env_name}，见 src/.env；或在界面「模型配置」里直接填）"
                   if env_name else "（在界面「模型配置」里填）"))
        # 走 import_chat_openai 而不是裸 import：首次导入要串行化（见该函数注释）。
        ChatOpenAI = import_chat_openai()
        return ChatOpenAI(model=entry["model"], api_key=key,
                          base_url=entry.get("base_url") or None,
                          temperature=self._temperature,
                          timeout=self._timeout, max_retries=1)

    def _retry_wait(self, rnd: int) -> float | None:
        """第 rnd 轮（从 0 数）全挂之后该退避几秒；None = 不该重试了。"""
        if rnd >= self._retries or rnd >= len(RETRY_WAITS):
            return None
        return RETRY_WAITS[rnd]

    def _notify_retry(self, attempt: int, wait: float, err) -> None:
        """叫一声"正在重连"。★ 整段包 try/except：**回调坏掉绝不许影响识别**。"""
        if self._on_retry is None:
            return
        try:
            self._on_retry({"attempt": attempt, "total": self._retries,
                            "wait": wait,
                            "error": f"{type(err).__name__}: {err}"})
        except Exception:                      # noqa：显示层的锅，不该影响识别
            pass

    def invoke(self, msgs):
        """按顺序试着调：构造失败（没 key）或调用失败都自动换下一家。

        两层容错：**内层换家**（快，几百毫秒），内层全挂且最后的错是"过会儿就好"
        那种 → **外层退避后整条链再来一轮**（最多 self._retries 轮）。

        识别场景下"单页失败不能拖垮整卷"：重试也没成，异常一路上抛，由 node_mark
        里的 try 兜住、给这页一张"空卡"进待核对（既有行为，不变）。
        """
        last: Exception | None = None
        for rnd in range(self._retries + 1):
            for entry in self._entries:
                try:
                    return self._client(entry).invoke(msgs)
                except Exception as e:         # noqa：这一家不行 → 换下一家
                    last = e
            wait = self._retry_wait(rnd)
            if wait is None or not _is_transient(last):
                break                           # 注定失败或轮次用完 → 别再干等
            self._notify_retry(rnd + 1, wait, last)
            time.sleep(wait)
        tried = " → ".join(str(e.get("id")) for e in self._entries)
        raise RuntimeError(
            f"所有通道都失败了（试过 {tried}）：{type(last).__name__}: {last}")


def make_model(kind: str = "text", channel: str | None = None,
               temperature: float = 0.0, timeout: int = 120,
               entries: list | None = None,
               retries: int | None = None, on_retry=None):
    """造一个可调模型的客户端（≈ new 一个已配好连接的对象）。

    kind=text|vision → 分别拿文本/视觉模型（视觉能看图，建档用；文本省钱快，定类用）。

    entries 是宿主按**用户「模型配置」**解析好的有序条目（见 station/modelcfg.py）：
    给了就返回一个降级链客户端（主通道挂了自动换下一家）。
    不给则按 .env 的老路子选通道 —— **archive 的 CLI（engine/run.py）走的就是这条**，
    它没有用户上下文，必须继续能跑。

    retries / on_retry 只对 entries 那条路（_Chain）有效：老路径返回的是一个裸
    ChatOpenAI，没有链也没有重试 —— CLI 是人在终端里跑，失败能自己重来。
    """
    if entries:
        return _Chain(kind, entries, temperature=temperature, timeout=timeout,
                      retries=retries, on_retry=on_retry)
    load_env()                               # 确保 key 已读进环境变量
    ch = channel or _pick(kind)              # 显式指定通道？否则按 kind 自动选
    p = PROVIDERS[ch]
    key = _key(p["key"])                     # 读这家服务商对应的 key
    if not key:
        raise RuntimeError(f"{ch} 未配置 key（{p['key']}）")
    ChatOpenAI = import_chat_openai()        # 用到才 import（且串行化，见该函数）
    # kind=vision → 用该通道的 vision 模型名；否则用 text 模型名
    return ChatOpenAI(model=p["vision"] if kind == "vision" else p["text"],
                      api_key=key, base_url=p["base_url"],
                      temperature=temperature, timeout=timeout, max_retries=1)
