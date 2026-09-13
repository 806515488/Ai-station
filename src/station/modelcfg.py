"""modelcfg —— 模型配置的**唯一真源**：内置 provider 表 + 每用户配置 + 槽位降级链。

为什么要有这个文件（改这一块之前先读这段）：
  以前"用哪家模型"写死在两处代码常量里——宿主侧 `core/model.py` 的 `_PROVIDERS`、
  识别侧 `archive/engine/providers.py` 的 `PROVIDERS`——而且两份表已经开始漂移
  （同是 deepseek，一边写 `deepseek-flash`、一边写 `deepseek-v4-flash`）。
  现在收敛到这一份：**内置表在这、用户的改动存 DB、解析成"可建连的条目"也在这**。

两个核心概念（别混）：
  - **provider（服务商）= 连接 + 模型名**：base_url、api_key、以及三个角色对应的
    模型名（text_model / vision_model / route_model）。
  - **slot（槽位）= 用谁、按什么顺序**：一个有序的 provider id 列表，第一位是主通道，
    后面是降级顺序。三个槽位见 SLOT_LABELS。

新手视角（Java 朋友版）：这个文件 ≈ 一份"数据源配置 + 解析器"，纯数据、不联网、
不 import langchain。它只回答两件事：
  ① 某人现在配置长什么样（load_config / mask_config）；
  ② 某个槽位该按什么顺序去连（resolve → 一串条目，调用方拿去造客户端）。
真正"连"的动作在 core/model.py（宿主）和 archive/engine/providers.py（识别），
它们只在构造失败/调用失败时按这个顺序往下试——降级策略不在这儿。

安全红线：`load_config()` 的返回值里**可能含明文 api_key**。它只许喂给本文件的
解析/掩码函数，**绝不能**直接进 HTTP 响应、日志或 route_log。
"""
from __future__ import annotations

import copy
import os
import re

from station import config, db

# 配置格式版本：将来结构大改时 +1。读到比这更大的版本不猜，回默认并给 warn。
CURRENT_VERSION = 1

# 四个槽位：id → 给人看的名字 + 一句人话说明（前端直接用）。
# 名字刻意用大白话：这个弹窗是给不写代码的人用的，"路由判词""建档"这类内部叫法
# 只有我们自己看得懂，别摆到界面上。
#
# ★ 09-11 砍掉原「给材料分类」(text) 槽：它和「通用模型」用的是**同一个角色模型名**
#   (text_model)，单列一行只会让用户以为"这两个得分别配"，白多一份纠结。
#   档案识别定类改走 chat 槽（见 archive/station_adapter）。老配置里存的 slots.text
#   由 _normalize / save_config 静默丢弃（它们只认 SLOT_LABELS 里有的键）。
SLOT_LABELS = {
    "chat":   "通用模型",
    "route":  "意图识别小模型",
    "vision": "识图模型",
}
SLOT_HINTS = {
    "chat":   "用于日常对话与文本生成，建议选用能力最强的模型。",
    "route":  "判断请求应路由到哪个功能，建议选用响应快、成本低的模型。",
    "vision": "用于图片内容识别，必须支持视觉输入，纯文本模型不可用。",
}

# 每个槽位该取 provider 的哪个模型名角色。
_ROLE_KEY = {"chat": "text_model", "route": "route_model",
             "vision": "vision_model"}

# 内置三家。`key_env` 是"去哪个环境变量找 key"（即 src/.env 里那几行）。
# 注意：这里就是那份漂移了两处的表的合并结果，口径是"取 station 侧的值"——
# 因为 Web 真在跑的是它；archive 的 CLI 走自己那张 PROVIDERS 表，不受影响。
BUILTIN_PROVIDERS = [
    {"id": "glm", "label": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "text_model": "glm-5.3-flash", "vision_model": "glm-5.3-flash",
     "route_model": "", "key_env": "GLM_API_KEY"},
    {"id": "deepseek", "label": "DeepSeek",
     "base_url": "https://api.deepseek.com/v1",
     "text_model": "deepseek-flash", "vision_model": "deepseek-flash",
     "route_model": "", "key_env": "DEEPSEEK_API_KEY"},
    {"id": "qwen", "label": "通义千问",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "text_model": "qwen-vl-max", "vision_model": "qwen-vl-max",
     # 判词走小模型：qwen 的现有能力里 qwen-flash 最便宜最快。
     # ★ 这个字段就是"干掉 qwen-flash 伪通道"的地方——以前它是一个独立的 provider
     #   条目，正是两份表漂移的源头；现在它只是 qwen 这个 provider 的一个角色模型名。
     "route_model": "qwen-flash", "key_env": "QWEN_API_KEY"},
]

# 老写法 → 新 id 的别名。**这条不能删**：.env.example 里写着 ROUTE_CHANNEL=qwen-flash，
# 用户本机 .env 随时可能填着它；没有映射的话判词会静默失效（不报错，只是变慢变贵）。
LEGACY_ALIASES = {"qwen-flash": "qwen"}

MAX_PROVIDERS = 20          # 一家都不用不了这么多，防手改请求塞爆
MAX_CHAIN = 6               # 单条降级链最长几跳（识别是每页一次，链条越长账单越大）

# 「＋ 添加模型来源」的模板。内置三家本来就在列表里，这里补的是"自己搭的"那两种。
# 为什么要模板：这个页面最劝退人的一步就是"接口地址"——普通人根本不知道该填什么。
# 给现成的填好，他只负责粘密钥。
EXTRA_PRESETS = [
    {"id": "ollama", "label": "本地 Ollama（本机部署）",
     "base_url": "http://localhost:11434/v1",
     "text_model": "qwen2.5:14b", "vision_model": "", "route_model": "",
     "key_env": "", "api_key": "ollama",
     "hint": "运行在本机的模型服务，接口地址固定为 11434 端口，密钥已预填（Ollama 不校验）。"
             "请将模型名改为本地实际已拉取的名称（可用 ollama list 查询）。"},
    {"id": "custom", "label": "其它（OpenAI 兼容接口）",
     "base_url": "", "text_model": "", "vision_model": "", "route_model": "",
     "key_env": "", "api_key": "",
     "hint": "接入其它服务商或中转站：填写对方提供的接口地址与模型名，并填入密钥。"},
]


# ── 小工具 ───────────────────────────────────────────────────────────

def _env(name: str) -> str:
    """取环境变量并去空格；没有/空返回 ''。"""
    return (os.environ.get(name or "") or "").strip()


def _alias(pid: str) -> str:
    """把老通道名映射成 provider id（qwen-flash → qwen）。"""
    return LEGACY_ALIASES.get((pid or "").strip(), (pid or "").strip())


def _by_id(cfg: dict, pid: str) -> dict | None:
    """在配置里按 id 找 provider；找不到返回 None。"""
    for p in cfg.get("providers") or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    return None


def _api_key(p: dict, allow_env: bool = True) -> str:
    """取这家 provider 实际要用的 key：**用户填的优先，留空才读 .env**。

    注意 `p["key_env"]` 是变量名（GLM_API_KEY），不是密钥本身。

    allow_env=False 时**不读 .env**（直接回空串）—— 这是"新账号不该白用站长密钥"的
    落点：站是部署的人自己掏钱开的，.env 里那几把只给站长。判断在 resolve/probe_entry
    里做（走 db.is_owner），这里只收一个开关，保持函数纯粹、好测。
    """
    stored = (p.get("api_key") or "").strip()
    if stored:
        return stored
    return _env(p.get("key_env") or "") if allow_env else ""


def _model_for(p: dict, slot: str) -> str:
    """这家 provider 在某个槽位该用哪个模型名；不适合这个槽位返回 ''。"""
    role = _ROLE_KEY.get(slot)
    if role is None:
        return ""
    m = (p.get(role) or "").strip()
    if not m and slot == "route":
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

    取链首 provider 的 id（不是模型名）。这样：内置三家的标签与改造前**完全一致**
    （qwen/glm/deepseek），老缓存照常命中、不白花一次重读钱；换了 provider 标签就变，
    不会把上一家读出来的结果当成这一家的。

    已知取舍：在**同一个 provider 里改模型名**不会让 OCR 缓存失效（标签没变）——
    真要重读就换个 provider，或删掉 data/ocr_cache 里对应的条目。见 conventions 坑区。
    """
    return entries[0]["id"] if entries else ""


# ── 默认配置 / 规范化 ─────────────────────────────────────────────────

def _default_slots(providers: list) -> dict:
    """零配置时的四条链：**链首都读 env**，所以 .env 里的
    TEXT_CHANNEL / VISION_CHANNEL / STATION_*_CHANNEL / ROUTE_CHANNEL 依然生效。

    这就是"没配置 = 行为和以前一模一样"的保证：链首等于今天代码里写死的那个通道。
    """
    config.load_env()                     # 先确保 .env 已进环境变量（幂等）
    ids = {p["id"] for p in providers}

    def _safe(pid: str, fallback: str) -> str:
        """链首 id 不认识/写错了就回落到兜底值——宁可回默认，也不能让对话整个不可用。"""
        pid = _alias(pid)
        return pid if pid in ids else fallback

    return {
        "chat":   [_safe(config.channel_for("text"), "glm")],
        "route":  [_safe(_env("ROUTE_CHANNEL") or config.ROUTE_CHANNEL, "qwen")],
        "vision": [_safe(config.channel_for("vision"), "qwen")],
    }


def default_config() -> dict:
    """合成一份"什么都没配过"的默认配置（**只在内存里，绝不写库**）。

    内置三家原样带上、api_key 留空（于是自然回落到 .env）。DB 里不会因为读了
    一次默认配置就凭空多出一行。
    """
    providers = []
    for b in BUILTIN_PROVIDERS:
        p = {k: v for k, v in b.items()}
        p["api_key"] = ""
        p["builtin"] = True
        providers.append(p)
    return {"version": CURRENT_VERSION,
            "providers": providers,
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
    for b in BUILTIN_PROVIDERS:
        if b["id"] not in have:
            p = {k: v for k, v in b.items()}
            p["api_key"] = ""
            p["builtin"] = True
            providers.append(p)

    ids = {p.get("id") for p in providers}
    slots = {}
    for slot in SLOT_LABELS:
        chain = (cfg.get("slots") or {}).get(slot) or []
        # 悬空 id（provider 被删了但链里还留着）静默剔除，并在末尾空时补上默认
        clean = [pid for pid in chain
                 if isinstance(pid, str) and pid in ids]
        if not clean:
            clean = _default_slots(providers).get(slot) or []
        slots[slot] = clean[:MAX_CHAIN]

    cfg["providers"] = providers
    cfg["slots"] = slots
    return cfg


def load_config(user_id: str) -> dict:
    """读某用户的**完整**配置（含明文 key）；没配过 → 内存里合成默认。

    返回值可能含明文密钥 —— 只许喂给 mask_config / resolve，别直接进响应或日志。
    """
    raw = db.model_config_load(user_id) if user_id else None
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


# ── 掩码（给前端看的那一份）──────────────────────────────────────────

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


# ── 保存（含校验 + key 三态）─────────────────────────────────────────

def _validate(cfg: dict) -> list[str]:
    """检查配置是否合理；返回**人话**问题清单（空列表 = 通过）。

    刻意不用 pydantic 的 422 原始结构：这些问题最终要显示在弹窗里给用户看，
    "field required" 这种文案帮不上忙。
    """
    def _name(p: dict) -> str:
        """报错时怎么称呼这一家：优先用用户填的名字，没有才退回落标识。"""
        return str(p.get("label") or "").strip() or str(p.get("id") or "?")

    problems: list[str] = []
    providers = cfg.get("providers")
    if not isinstance(providers, list) or not providers:
        return ["至少要保留一个模型来源"]
    if len(providers) > MAX_PROVIDERS:
        problems.append(f"模型来源过多（最多 {MAX_PROVIDERS} 个）")

    # 先看哪些模型来源**真的被上面某一行用上了**。
    # ★ 内容完整性的检查只针对这些"在用"的家：刚加进来、还没来得及填地址的，
    #   应该允许当成草稿存下来（用户的心智是"我先把密钥存着"）。否则他会被两个
    #   自己还没打算填的字段挡住，表现就是"我加了密钥却不能保存"（真发生过）。
    used: set[str] = set()
    for slot in SLOT_LABELS:
        chain = (cfg.get("slots") or {}).get(slot) or []
        if isinstance(chain, list):
            used |= {str(x) for x in chain}

    ids: set[str] = set()
    by_id: dict[str, dict] = {}
    for p in providers:
        pid = str(p.get("id") or "").strip()
        # —— 结构检查：所有家都要过（id 是骨架，缺了/重了/脏了会出乱子）——
        if not pid:
            problems.append("有一个模型来源缺少内部标识")
            continue
        if pid in ids:
            problems.append(f"有两家的内部标识重复了：{pid}")
            continue
        # 标识的字符集要卡死：它会出现在前端 DOM 的 id 和内联 onclick 里，
        # 放任引号/括号进来就是一个注入面（别只靠前端转义兜着）。
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", pid):
            problems.append(f"内部标识只能用字母、数字和 . _ -（现在：{pid[:20]}）")
            continue
        ids.add(pid)
        by_id[pid] = p
        if pid not in used:
            continue                        # 没用上的：允许是半成品，不挑内容
        # —— 内容检查：只挑"在用"的家 ——
        if not str(p.get("label") or "").strip():
            problems.append(f"{_name(p)} 未填写名称")
        if not _scheme_ok(p.get("base_url")):
            problems.append(f"{_name(p)} 的「接口地址」需以 http:// 或 https:// 开头")
        if not str(p.get("text_model") or "").strip():
            problems.append(f"{_name(p)} 未填写「通用模型」")

    for slot, label in SLOT_LABELS.items():
        chain = (cfg.get("slots") or {}).get(slot) or []
        if not isinstance(chain, list):
            problems.append(f"「{label}」的配置格式不正确")
            continue
        if len(chain) > MAX_CHAIN:
            problems.append(f"「{label}」的备用顺序过长（最多 {MAX_CHAIN} 个）")
        for pid in chain:
            if pid not in by_id:
                problems.append(f"「{label}」中包含已不存在的来源：{pid}")
            elif slot == "vision" and not str(
                    by_id[pid].get("vision_model") or "").strip():
                # 看图那一行里放纯文字模型 = 识别时整卷报 400，用户根本猜不到原因。
                # 在保存这关就拦住，别让它变成运行时的天书报错。
                problems.append(
                    f"{_name(by_id[pid])} 未填写「识图模型」，不能用于「{label}」")
    return problems


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
    _KEEP = ("id", "label", "base_url", "text_model", "vision_model",
             "route_model", "key_env", "api_key")
    cfg["providers"] = [{k: p[k] for k in _KEEP if k in p}
                        for p in (cfg.get("providers") or []) if isinstance(p, dict)]

    # —— key 三态合并：拿库里上一版对照 ——
    prev = db.model_config_load(user_id) or {}
    prev_keys = {p.get("id"): (p.get("api_key") or "")
                 for p in (prev.get("providers") or []) if isinstance(p, dict)}
    for p in cfg.get("providers") or []:
        if not isinstance(p, dict):
            continue
        if "api_key" not in p or p.get("api_key") is None:
            p["api_key"] = prev_keys.get(p.get("id"), "")     # 不改
        else:
            p["api_key"] = str(p.get("api_key") or "").strip()  # ""=清空，非空=覆盖
        p["builtin"] = p.get("id") in {b["id"] for b in BUILTIN_PROVIDERS}

    # —— 服务端级联去悬空 id：删 provider 时同步从所有链里剔除 ——
    # （前端也会做一次，但这里必须有：手改请求能塞进来悬空 id）
    ids = {p.get("id") for p in cfg.get("providers") or [] if isinstance(p, dict)}
    for slot in list((cfg.get("slots") or {}).keys()):
        if slot not in SLOT_LABELS:
            cfg["slots"].pop(slot, None)         # 不认识的槽位直接丢弃
    for slot in SLOT_LABELS:
        chain = (cfg.get("slots") or {}).get(slot) or []
        cfg.setdefault("slots", {})[slot] = [pid for pid in chain if pid in ids]

    problems = _validate(cfg)
    if problems:
        raise ValueError("；".join(problems))

    cfg.pop("warn", None)                        # warn 只是给前端看的临时字段
    db.model_config_save(user_id, cfg)
    return cfg


def delete_config(user_id: str) -> None:
    """恢复默认：把库里那份删掉（下次 load_config 又回到内置默认）。"""
    db.model_config_delete(user_id)


# ── 解析成"可建连的条目" ──────────────────────────────────────────────

def resolve(user_id: str, slot: str) -> list[dict]:
    """把某用户某个槽位解析成**有序条目**（降级顺序），调用方按顺序去建客户端。

    返回 [{"id","label","base_url","api_key","key_env","model","slot"}, ...]
    —— 列表为空 = 这个槽位没有任何能用的条目（调用方自己决定怎么报错）。

    刻意**不在这里过滤"没配 key"的条目**：让 Model 构造时报错、然后按链切下一家，
    这样"没 key 自动换一家"和"网络失败自动换一家"走的是同一套机制，报错也更具体
    （"glm 缺 key → 试 qwen"）。也刻意**不加缓存**——用户点保存后，下一轮对话就要生效。

    ★ .env 兜底只给站长（db.is_owner；空 user_id 的 CLI/后台路径也算站长）。
      非站长解析出来的 api_key 是空串 → Model 抛出"去界面里填密钥"的人话提示。
      allow_env 也一并塞进每条 entry，好让 Model 知道该报哪种文案（它自己查不到用户）。
    """
    cfg = load_config(user_id)
    allow_env = db.is_owner(user_id)
    out: list[dict] = []
    seen: set[str] = set()
    for pid in (cfg.get("slots") or {}).get(slot) or []:
        if pid in seen:
            continue
        seen.add(pid)
        p = _by_id(cfg, pid)
        if p is None:
            continue
        e = _entry(p, slot, allow_env)
        if e is None:                # 这家不参与该槽位（例如没填 vision 模型名）
            continue
        out.append(e)
    return out


def _entry(p: dict, slot: str, allow_env: bool) -> dict | None:
    """把一家 provider 解析成一条"可建连的 entry"；这家在该槽位没有模型名就返回 None。

    ★ 形状只有这一处定义（resolve 的每条、resolve_one 的单条都走这里）：
      调用方拿到的 entry 长什么样必须一致，否则"链里那家"和"单独指定那家"
      行为会悄悄不同。
    """
    model = _model_for(p, slot)
    if not model:
        return None
    pid = p.get("id") or ""
    return {"id": pid,
            "label": p.get("label") or pid,
            "base_url": (p.get("base_url") or "").strip(),
            "api_key": _api_key(p, allow_env),
            "key_env": p.get("key_env") or "",
            "allow_env": allow_env,          # 给 Model 选报错文案用
            "model": model,
            "slot": slot}


def resolve_one(user_id: str, provider_id: str, slot: str) -> list[dict]:
    """把**指定的一家**解析成单条 entry（"换个模型重跑"用）；不适用就返回 []。

    与 resolve 的区别：只看这一家、不排队、**不管它在不在槽位的降级链里**
    （用户可能就是想单独拿某家跑一遍对比）。返回形状与 resolve 的每条一致。
    """
    cfg = load_config(user_id)
    p = _by_id(cfg, provider_id)
    if p is None:
        return []
    e = _entry(p, slot, db.is_owner(user_id))
    return [e] if e else []


def vision_providers(user_id: str) -> list[dict]:
    """**所有配了识图模型的家**（不管它在不在「识图模型」槽的降级链里）—— 报菜单用。

    为什么与 resolve 不同：resolve 只给**链里**的家（那才是"默认识别会用到的"），
    而用户要换模型重跑时，应该能看到**任何一家能看图的**（哪怕没排进链）。
    返回 [{"id","label","model","in_chain","order"}]，顺序：链里的按链序在前，
    其余按配置顺序在后。**不含 key**（只用来报菜单，不建客户端）。
    """
    cfg = load_config(user_id)
    chain = [pid for pid in ((cfg.get("slots") or {}).get("vision") or [])]
    out: list[dict] = []
    for p in cfg.get("providers") or []:
        model = _model_for(p, "vision")
        if not model:                # 没填识图模型名 → 这家看不了图，不进菜单
            continue
        pid = p.get("id") or ""
        out.append({"id": pid, "label": p.get("label") or pid,
                    "model": model, "in_chain": pid in chain,
                    "order": chain.index(pid) if pid in chain else len(chain) + 1})
    out.sort(key=lambda d: d["order"])
    return out


def slots_view() -> list[dict]:
    """给前端渲染用的槽位清单（顺序固定，每条带一句人话说明）。"""
    return [{"id": s, "label": SLOT_LABELS[s], "hint": SLOT_HINTS[s]}
            for s in SLOT_LABELS]


def presets() -> list[dict]:
    """「＋ 添加模型来源」的模板清单（内置三家不在里面——它们本来就在列表里）。"""
    return [dict(p) for p in EXTRA_PRESETS]


def probe_entry(user_id: str, provider_id: str,
                slot: str = "") -> tuple[str, str, str]:
    """给「试一下能不能用」端点用：从**已保存的**配置里取 (base_url, api_key, model)。

    刻意只认已保存的 provider id、**不接受裸 URL** —— 这样"测试"这个动作对外能到达
    的范围就被收敛成"你自己配置里的那几个地址"，没法被拿来探测任意内网服务。

    slot 留空 = 自动挑一个这家有的模型名（文字优先，其次看图）—— 界面上是按
    "这一家"测的，用户不该被要求先理解槽位。
    """
    cfg = load_config(user_id)
    p = _by_id(cfg, provider_id)
    if p is None:
        raise ValueError("未找到该模型来源，请先保存配置")
    order = ([slot] if slot else []) + ["chat", "vision", "route"]
    for s in order:
        m = _model_for(p, s)
        if m:
            # 非站长在这里同样拿不到 .env 的 key —— 于是"测试连接"测的就是他真正
            # 会用的那把；否则会出现"测试通过、一说话就报缺 key"的假绿灯。
            return (p.get("base_url") or "").strip(), _api_key(p, db.is_owner(user_id)), m
    raise ValueError("该来源尚未填写模型名，请填写后重试")
