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
                 temperature: float = 0.0, timeout: int = 120):
        self._kind = kind
        self._entries = [dict(e) for e in entries]
        self._temperature = temperature
        self._timeout = timeout
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
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=entry["model"], api_key=key,
                          base_url=entry.get("base_url") or None,
                          temperature=self._temperature,
                          timeout=self._timeout, max_retries=1)

    def invoke(self, msgs):
        """按顺序试着调：构造失败（没 key）或调用失败都自动换下一家。

        识别场景下"单页失败不能拖垮整卷"：换家都没成，异常一路上抛，由 node_mark
        里的 try 兜住、给这页一张"空卡"进待核对（既有行为，不变）。
        """
        last: Exception | None = None
        for entry in self._entries:
            try:
                return self._client(entry).invoke(msgs)
            except Exception as e:             # noqa：这一家不行 → 换下一家
                last = e
        tried = " → ".join(str(e.get("id")) for e in self._entries)
        raise RuntimeError(
            f"所有通道都失败了（试过 {tried}）：{type(last).__name__}: {last}")


def make_model(kind: str = "text", channel: str | None = None,
               temperature: float = 0.0, timeout: int = 120,
               entries: list | None = None):
    """造一个可调模型的客户端（≈ new 一个已配好连接的对象）。

    kind=text|vision → 分别拿文本/视觉模型（视觉能看图，建档用；文本省钱快，定类用）。

    entries 是宿主按**用户「模型配置」**解析好的有序条目（见 station/modelcfg.py）：
    给了就返回一个降级链客户端（主通道挂了自动换下一家）。
    不给则按 .env 的老路子选通道 —— **archive 的 CLI（engine/run.py）走的就是这条**，
    它没有用户上下文，必须继续能跑。
    """
    if entries:
        return _Chain(kind, entries, temperature=temperature, timeout=timeout)
    load_env()                               # 确保 key 已读进环境变量
    ch = channel or _pick(kind)              # 显式指定通道？否则按 kind 自动选
    p = PROVIDERS[ch]
    key = _key(p["key"])                     # 读这家服务商对应的 key
    if not key:
        raise RuntimeError(f"{ch} 未配置 key（{p['key']}）")
    from langchain_openai import ChatOpenAI  # 用到才 import
    # kind=vision → 用该通道的 vision 模型名；否则用 text 模型名
    return ChatOpenAI(model=p["vision"] if kind == "vision" else p["text"],
                      api_key=key, base_url=p["base_url"],
                      temperature=temperature, timeout=timeout, max_retries=1)
