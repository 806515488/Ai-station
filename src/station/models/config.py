"""用户模型配置用例：读取、兼容、保存、凭据选择和有序通道解析。

元数据与模型引用在 catalog，保存校验在 validation；此处不构造供应商客户端。
"""
from __future__ import annotations
import copy
import os
from station import config
from station.models import catalog, validation
from station.persistence.repositories import settings as settings_records
from station.persistence.repositories import users as user_records

CURRENT_VERSION = 2


DEFAULT_WEB_SEARCH = {"enabled": True, "provider_id": "deepseek"}


def _env(name: str) -> str:
    """取环境变量并去空格；没有/空返回 ''。"""
    return (os.environ.get(name or "") or "").strip()


def _api_key(p: dict, allow_env: bool = True) -> str:
    """取这家 provider 实际要用的 key：**用户填的优先，留空才读 .env**。

    注意 `p["key_env"]` 是变量名（GLM_API_KEY），不是密钥本身。

    allow_env=False 时**不读 .env**（直接回空串）—— 这是"新账号不该白用站长密钥"的
    落点：站是部署的人自己掏钱开的，.env 里那几把只给站长。判断在 resolve/probe_entry
    里做（走 user_records.is_owner），这里只收一个开关，保持函数纯粹、好测。
    """
    stored = (p.get("api_key") or "").strip()
    if stored:
        return stored
    return _env(p.get("key_env") or "") if allow_env else ""


def _default_slots(providers: list) -> dict:
    """零配置时的各槽位链：**链首都读 env**，所以 .env 里的
    TEXT_CHANNEL / VISION_CHANNEL / VIDEO_CHANNEL / SPEECH_CHANNEL /
    STATION_*_CHANNEL / ROUTE_CHANNEL 依然生效。

    语音识别没有内置服务商，所以默认链为空；用户添加并配置自己的 ASR provider 后，
    可以在界面里把它选进「语音识别模型」这一行。

    这就是"没配置 = 行为和以前一模一样"的保证：链首等于今天代码里写死的那个通道。
    """
    config.load_env()                     # 先确保 .env 已进环境变量（幂等）
    ids = {p["id"] for p in providers}

    def _safe(pid: str, fallback: str) -> str:
        """链首 id 不认识/写错了就回落到兜底值——宁可回默认，也不能让对话整个不可用。"""
        pid = catalog._alias(pid)
        return pid if pid in ids else fallback

    speech = _safe(config.channel_for("speech"), "")
    speech_provider = next((p for p in providers if p.get("id") == speech), None)
    if speech_provider is None or not catalog._model_for(speech_provider, "speech"):
        speech = ""

    return {
        "chat":   [_safe(config.channel_for("text"), "glm")],
        "route":  [_safe(_env("ROUTE_CHANNEL") or config.ROUTE_CHANNEL, "qwen")],
        "vision": [_safe(config.channel_for("vision"), "qwen")],
        # 没有内置 ASR 家；默认空链，等用户配置自己的语音识别来源。
        "speech": [speech] if speech else [],
        # 视频这一行的默认链首来自 DEFAULT_CHANNEL["video"]（= agnes），
        # 可以用 VIDEO_CHANNEL 环境变量覆盖。链首那家要是被用户删了就回落 agnes。
        "video":  [_safe(config.channel_for("video"), "agnes")],
    }


def default_config() -> dict:
    """合成一份"什么都没配过"的默认配置（**只在内存里，绝不写库**）。

    内置三家原样带上、api_key 留空（于是自然回落到 .env）。DB 里不会因为读了
    一次默认配置就凭空多出一行。
    """
    providers = []
    for b in catalog.BUILTIN_PROVIDERS:
        p = {k: v for k, v in b.items()}
        p["api_key"] = ""
        # 推理强度按内置默认填好：用户**打开就是对的**，不需要先理解它是什么。
        p["reasoning_effort"] = catalog._builtin_reasoning_effort(b["id"])
        p["builtin"] = True
        providers.append(p)
    return {"version": CURRENT_VERSION,
            "providers": providers,
            "web_search": dict(DEFAULT_WEB_SEARCH),
            "slots": _default_slots(providers)}


def _normalize(raw: dict) -> dict:
    """把库里存的配置"修一遍"再用：补齐新加的内置家、丢掉链里的悬空 id。

    为什么必须容错而不是报错：一份坏配置会让对话整个不可用，而用户根本不知道该去
    哪儿改。所以这里一律**静默修复**，能跑就先跑起来。
    """
    cfg = copy.deepcopy(raw)
    cfg["version"] = CURRENT_VERSION

    providers = [p for p in (cfg.get("providers") or []) if isinstance(p, dict)]

    # 内置家回填：将来代码里新加一家，老配置也能用上（用户没改过它就用默认值）
    have = {p.get("id") for p in providers}
    for b in catalog.BUILTIN_PROVIDERS:
        if b["id"] not in have:
            p = {k: v for k, v in b.items()}
            p["api_key"] = ""
            p["reasoning_effort"] = catalog._builtin_reasoning_effort(b["id"])
            p["builtin"] = True
            providers.append(p)

    # 老配置回填「推理强度」（09-20 新加的字段）：库里存的那份没有这一栏时，
    # 按内置默认补上 —— 否则**已经存过配置的老用户永远拿不到"glm 默认 low"**
    # 这个我们实测出来的正确值（他们打开界面看到的是空白，又不知道要不要填）。
    # ★ 判据是"键在不在"而不是"值空不空"（同 api_key 的三态）：用户**主动清空**过
    #   的家，键是存在的、值为 ""，这里绝不能给他填回去 —— 那是覆盖用户的选择。
    for p in providers:
        if "reasoning_effort" not in p:
            p["reasoning_effort"] = catalog._builtin_reasoning_effort(p.get("id"))

    ids = {p.get("id") for p in providers}
    slots = {}
    for slot in catalog.SLOT_LABELS:
        chain = (cfg.get("slots") or {}).get(slot) or []
        # 悬空 id（provider 被删了但链里还留着）静默剔除，并在末尾空时补上默认
        clean = [ref for ref in chain if isinstance(ref, str) and catalog.split(ref)[0] in ids
                 and (not catalog.split(ref)[1] or catalog.select(catalog._by_id({"providers": providers}, catalog.split(ref)[0]), slot, catalog.split(ref)[1]))]
        explicit_empty = slot in (cfg.get("slots") or {}) and chain == [] and any("models" in p for p in providers)
        if not clean and not explicit_empty:
            clean = _default_slots(providers).get(slot) or []
        slots[slot] = clean[:catalog.MAX_CHAIN]

    cfg["providers"] = providers
    cfg["slots"] = slots
    if "web_search" not in cfg:
        cfg["web_search"] = dict(DEFAULT_WEB_SEARCH)
    return cfg


def load_config(user_id: str) -> dict:
    """读某用户的**完整**配置（含明文 key）；没配过 → 内存里合成默认。

    返回值可能含明文密钥 —— 只许喂给 mask_config / resolve，别直接进响应或日志。
    """
    raw = settings_records.model_config_load(user_id) if user_id else None
    if not isinstance(raw, dict):
        return default_config()
    ver = raw.get("version")
    if not isinstance(ver, int) or ver > CURRENT_VERSION:
        # 版本比代码新：不猜它的语义，回默认并把原因带出去让前端提示。
        cfg = default_config()
        cfg["warn"] = (f"配置版本 {ver} 比当前程序支持的 {CURRENT_VERSION} 新，"
                       f"已按默认配置运行")
        return cfg
    return _normalize(raw)


def mask_config(cfg: dict, reveal: bool = False, allow_env: bool = True) -> dict:
    """把配置整理成"能给前端看"的形状，并标明每家的 key 是从哪来的。

    reveal=False（默认，给日志/调试等"只想看结构"的调用方）：
        key 字段被摘掉，只留 has_key / key_source —— 不含明文。
    reveal=True（给界面用）：
        带上**实际会用的那把** key（用户自己存的优先，没有才是 src/.env 读到的），
        前端默认用密码框显示成 ****、点「显示」才明文 —— 让用户能核对自己存的密钥。

    ⚠ reveal=True 是 09-10 用户明确拍板的：单人自部署的站，配置页要能看见并改自己的
      密钥，不能只给个"已配置"的黑盒。代价是 key 会出现在浏览器的网络响应里 ——
      别把这个端点做成公开的（它一直是 require_user 后面的），也别往里加缓存。

    key_source 三态（前端据此写提示文案）：
      db   → 你自己填的（改这里会覆盖它）
      env  → 来自 src/.env 的 <KEY_ENV>（在这里填会覆盖它）
      none → 都还没有

    allow_env=False（非站长，见 _api_key）→ 假装 .env 里什么都没有：这些家全部落到
      "none"，界面显示黄点 +「还没填密钥」，引导他自己去填。传这个开关的是 server 层
      （它知道当前登录的是谁），别在这里自己查库——保持这一层纯粹、好测。
    """
    config.load_env()
    out = copy.deepcopy(cfg)
    for p in out.get("providers") or []:
        stored = (p.get("api_key") or "").strip()      # 用户自己存的
        env_val = _env(p.get("key_env") or "") if allow_env else ""   # 兜底的（.env）
        if stored:
            p["has_key"], p["key_source"] = True, "db"
        elif env_val:
            p["has_key"], p["key_source"] = True, "env"
        else:
            p["has_key"], p["key_source"] = False, "none"
        if reveal:
            p["api_key"] = stored or env_val           # 实际会用的那把
        else:
            p.pop("api_key", None)
    return out


def save_config(user_id: str, incoming: dict) -> dict:
    """校验并保存某用户的整包配置；返回存进去的配置。校验不过抛 ValueError。

    ★ key 的三态契约（最容易写错、写错了会静默丢用户的 key，务必看清）：
        字段缺失 / null  → **不改**（沿用库里已有的；库里没有就继续读 .env）
        ""（空串）        → **显式清空**（删掉库里那份，回落到 .env 的 key_env）
        非空串            → **覆盖**（写进库）
      前端对应三个可见状态：灰占位符=不改 / 输入框有字=覆盖 / 点「清除」= 发空串。
    """
    if not isinstance(incoming, dict):
        raise ValueError("配置格式不正确")
    cfg = copy.deepcopy(incoming)
    cfg["version"] = CURRENT_VERSION

    # 只留认识的字段：界面上可能会带点自己的东西（比如给用户看的提示语），
    # 不筛的话会一路存进库里，越积越脏。
    # ★ 注意用 `if k in p` 而不是补空串：**字段"缺失"和"空串"含义不同**（见下面
    #   api_key 的三态）。补成空串会把"不改动"误判成"清空"（这条踩过）。
    # ★ 加一个新的模型名字段时**必须**同步加进这个元组，否则它会被静默丢掉
    #   （用户填了、保存也不报错，就是下次打开发现空了）。
    _KEEP = ("id", "label", "base_url", "text_model", "vision_model",
             "route_model", "speech_model", "video_model", "key_env", "api_key",
             "reasoning_effort", "route_protocol", "route_threshold", "models")
    cfg["providers"] = [{k: p[k] for k in _KEEP if k in p}
                        for p in (cfg.get("providers") or []) if isinstance(p, dict)]

    # —— key 三态合并：拿库里上一版对照 ——
    prev = settings_records.model_config_load(user_id) or {}
    previous = {p.get("id"): p for p in prev.get("providers", []) if isinstance(p, dict)}
    for p in cfg["providers"]:
        old = previous.get(p.get("id"), {})
        if "models" not in p and "models" in old:
            if any(k in p and p[k] != old.get(k, "") for k in catalog.ROLES.values()):
                raise ValueError("配置已包含多个模型，请刷新页面后编辑")
            p["models"] = copy.deepcopy(old["models"])
        if "models" in p:
            p["models"] = catalog.clean(p["models"])
    # Old clients submit providers/slots only: preserve the saved search choice.
    web = cfg.get("web_search", prev.get("web_search", DEFAULT_WEB_SEARCH))
    if isinstance(web, dict):
        web = {k: web[k] for k in ("enabled", "provider_id") if k in web}
        old_ids = {p.get("id") for p in prev.get("providers", [])}
        new_ids = {p.get("id") for p in cfg.get("providers", [])}
        if isinstance(web.get("provider_id"), str) and web["provider_id"] in old_ids - new_ids:
            web["provider_id"] = ""
        if "web_search" not in cfg and isinstance(web.get("provider_id"), str) and web["provider_id"] not in new_ids:
            web["provider_id"] = ""
    cfg["web_search"] = copy.deepcopy(web)
    prev_keys = {p.get("id"): (p.get("api_key") or "")
                 for p in (prev.get("providers") or []) if isinstance(p, dict)}
    for p in cfg.get("providers") or []:
        if not isinstance(p, dict):
            continue
        if "api_key" not in p or p.get("api_key") is None:
            p["api_key"] = prev_keys.get(p.get("id"), "")     # 不改
        else:
            p["api_key"] = str(p.get("api_key") or "").strip()  # ""=清空，非空=覆盖
        p["builtin"] = p.get("id") in {b["id"] for b in catalog.BUILTIN_PROVIDERS}

    # —— 服务端级联去悬空 id：删 provider 时同步从所有链里剔除 ——
    # （前端也会做一次，但这里必须有：手改请求能塞进来悬空 id）
    ids = {p.get("id") for p in cfg.get("providers") or [] if isinstance(p, dict)}
    for slot in list((cfg.get("slots") or {}).keys()):
        if slot not in catalog.SLOT_LABELS:
            cfg["slots"].pop(slot, None)         # 不认识的槽位直接丢弃
    for slot in catalog.SLOT_LABELS:
        chain = (cfg.get("slots") or {}).get(slot) or []
        keep = []
        for ref in chain:
            pid, mid = catalog.split(ref)
            if pid not in ids:
                continue
            now = next(p for p in cfg["providers"] if p["id"] == pid)
            old = previous.get(pid, {})
            if mid and catalog.select(old, slot, mid) and not any(m.get("id") == mid for m in catalog.models(now)):
                continue  # Deleting a known model removes only its own bindings.
            keep.append(ref)
        cfg.setdefault("slots", {})[slot] = keep

    problems = validation._validate(cfg)
    if problems:
        raise ValueError("；".join(problems))

    cfg["version"] = 2 if any("models" in p for p in cfg["providers"]) else 1

    cfg.pop("warn", None)                        # warn 只是给前端看的临时字段
    settings_records.model_config_save(user_id, cfg)
    return cfg


def delete_config(user_id: str) -> None:
    """恢复默认：把库里那份删掉（下次 load_config 又回到内置默认）。"""
    settings_records.model_config_delete(user_id)


def search_entry(user_id: str) -> tuple[dict | None, str]:
    """Resolve at call time; no text-model name is required, no shared user secrets."""
    if not user_id:
        return None, "网页检索需要登录"
    config.load_env()
    cfg = load_config(user_id)
    web = cfg.get("web_search")
    if not isinstance(web, dict) or web.get("enabled") is not True:
        return None, "网页检索已关闭"
    pid = web.get("provider_id")
    p = catalog._by_id(cfg, pid) if isinstance(pid, str) else None
    if p is None or not catalog.official_search_source(p):
        return None, "请在模型配置的网页检索中选择官方 DeepSeek 来源"
    key = _api_key(p, user_records.is_owner(user_id))
    if not key:
        return None, "网页检索缺少密钥，请在模型配置中填写所选来源的 DeepSeek API Key"
    return {"id": p["id"], "api_key": key}, ""


def resolve(user_id: str, slot: str) -> list[dict]:
    """把某用户某个槽位解析成**有序条目**（降级顺序），调用方按顺序去建客户端。

    返回 [{"id","label","base_url","api_key","key_env","model","slot"}, ...]
    —— 列表为空 = 这个槽位没有任何能用的条目（调用方自己决定怎么报错）。

    刻意**不在这里过滤"没配 key"的条目**：让 Model 构造时报错、然后按链切下一家，
    这样"没 key 自动换一家"和"网络失败自动换一家"走的是同一套机制，报错也更具体
    （"glm 缺 key → 试 qwen"）。也刻意**不加缓存**——用户点保存后，下一轮对话就要生效。

    ★ .env 兜底只给站长（user_records.is_owner；空 user_id 的 CLI/后台路径也算站长）。
      非站长解析出来的 api_key 是空串 → Model 抛出"去界面里填密钥"的人话提示。
      allow_env 也一并塞进每条 entry，好让 Model 知道该报哪种文案（它自己查不到用户）。
    """
    cfg = load_config(user_id)
    allow_env = user_records.is_owner(user_id)
    out: list[dict] = []
    seen: set[str] = set()
    for pid in (cfg.get("slots") or {}).get(slot) or []:
        if pid in seen:
            continue
        seen.add(pid)
        source_id, model_id = catalog.split(pid)
        p = catalog._by_id(cfg, source_id)
        if p is None:
            continue
        e = _entry(p, slot, allow_env, model_id)
        if e is None:                # 这家不参与该槽位（例如没填 vision 模型名）
            continue
        out.append(e)
    return out


def _entry(p: dict, slot: str, allow_env: bool, model_id: str = "") -> dict | None:
    """把一家 provider 解析成一条"可建连的 entry"；这家在该槽位没有模型名就返回 None。

    ★ 形状只有这一处定义（resolve 的每条、resolve_one 的单条都走这里）：
      调用方拿到的 entry 长什么样必须一致，否则"链里那家"和"单独指定那家"
      行为会悄悄不同。
    """
    profile = catalog.select(p, slot, model_id) if "models" in p or model_id else None
    if ("models" in p or model_id) and profile is None:
        return None
    model = profile["name"] if profile else catalog._model_for(p, slot)
    if not model:
        return None
    pid = p.get("id") or ""
    options = profile if profile else p
    entry = {"id": catalog.reference(pid, profile["id"]) if profile else pid,
            "label": ((p.get("label") or pid) + " / " + (profile.get("label") or model)) if profile else (p.get("label") or pid),
            "base_url": (p.get("base_url") or "").strip(),
            "api_key": _api_key(p, allow_env),
            "key_env": p.get("key_env") or "",
            "allow_env": allow_env,          # 给 Model 选报错文案用
            "model": model,
            "slot": slot,
            "route_protocol": options.get("route_protocol", "chat_completions"),
            "route_threshold": options.get("route_threshold"),
            # 空串 = **一个字都不发**（不是"发个空的"）。见 REASONING_EFFORTS 的说明：
            # 不认这个参数的家收到它会 400，而 400 会让降级链静默换家。
            # ★★ 只对**通用模型（chat）**那一条生效，别的槽位一律空串、不发：
            #    这是"对话要多想多久"的档位 —— 判词要的是快（它本来就有 10 秒总预算）、
            #    视频压根不是对话模型、识图走的是另一条链。给它们发没有任何意义，
            #    只会多一个"对端不认就 400 → 静默换家"的机会。
            "reasoning_effort": ((options.get("reasoning_effort") or "").strip()
                                 if slot == "chat" else "")}
    if profile:
        entry.update(provider_id=pid, model_id=profile["id"])
        if slot == "vision":
            legacy = catalog.select({k: v for k, v in p.items() if k != "models"}, slot)
            if legacy and legacy["id"] == profile["id"] and legacy["name"] == profile["name"]:
                entry["legacy_cache_id"] = pid
    return entry


def resolve_one(user_id: str, provider_id: str, slot: str) -> list[dict]:
    """把**指定的一家**解析成单条 entry（"换个模型重跑"用）；不适用就返回 []。

    与 resolve 的区别：只看这一家、不排队、**不管它在不在槽位的降级链里**
    （用户可能就是想单独拿某家跑一遍对比）。返回形状与 resolve 的每条一致。
    """
    cfg = load_config(user_id)
    pid, mid = catalog.split(provider_id)
    p = catalog._by_id(cfg, pid)
    if p is None:
        return []
    e = _entry(p, slot, user_records.is_owner(user_id), mid)
    return [e] if e else []


def vision_providers(user_id: str) -> list[dict]:
    """**所有配了识图模型的家**（不管它在不在「识图模型」槽的降级链里）—— 报菜单用。

    为什么与 resolve 不同：resolve 只给**链里**的家（那才是"默认识别会用到的"），
    而用户要换模型重跑时，应该能看到**任何一家能看图的**（哪怕没排进链）。
    返回 [{"id","label","model","in_chain","order"}]，顺序：链里的按链序在前，
    其余按配置顺序在后。**不含 key**（只用来报菜单，不建客户端）。
    """
    cfg = load_config(user_id)
    chain = []
    for ref in (cfg.get("slots") or {}).get("vision", []):
        pid, mid = catalog.split(ref)
        p = catalog._by_id(cfg, pid)
        if not p:
            continue
        m = catalog.select(p, "vision", mid) if "models" in p or mid else None
        chain.append(catalog.reference(pid, m["id"]) if m else ref)
    out = []
    for p in cfg.get("providers") or []:
        variants = [(catalog.reference(p["id"], m["id"]), m["name"], m.get("label") or m["name"])
                    for m in catalog.models(p) if "vision" in m.get("capabilities", [])] if "models" in p else []
        if "models" not in p and catalog._model_for(p, "vision"):
            variants = [(p["id"], catalog._model_for(p, "vision"), "")]
        for ref, name, label in variants:
            out.append({"id": ref, "label": (p.get("label") or p["id"]) + (" / " + label if label else ""),
                        "model": name, "in_chain": ref in chain,
                        "order": chain.index(ref) if ref in chain else len(chain) + 1})
    out.sort(key=lambda d: d["order"])
    return out


def probe_entry(user_id: str, provider_id: str,
                slot: str = "", model_id: str = "") -> tuple[str, str, str, str, str]:
    """给「试一下能不能用」端点用：从**已保存的**配置里取 (base_url, api_key, model, slot)。

    刻意只认已保存的 provider id、**不接受裸 URL** —— 这样"测试"这个动作对外能到达
    的范围就被收敛成"你自己配置里的那几个地址"，没法被拿来探测任意内网服务。

    slot 留空 = 自动挑一个这家有的模型名（对话优先，最后才轮到视频）—— 界面上是按
    "这一家"测的，用户不该被要求先理解槽位。

    ★ 09-14 起**多回一个"命中的是哪个槽位"**：调用方靠它判断"这个模型能不能用对话
      接口试连"（语音/视频模型不能，见 PROBE_NOT_CHAT）。返回值从 3 元组变 4 元组，
      全仓只有 app/routes/models.py 的测试端点一处解包（没有测试引用过它）。
    ★ 09-20 再**多回第 5 个「推理强度」**（空串 = 不发）：端点用它做两趟探测，
      把"参数被拒"和"连不上"分开报（见 app/routes/models.py 的测试端点）。
    """
    cfg = load_config(user_id)
    pid, mid = catalog.split(provider_id)
    model_id = model_id or mid
    p = catalog._by_id(cfg, pid)
    if p is None:
        raise ValueError("未找到该模型来源，请先保存配置")
    # video 排最后：内置那几家、以及用户自己加的家都是先命中 chat，行为一字不变；
    # 只有"除了视频模型名什么都没有"的家（agnes）才会落到 video。
    if "models" in p or model_id:
        order = [slot] if slot else ["chat", "vision", "route", "speech", "video"]
        for s in order:
            entry = _entry(p, s, user_records.is_owner(user_id), model_id)
            if entry:
                return entry["base_url"], entry["api_key"], entry["model"], s, entry["reasoning_effort"]
        raise ValueError("该模型不存在或不支持所选能力")
    order = ([slot] if slot else []) + ["chat", "vision", "route", "speech", "video"]
    for s in order:
        m = catalog._model_for(p, s)
        if m:
            # 非站长在这里同样拿不到 .env 的 key —— 于是"测试连接"测的就是他真正
            # 会用的那把；否则会出现"测试通过、一说话就报缺 key"的假绿灯。
            return ((p.get("base_url") or "").strip(),
                    _api_key(p, user_records.is_owner(user_id)), m, s,
                    # 第 5 个：这家填的「推理强度」（空串 = 不发）。
                    # 端点拿它做**两趟探测** —— 先不带参数确认"连得上"，
                    # 再带上参数确认"认不认"，好把"你的参数被拒了"和"你连不上"
                    # 分开报，别让用户去改一个本来正确的地址。
                    # ★ 与 _entry 同一口径：**只有命中通用模型那一行才验** ——
                    #   别的槽位本来就不会发这个参数，验了反而误导（"这里标红"
                    #   会让用户以为识图也受影响）。
                    ((p.get("reasoning_effort") or "").strip() if s == "chat" else ""))
    raise ValueError("该来源尚未填写模型名，请填写后重试")


def legacy_entry(kind: str, channel: str | None) -> dict:
    """老写法（只给通道名）→ 一条 entry。表仍读 catalog.LEGACY_PROVIDERS，与改造前同源。"""
    ch = channel or config.channel_for(kind)
    p = catalog.LEGACY_PROVIDERS.get(ch)
    if p is None:
        raise RuntimeError(f"未知通道 {ch}，可选 {list(catalog.LEGACY_PROVIDERS)}")
    return {"id": ch, "label": ch, "slot": "",
            "base_url": p["base_url"],
            "api_key": (os.environ.get(p["key"]) or "").strip(),
            "key_env": p["key"],
            # 视觉任务用该通道的 vision 模型，否则用 text 模型
            "model": p["vision"] if kind == "vision" else p["text"]}
