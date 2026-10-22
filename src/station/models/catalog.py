"""模型目录与能力元数据：来源预置、功能槽位、模型引用与旧配置转换。

只处理普通数据，不查库、不加载供应商 SDK。config 使用本模块解析用户配置。
"""
from __future__ import annotations

import copy
import math
import re
from urllib.parse import urlsplit

ROLES = {"chat": "text_model", "route": "route_model", "vision": "vision_model",
         "speech": "speech_model", "video": "video_model"}
FIELDS = ("id", "label", "name", "capabilities", "reasoning_effort", "route_protocol", "route_threshold")
ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def reference(provider_id, model_id):
    return provider_id + "~" + model_id


def split(value):
    if not isinstance(value, str):
        return "", ""
    parts = value.split("~")
    return (parts[0], parts[1]) if len(parts) == 2 else (value, "")


def models(provider):
    if "models" in provider:
        return provider["models"] if isinstance(provider["models"], list) else []
    out = []
    grouped = {}
    for role, field in ROLES.items():
        if provider.get("route_protocol") == "systemone" and role != "route":
            continue
        name = provider.get(field) or (provider.get("text_model") if role == "route" else "") or ""
        if not name:
            continue
        protocol = provider.get("route_protocol", "chat_completions") if role == "route" else "chat_completions"
        signature = (name, protocol)
        if signature not in grouped:
            value = {"id": "legacy-" + role, "label": name, "name": name,
                     "capabilities": [], "reasoning_effort": "", "route_protocol": protocol,
                     "route_threshold": None}
            grouped[signature] = value
            out.append(value)
        value = grouped[signature]
        value["capabilities"].append(role)
        if role == "chat":
            value["reasoning_effort"] = provider.get("reasoning_effort", "")
        if role == "route":
            value["route_threshold"] = provider.get("route_threshold")
    return out


def select(provider, slot, model_id=""):
    for model in models(provider):
        if (isinstance(model, dict) and isinstance(model.get("capabilities"), list)
                and (not model_id or model.get("id") == model_id) and slot in model["capabilities"]):
            return model
    return None


def clean(values):
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("每个来源最多配置 32 个模型")
    result, ids = [], set()
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("模型配置格式不正确")
        model = {key: copy.deepcopy(raw[key]) for key in FIELDS if key in raw}
        mid, name = model.get("id"), model.get("name")
        if not isinstance(mid, str) or not ID.fullmatch(mid) or mid in ids:
            raise ValueError("模型内部标识无效或重复")
        ids.add(mid)
        if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
            raise ValueError("请填写有效的模型名称（最多 256 个字符）")
        model["name"] = name.strip()
        label = model.get("label", "")
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError("模型显示名称最多 80 个字符")
        caps = model.get("capabilities")
        if (not isinstance(caps, list) or not caps or any(not isinstance(c, str) or c not in ROLES for c in caps)
                or len(caps) != len(set(caps))):
            raise ValueError("请为模型选择有效能力")
        protocol = model.get("route_protocol", "chat_completions")
        if protocol not in ("chat_completions", "systemone") or (protocol == "systemone" and caps != ["route"]):
            raise ValueError("Jev 决策模型只能用于意图识别")
        effort = model.get("reasoning_effort", "")
        if effort not in ("", "low", "medium", "high", "max") or (effort and "chat" not in caps):
            raise ValueError("推理强度只适用于通用对话模型")
        threshold = model.get("route_threshold")
        if threshold not in (None, "") and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                                               or not math.isfinite(threshold) or not 0 <= threshold <= 1):
            raise ValueError("模型意图识别阈值需为 0 到 1 的数字")
        result.append(model)
    return result


def upgrade(cfg):
    """View-only conversion; does not write the DB or change saved selections."""
    cfg = copy.deepcopy(cfg)
    providers = {p["id"]: p for p in cfg.get("providers", [])}
    for p in providers.values():
        p["models"] = models(p)
    for role, chain in cfg.get("slots", {}).items():
        upgraded = []
        for value in chain:
            pid, mid = split(value)
            p = providers.get(pid)
            model = select(p, role, mid) if p else None
            if model:
                upgraded.append(reference(pid, model["id"]))
        cfg["slots"][role] = upgraded
    return cfg

ROUTE_PROTOCOLS = ("chat_completions", "systemone")


SLOT_LABELS = {
    "chat":   "通用模型",
    "route":  "意图识别小模型",
    "vision": "识图模型",
    # ★ 09-24 加的第五类。语音识别接收音频、输出文字；没有内置服务商，
    #   用户自己配置的 provider 填了 speech_model 才会出现在这一行。
    "speech": "语音识别模型",
    # ★ 09-14 加的第四类。它和上面几个**不是同一类能力**：视频是"生成"，
    #   上面几个是"理解/识别/对话"。挂法完全一样 —— 一家服务商在这个槽位有没有
    #   对应能力的模型名（video_model），决定它能不能被选进这一行。
    #   和当初加"识图模型"是同一个形状：agnes 就是"只有视频这一个能力"的家。
    "video":  "视频生成模型",
}


SLOT_HINTS = {
    "chat":   "用于日常对话与文本生成，建议选用能力最强的模型。",
    "route":  "判断请求应路由到哪个功能，建议选用响应快、成本低的模型。",
    "vision": "用于图片内容识别，必须支持视觉输入，纯文本模型不可用。",
    "speech": "用于把音频转成文字，必须选择支持语音识别的模型或接口。",
    "video":  "用于按文字描述生成短视频，必须是视频生成模型，不是对话模型。",
}


_ROLE_KEY = {"chat": "text_model", "route": "route_model",
             "vision": "vision_model", "speech": "speech_model",
             "video": "video_model"}


PROBE_NOT_CHAT = {
    "speech": "语音识别模型不使用对话接口，当前「测试连接」先跳过；保存后由实际语音识别流程验证。",
    "video": "视频模型不走对话接口，没法用「测试连接」试。保存后直接生成一条试试。",
}


REASONING_EFFORTS = ("low", "medium", "high", "max")


BUILTIN_REASONING_EFFORT = {
    "glm": "low",        # 实测：不填 50.5s，填 low 后 0.57s
}


BUILTIN_PROVIDERS = [
    {"id": "glm", "label": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "text_model": "glm-5.3-flash", "vision_model": "glm-5.3-flash",
     "route_model": "", "speech_model": "", "key_env": "GLM_API_KEY"},
    {"id": "deepseek", "label": "DeepSeek",
     "base_url": "https://api.deepseek.com/v1",
     "text_model": "deepseek-flash", "vision_model": "deepseek-flash",
     "route_model": "", "speech_model": "", "key_env": "DEEPSEEK_API_KEY"},
    {"id": "qwen", "label": "通义千问",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "text_model": "qwen-vl-max", "vision_model": "qwen-vl-max",
     # 判词走小模型：qwen 的现有能力里 qwen-flash 最便宜最快。
     # ★ 这个字段就是"干掉 qwen-flash 伪通道"的地方——以前它是一个独立的 provider
     #   条目，正是两份表漂移的源头；现在它只是 qwen 这个 provider 的一个角色模型名。
     "route_model": "qwen-flash", "speech_model": "", "key_env": "QWEN_API_KEY"},
    # Agnes AI：目前只服务「视频生成模型」这一个槽位（见上面的说明）。
    # ★ 国内访问要用 https://apihub.agnes-ai.cn/v1，而 **密钥与节点是绑的** ——
    #   .cn 的 key 打 .com 会 401，反之亦然（官方文档明说）。这里给的是国际站。
    {"id": "agnes", "label": "Agnes AI",
     "base_url": "https://apihub.agnes-ai.com/v1",
     "text_model": "", "vision_model": "", "route_model": "", "speech_model": "",
     # 限时免费档；用户想换标准版/别家，直接在界面里改这一格即可。
     "video_model": "agnes-video-2.5-flash", "key_env": "AGNES_API_KEY"},
]


LEGACY_ALIASES = {"qwen-flash": "qwen"}


MAX_PROVIDERS = 20          # 一家都不用不了这么多，防手改请求塞爆


MAX_CHAIN = 6               # 单条降级链最长几跳（识别是每页一次，链条越长账单越大）


EXTRA_PRESETS = [
    {"id": "opencode-jev", "label": "OpenCode Zen · Jev",
     "base_url": "https://opencode.ai/zen/v1",
     "text_model": "", "vision_model": "", "route_model": "jev-1.13-free",
     "speech_model": "", "video_model": "", "key_env": "", "api_key": "",
     "route_protocol": "systemone", "route_threshold": 0.8,
     "hint": "用于意图识别。填写 OpenCode Zen API Key，再加入上方意图识别顺序；"
             "建议保留原模型作备用。免费模型为限时提供，不自动切换付费版本。"},
    {"id": "ollama", "label": "本地 Ollama（本机部署）",
     "base_url": "http://localhost:11434/v1",
     "text_model": "qwen2.5:14b", "vision_model": "", "route_model": "",
     "speech_model": "",
     "key_env": "", "api_key": "ollama",
     "hint": "运行在本机的模型服务，接口地址固定为 11434 端口，密钥已预填（Ollama 不校验）。"
             "请将模型名改为本地实际已拉取的名称（可用 ollama list 查询）。"},
    {"id": "custom", "label": "其它（OpenAI 兼容接口）",
     "base_url": "", "text_model": "", "vision_model": "", "route_model": "",
     "speech_model": "",
     "key_env": "", "api_key": "",
     "hint": "接入其它服务商或中转站：填写对方提供的接口地址与模型名，并填入密钥。"},
]


LEGACY_PROVIDERS = {
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


def official_search_source(provider: dict) -> bool:
    """Search credentials may only be used against DeepSeek's official origin."""
    try:
        u = urlsplit(provider.get("base_url") or "")
        return (u.scheme == "https" and u.hostname == "api.deepseek.com"
                and u.port in (None, 443) and u.path.rstrip("/") in ("", "/v1")
                and u.username is None and u.password is None and not (u.query or u.fragment)
                and ("models" in provider or provider.get("route_protocol") != "systemone"))
    except (ValueError, TypeError, AttributeError):
        return False


def _alias(pid: str) -> str:
    """把老通道名映射成 provider id（qwen-flash → qwen）。"""
    return LEGACY_ALIASES.get((pid or "").strip(), (pid or "").strip())


def _by_id(cfg: dict, pid: str) -> dict | None:
    """在配置里按 id 找 provider；找不到返回 None。"""
    for p in cfg.get("providers") or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    return None


def _model_for(p: dict, slot: str) -> str:
    """这家 provider 在某个槽位该用哪个模型名；不适合这个槽位返回 ''。"""
    if "models" in p:
        model = select(p, slot)
        return model["name"] if model else ""
    # System One 是决策接口；这一来源只允许参与意图识别。
    if p.get("route_protocol") == "systemone" and slot != "route":
        return ""
    role = _ROLE_KEY.get(slot)
    if role is None:
        return ""
    m = (p.get(role) or "").strip()
    if not m and slot == "route" and p.get("route_protocol") != "systemone":
        # route_model 留空 = 跟文本模型用同一个（内置三家除 qwen 外都是这样）
        m = (p.get("text_model") or "").strip()
    return m


def _scheme_ok(url: str) -> bool:
    """base_url 是否合法：必须 http/https 开头且有主机名。"""
    u = (url or "").strip().lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    return bool(u.split("//", 1)[1].split("/", 1)[0].strip())


def chain_label(entries: list) -> str:
    """把一条链压成一个短标签，用于**缓存键**（如 OCR 缓存："谁读的这一页"）。

    新模型使用 profile 引用与模型名/协议/地址的摘要，避免同来源的不同模型共用结果。
    未改变的旧识图模型保留原 provider 标签，界面升级不会导致已有 OCR 缓存失效。
    """
    if entries and entries[0].get("legacy_cache_id"):
        return entries[0]["legacy_cache_id"]
    if entries and entries[0].get("model_id"):
        import hashlib
        item = entries[0]
        digest = hashlib.sha256((item["model"] + "|" + item.get("route_protocol", "") + "|" + item.get("base_url", "")).encode()).hexdigest()[:8]
        return item["id"] + "-" + digest
    return entries[0]["id"] if entries else ""


def _builtin_reasoning_effort(pid) -> str:
    """内置家在我们**实测过**的基础上的默认推理强度；没测过 = 空串 = 一个字都不发。

    ★ 只写实测过的：宁可让用户点「测试连接」自己验，也别替他猜 ——
      猜错的代价是把参数发给不认它的家，换来一个静默换家的 400。
    """
    return BUILTIN_REASONING_EFFORT.get(str(pid or ""), "")


def slots_view() -> list[dict]:
    """给前端渲染用的槽位清单（顺序固定，每条带一句人话说明）。"""
    return [{"id": s, "label": SLOT_LABELS[s], "hint": SLOT_HINTS[s]}
            for s in SLOT_LABELS]


def presets() -> list[dict]:
    """「＋ 添加模型来源」的模板清单（内置三家不在里面——它们本来就在列表里）。"""
    return [dict(p) for p in EXTRA_PRESETS]
