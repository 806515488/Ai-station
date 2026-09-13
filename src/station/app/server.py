"""station Web 宿主（单用户）—— FastAPI。

UI 在 static/index.html（v2：左历史会话 / 中统一对话 / 右上“我的产物”抽屉）。

★ 端点清单**不在这里重复维护** —— 抄一份到这儿就是"同一份清单两处手写"，迟早漂移
  （09-13 发现这份抄件已经漂了：还列着早已删掉的 `GET /api/capabilities`、
  `POST /api/jobs`、`GET /api/jobs/{id}/events`，而 auth / modelconfig /
  archive.reconcile 一个都没进去）。权威清单在 `docs/conventions.md`「站点接口」。

启动：`station-web`（已 `-e` 安装）或 `PYTHONPATH=src <conda-py> -m station.app.server`
      → 默认 http://127.0.0.1:8001；地址/端口/TLS 走环境变量，见下面的 _serve_options()。
调试无 key：STATION_FAKE=1 用离线 FakeModel 验证 harness 循环。

新手视角（Java 朋友版）：FastAPI ≈ Spring Boot。`@app.get("/x")` 就像一个
注解告诉框架“这个函数负责响应 GET /x”。整个文件 = “Controller 层”，
职责只有三件：认路(路由) → 把请求体转成内部对象 → 调 service/core 干正事。
真正的脑子在 core/agent.py，任务在 jobs/manager.py，业务全在 skills/。
"""
from __future__ import annotations        # 允许注解写 `str | None`（Python 3.10+），相当于类型注解开关

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path                   # Path：路径对象（比字符串拼接路径更安全、可跨平台）

# 保证 `python -m station.app.server` 时能 import（把仓库 src 放进 Python 的“找包路径”）。
# 类比：Java 靠 classpath 找类，Python 靠 sys.path 找模块 —— 我们的包 station/ 在 src/ 下。
_HERE = Path(__file__).resolve()           # 本文件绝对路径
_SRC = _HERE.parents[2]                    # …/station/app → 往上 2 级 = …/src（parents[0]=app,[1]=station,[2]=src）
for _p in (_SRC, _HERE.parents[3]):        # 把 src 和仓库根都放上搜索路径（仓库根也有用）
    if str(_p) not in sys.path:            # 防重复添加
        sys.path.insert(0, str(_p))

# 第三方库 / 本项目其它模块。装饰器/类型都来自这些 import。
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # FastAPI + 文件上传 + 表单 + 404 + 请求对象(读cookie)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # 文件响应 / JSON / 流式响应
from fastapi.staticfiles import StaticFiles          # 把 static/ 目录直接挂成静态资源（HTML/JS/CSS）
from pydantic import BaseModel                       # 请求体模型：像 Java 的 record/DTO，自动校验字段

from station import config, db
from station.core import guards
from station.core.agent import run_agent, run_tool  # 大脑循环 + 执行单个工具
from station.core.context import Context
from station.core.router import l1_route, l2_route, log_route  # 三层意图漏斗 + 路由日志
from station.core.session import Thread
from station.jobs.manager import get_manager
from station.skills.registry import get_registry
from station.app.unified import (build_generic_agent, build_skill_catalog,
                                 get_skill)
from station.app.uploads import purge_old, store_upload  # 客户端照片上传落盘


# ── 登录态（09-06 起多用户：cookie 里存 uid，每个请求回查 users 表）────────
def current_user(request: Request) -> dict | None:
    """从请求 cookie 解出当前登录用户；没登录/用户已被删 → None。

    cookie 里只放 uid（不放密码类信息）；每请求回查 DB——删号/重建立即生效。
    """
    uid = request.cookies.get("uid") or ""
    return db.get_user(uid) if uid else None


def require_user(request: Request) -> dict:
    """必须已登录才能用的接口用这个：没登录直接 401（前端据此弹登录框）。"""
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "未登录")
    return u



def _make_model(skill, user_id: str = ""):
    """按“开关”决定给这次对话用真模型还是假模型(FakeModel)。

    读环境变量 STATION_FAKE：==1 → 用离线假模型（不烧钱、看流程用）；
    否则 → 按**当前用户**「模型配置」里的「对话主模型」降级链去建（见 station/modelcfg.py）：
    链首是他选的主通道，后面是兜底顺序，主家挂了自动换下一家。
    """
    from station.core import model as _m      # 函数内 import：用到才加载，省启动时间
    fake = os.environ.get("STATION_FAKE", "0") == "1"
    if fake:
        return _m.FakeModel(skill.ns)
    from station import modelcfg
    return _m.build(skill.model, entries=modelcfg.resolve(user_id, "chat"))


# 创建 FastAPI 应用实例（“整个 web 服务的根对象”），好比 new 一个 SpringApplication
app = FastAPI(title="station · 个人 AI 工作站")



class AuthIn(BaseModel):
    """注册/登录请求体。"""
    name: str
    password: str


@app.post("/api/auth/register")
def auth_register(body: AuthIn):
    """注册（首个注册的用户即站长——本地站不做角色区分，仅提示）。"""
    try:
        u = db.create_user(body.name, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({"user": u, "first": db.user_count() == 1})
    resp.set_cookie("uid", u["id"], httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)   # 30 天免登录
    return resp


@app.post("/api/auth/login")
def auth_login(body: AuthIn):
    """登录：对了种 cookie；错了 401（不区分没此人和密码错，防探测）。"""
    u = db.check_login(body.name, body.password)
    if u is None:
        raise HTTPException(401, "用户名或密码不对")
    resp = JSONResponse({"user": u})
    resp.set_cookie("uid", u["id"], httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    """登出：清 cookie。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("uid")
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    """前端刷新后第一步就调它：登录了返回用户，没登录 401 → 弹登录视图。"""
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "未登录")
    return {"user": u}

def _ctx(thread, agent) -> Context:
    """造本次运行的 Context：按命中的技能给独立数据目录（工具不是全局的）。"""
    return Context(thread=thread, skill_id=agent.id,
                   data_dir=config.sub("skills", agent.id),
                   allowed_keys=list(getattr(agent, "keys", None) or []),
                   auto_approve=config.AUTO_APPROVE,
                   user_id=getattr(thread, "user_id", "") or "")


def _skill_for_tool(tool_name: str):
    """由“完整工具名”反查它所属技能（如 archive.export → archive）。

    `station.` 开头的是**全局工具**（见 station/tools/），不属于任何技能 ——
    它落在通用聊天 agent 上（那个 agent 的 tools 就是全局工具清单）。
    ★ 这条不能不写：返回 None 的话会退回一个没有该工具的 agent，用户回了"允许"
      却什么都不会发生（静默失败，最难查的那种）。现在 4 个全局工具**都不是**
      approve 类，但分支保留着 —— 哪天加回一个危险工具，这条不能少。
    """
    ns = (tool_name or "").split(".", 1)[0]
    if not ns:
        return None
    if ns == "station":
        return build_generic_agent()
    for s in get_registry().list():
        if s.ns == ns:
            return s
    return None


def _tool_label(tool_name: str) -> str:
    """工具全名 → **给人看的一句话**（工具在 Tool.label 里声明的）；查不到给空串。

    历史回放需要它：thread 里只存了工具名，而"该怎么跟用户说"是**工具自己**的事
    （宿主不认识业务），所以回注册表问一遍。空串时前端退回显示工具叶子名。
    """
    leaf = str(tool_name).split(".", 1)[-1]
    # 全局工具先查（它们不在技能注册表里）
    from station.tools import GLOBAL_TOOLS
    for t in GLOBAL_TOOLS:
        if t.name == tool_name:
            return getattr(t, "label", "") or ""
    s = _skill_for_tool(tool_name)
    if s is None:
        return ""
    for t in (getattr(s, "tools", None) or []):
        if t.leaf() == leaf:
            return getattr(t, "label", "") or ""
    return ""


# ── 模型配置（每人一份：选哪家 provider、填 key、给四个槽位排降级链）─────────
# 为什么有这一组端点：以前"用哪家模型"写死在代码常量里、key 只能去 src/.env 改；
# 现在用户可以在界面「模型配置」弹窗里自己配。整包配置的校验/掩码/解析全在
# station/modelcfg.py —— 这里只做 HTTP 这层（取登录态 → 调它 → 转成响应）。

class ModelConfigIn(BaseModel):
    """PUT /api/modelconfig 的请求体：整包 provider 列表 + 四个槽位的降级链。"""
    providers: list = []
    slots: dict = {}


class ProviderTestIn(BaseModel):
    """POST /api/modelconfig/test 的请求体。slot 留空=自动挑一个这家有的模型名。"""
    provider_id: str
    slot: str = ""


def _cfg_payload(user: dict, cfg: dict | None = None) -> dict:
    """组装给前端的配置响应。

    reveal=True：带上**本人**的密钥明文，界面里默认显示成 ****、点「显示」才明文
    （用户要求"保存后能回显、能核对"）。只走登录态、只回当前用户自己的那份 ——
    所以这个响应里绝不能加缓存，也别忘了它是 require_user 后面的。
    """
    from station import modelcfg
    cfg = cfg if cfg is not None else modelcfg.load_config(user["id"])
    # .env 里的密钥只兜底给站长（最早注册的账号）。非站长这里就当作"什么都没有"，
    # 界面显示黄点 +「还没填密钥」，逼他自带密钥 —— 站是站长掏钱开的，不该被白用。
    is_owner = db.is_owner(user["id"])
    return {"config": modelcfg.mask_config(cfg, reveal=True, allow_env=is_owner),
            "slots": modelcfg.slots_view(),
            "presets": modelcfg.presets(),      # 「＋ 添加模型来源」的一键模板
            # 界面据此选引导文案：站长"不改也能用"，别人"先去填密钥"。
            "env_keys": is_owner,
            # 离线假模型模式下整条配置链路都被绕过（_make_model 直接返回 FakeModel），
            # 告诉前端一声，免得用户配了半天以为没生效。
            "fake": os.environ.get("STATION_FAKE", "0") == "1"}


@app.get("/api/modelconfig")
def get_model_config(request: Request):
    """读当前用户的模型配置（key 只回"有没有配/从哪来"，绝不回明文）。"""
    return _cfg_payload(require_user(request))


@app.put("/api/modelconfig")
def put_model_config(body: ModelConfigIn, request: Request):
    """保存整包配置。校验不过回 400 + **人话**问题清单（不抛 pydantic 的 422）。"""
    u = require_user(request)
    from station import modelcfg
    try:
        cfg = modelcfg.save_config(u["id"], {"providers": body.providers,
                                             "slots": body.slots})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _cfg_payload(u, cfg)


@app.delete("/api/modelconfig")
def delete_model_config(request: Request):
    """恢复默认：删掉本人这份配置（下次读就又回到内置默认）。"""
    u = require_user(request)
    from station import modelcfg
    modelcfg.delete_config(u["id"])
    return _cfg_payload(u)


# 「测试连接」限流表：user_id → 最近一分钟的测试时间戳
_TEST_HITS: dict[str, list] = {}
_BLOCKED_HOSTS = {"169.254.169.254", "100.100.100.200", "metadata.google.internal"}

# 异常类型名 → 人话。界面上要显示给不写代码的人看，"OpenAIConnectionError"
# 对他等于没说。这里只做**我们自己写的**映射，不回对端返回的任何字符串。
_ERR_HINTS = (
    ("Authentication", "密钥不对 —— 服务端拒绝了这个 key，回去核对一下是不是粘错了/多粘了空格"),
    ("PermissionDenied", "这个 key 没权限用这个模型"),
    ("NotFound", "地址或模型名不对 —— 服务端说找不到。检查「接口地址」和「模型名」"),
    ("RateLimit", "被限流了 —— 这个 key 请求太频繁，缓一会儿再试"),
    ("Timeout", "超时 —— 地址连得通但没响应，可能是网络慢或服务在忙"),
    ("Connect", "连不上 —— 接口地址写错了，或者本机服务（如 Ollama）没启动"),
    ("BadRequest", "请求被拒 —— 多半是「模型名」填错了，核对一下"),
)


def _err_hint(name: str) -> str:
    """把异常类型名翻成一句人话（翻不出就返回空串，界面只显示类型名）。"""
    for key, hint in _ERR_HINTS:
        if key in (name or ""):
            return hint
    return ""


def _test_allowed(user_id: str, per_minute: int = 10) -> bool:
    """同一用户每分钟最多测几次（防有人拿它当免费压测/烧钱工具）。"""
    now = time.time()
    hits = [t for t in _TEST_HITS.get(user_id, []) if now - t < 60]
    if len(hits) >= per_minute:
        _TEST_HITS[user_id] = hits
        return False
    hits.append(now)
    _TEST_HITS[user_id] = hits
    return True


@app.post("/api/modelconfig/test")
def test_model_config(body: ProviderTestIn, request: Request):
    """试连一个 provider，只回"通没通 + 耗时"。四条防护（删之前先想清楚）：

    1) **只接受已保存的 provider id**（见 modelcfg.probe_entry），不接裸 URL ——
       用户想测就先保存。这一条直接把"拿它探测任意地址"的面消掉了。
    2) scheme 只许 http/https；云元数据地址直接拒（169.254.x 等）。
       但 **localhost 必须放行** —— 本地 ollama 就是 http://localhost:11434/v1。
    3) max_tokens=1 + 8 秒超时：一次测试几分钱，也不会让对端写小作文。
    4) **只回异常类型名，不回 message** —— message 里可能含对端返回的片段；
       把 base_url 指向内网服务、再靠报错把内容读出来是经典的探测手法。
    """
    u = require_user(request)
    if not _test_allowed(u["id"]):
        raise HTTPException(429, "测试过于频繁，请稍后重试")
    from station import modelcfg
    try:
        url, key, model = modelcfg.probe_entry(u["id"], body.provider_id, body.slot)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "base_url 必须是 http:// 或 https:// 地址")
    if host in _BLOCKED_HOSTS or host.startswith("169.254."):
        raise HTTPException(400, "该地址不允许测试")
    if not key:
        return {"ok": False, "error": "NoKey", "ms": 0,
                "hint": "尚未配置密钥，请填写后重试（本地 Ollama 可填任意非空值）"}
    t0 = time.perf_counter()
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=model, api_key=key, base_url=url or None,
                         temperature=0, timeout=8, max_retries=0, max_tokens=1)
        llm.invoke([{"role": "user", "content": "hi"}])
        return {"ok": True, "ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:                        # noqa：连不上是常见情况，不算服务端错误
        name = type(e).__name__
        return {"ok": False, "error": name, "hint": _err_hint(name),
                "ms": int((time.perf_counter() - t0) * 1000)}


# ── 客户端照片上传（archive 的 photos_dir 用；用户在自己电脑选照片）────
@app.post("/api/photos")
async def upload_photos(files: list[UploadFile] = File(...),
                        label: str = Form("")):
    """接收多张照片 → 落到 data/station/uploads/<人名>-<uid>/，返回该目录。

    body: multipart/form-data；files（可多个）为照片，label（可选）是"这一卷是谁的"
    （干部姓名或所选文件夹名）。返回 {"dir","count","label"}；dir 作 job 的 photos_dir。
    每次新上传先把旧上传目录清掉（单用户顺序流程，旧的已被上一次 job 消费）。
    """
    items: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read()
        if data:
            items.append((f.filename or "", data))
    if not items:
        raise HTTPException(400, "没有收到照片")   # 一张都没有 → 直接告诉前端
    root = config.sub("uploads")                 # data/station/uploads
    purge_old(root)                              # 只留最近这一批
    d, n = store_upload(root, items, label.strip())
    return {"dir": d, "count": n, "label": label.strip()}


# ── 会话（09-06 起按用户隔离）─────────────────────────────────────
@app.get("/api/sessions")
def sessions(request: Request, skill: str = ""):
    """列出当前登录用户的历史会话摘要（可选按技能过滤 ?skill=xxx）。"""
    u = require_user(request)
    return {"sessions": db.thread_list(u["id"], skill or None)}


@app.get("/api/sessions/{tid}")
def session_detail(tid: str, request: Request):
    """看某个会话的完整消息（只能看自己的）。"""
    u = require_user(request)
    t = Thread.load(tid)
    if t is None or getattr(t, "user_id", "") != u["id"]:
        raise HTTPException(404, "会话不存在")   # 抛 404 ≈ Java 里抛特定异常→框架转成 HTTP 状态码
    meta = t.meta or {}
    view = _replay_items(t.msgs, meta.get("cards") or [])
    # ★ pending 要带上 **label**：刷新页面时前端要重画那张批准卡，而卡片上显示的是
    #   人话标签不是内部工具名 —— 不带的话重画出来又是 `archive.xxx`（或者干脆画不出来，
    #   用户会卡在"agent 在等允许、屏幕上什么都没有"）。
    pend = t.pending
    if pend:
        pend = {**pend, "label": _tool_label(pend.get("tool") or "")}
    return {"id": t.id, "skill_id": t.skill_id, "msgs": view,
            "pending": pend,
            "job_id": meta.get("job_id") or "",
            "cards": meta.get("cards") or []}


def _replay_items(msgs: list, cards: list) -> list:
    """按“真实发生顺序”把文本/工具事件/卡片交错成回放列表。

    原始 thread.msgs 是给模型看的（user/assistant/tool 消息），而页面还出现过
    工具调用行和渲染卡片。为了让刷新后的顺序跟当时对话一致，这里按消息位置
    合成 UI 顺序：assistant 工具调用 → “调用工具”行，卡片插在它所在工具结果之后。
    """
    items: list = []
    cards = sorted(cards, key=lambda c: int(c.get("idx") or 0))
    ci = 0
    for pos, m in enumerate(msgs):
        role = m.get("role") or ""
        content = m.get("content") or ""
        if role == "assistant" and m.get("tool_calls"):
            # 模型“边说话边调工具”时，那句前言也要回放出来（实时显示过，刷新后不能丢）
            if content.strip():
                items.append({"role": "assistant", "content": content})
            calls = [c for c in m["tool_calls"] if c.get("name")]
            if calls:
                # 只给**结构化数据**（名字/label/参数），"怎么显示成人话"交给前端 ——
                # 实时那条路（core/agent 的 EV_TOOL）发的也是这个形状，两边共用同一个
                # 渲染函数。以前这里和前端各拼一份"→ 调用工具：…"，改一处忘一处。
                items.append({"role": "tool", "content": "", "tool_call": {
                    "names": [str(c.get("name")) for c in calls],
                    "labels": [_tool_label(str(c.get("name"))) for c in calls],
                    "args": [c.get("arguments") or {} for c in calls]}})
        elif role in ("user", "assistant") and content:
            items.append({"role": role, "content": content})
        # 卡片 idx 记的是“该工具结果出现的位置”，处理完这一条后再插入
        while ci < len(cards) and int(cards[ci].get("idx") or 0) <= pos:
            card = cards[ci].get("card")
            if card:
                items.append({"role": "render", "content": "", "card": card})
            ci += 1
    while ci < len(cards):              # 兜底：异常/旧数据 idx 超出范围也补在末尾
        card = cards[ci].get("card")
        if card:
            items.append({"role": "render", "content": "", "card": card})
        ci += 1
    return items


# ── 统一主对话（SSE）───────────────────────────────────────────
class ChatIn(BaseModel):
    """POST /api/chat 的请求体模型（像 Java record）：声明要哪些字段、类型。

    pydantic 会自动把前端传来的 JSON 校验/转换成这个对象，缺字段直接报 422。
    """
    message: str                    # 用户说的话
    thread_id: str | None = None    # 续聊：带旧会话 id；None=新开一局
    skill: str | None = None        # 兼容旧客户端：统一入口下不再按它分技能


def _sse(d: dict) -> str:
    """把一个 dict 包成一条 SSE 消息：`data: {json}\n\n`。前端按这个格式切。"""
    return "data: " + json.dumps(d, ensure_ascii=False) + "\n\n"


def _remember_card(thread, card: dict) -> None:
    """把一次渲染卡片写进会话 meta，刷新/回放时能重新画出来。

    卡片只给人看，不进 model 消息历史；这里以事件快照保存，
    最多留最近 30 张，避免聊天久了 meta 无限膨胀。
    """
    if not isinstance(card, dict):
        return
    cards = thread.meta.setdefault("cards", [])
    cards.append({"idx": len(thread.msgs), "card": card})
    if len(cards) > 30:
        del cards[:-30]


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    """统一对话入口。返回一个 SSE 流（不是一次性 JSON），边跑边推。

    async def + 返回 StreamingResponse：请求进来立刻“占住”这个连接，
    后台 gen() 生成器一条一条往浏览器推数据（打字机效果、进度、批准提示都靠它）。
    """
    # gen() 是嵌套【生成器】：调用 chat() 只返回一个 StreamingResponse，
    # 只有当框架真正去读这个流时，gen() 里的代码才会逐行执行到每个 yield。
    def gen():
        t_start = time.perf_counter()          # 分段计时用（见末尾的 timing 日志）
        user = None
        try:
            user = require_user(request)      # 未登录 → 401（这里转成 SSE 错误事件）
        except HTTPException as e:
            yield _sse({"type": "error", "text": e.detail})
            return
        # ① 只读“技能目录”（id/name/desc），此时不把任何技能的工具给模型。
        catalog = build_skill_catalog()
        available = {c.id for c in catalog}
        generic = build_generic_agent()

        # ② 会话（Thread）：有 thread_id 就读旧会话续聊；没有就新建。
        #    只能续自己的会话（user_id 归属校验）；新建的立即打上当前用户。
        thread = (Thread.load(body.thread_id) if body.thread_id else None)
        if thread is not None and getattr(thread, "user_id", "") != user["id"]:
            yield _sse({"type": "error", "text": "会话不存在（不属于当前用户）"})
            return
        if thread is None:
            thread = Thread(skill_id="station")    # 新 Thread（统一主对话）
            thread.user_id = user["id"]
        else:
            thread.skill_id = "station"            # 旧模块会话也并入统一主对话
        thread.add_user(body.message)              # 把用户这句话写进历史
        yield _sse({"type": "meta", "thread_id": thread.id})   # 先告诉前端“这是哪个会话”

        agent = None                               # 本次真正执行的 agent（命中后才定）
        ctx = None

        # ③ 意图入口第一段（L1 规则）：上一轮危险工具挂起(pending)，
        #    这一轮用户的话就是对“是否允许”的回答 → 在这里补执行
        pend = thread.pending                      # thread.pending 是只读属性（见 session.py）
        if pend:
            ans = guards.classify_answer(body.message)   # 判词器：“允许/拒绝/无关”
            if ans == "":                                # 用户没回答 → 提示他明确回复
                thread.save()
                yield _sse({"type": "delta", "text": "上一操作仍在等待确认，请回复：允许 / 拒绝。\n"})
                yield _sse({"type": "done"})
                return
            # 批准挂起里记的是“完整工具名”，按 ns 反查它属于哪个技能，
            # 只加载那个技能的 tools 来补执行，而不是拿全局工具表去找。
            agent = _skill_for_tool(pend.get("tool") or "") or generic
            ctx = _ctx(thread, agent)
            ctx.system = (agent.system or "").strip()
            tid = "a" + uuid.uuid4().hex[:10]      # 造一个假的工具调用 id（历史配对要用）
            tool = next((t for t in getattr(agent, "tools", [])
                         if t.name == pend.get("tool")), None)
            if tool is None:
                out = f"工具 {pend['tool']} 已不存在"
            elif ans == "allow":
                out = run_tool(ctx, tool, pend.get("arguments") or {})   # 真的执行它
            else:
                out = "用户拒绝了该操作。"
            # 工具若发了渲染卡片（EV_RENDER，如出件后的文件卡片）→ 推给前端
            for ev in list(ctx.events):
                if ev.type == "render":
                    _remember_card(thread, ev.data.get("card"))
                    yield ev.to_sse()
                ctx.events.remove(ev)
            # 协议要求“工具结果消息”前面必须有一条“assistant 决定调这个工具”的消息配对：
            thread.add({"role": "assistant", "content": "", "tool_calls": [
                {"id": tid, "name": pend["tool"], "arguments": pend.get("arguments") or {}}]})
            thread.add({"role": "tool", "tool_call_id": tid, "name": pend["tool"],
                        "content": out})
            thread.clear_pending()                 # 处理完了就清掉挂起标记
            thread.save()                          # 存盘（中断/重启后还能续）

        # ③½ 三层漏斗只在“没有待批准补执行”时跑：先 L1 关键词，再 L2 小模型。
        #     两层都只返回“命中哪个 skill”，不返回工具名、不展开工具表。
        route = None
        diag: dict = {}                            # L2 的判定细节（写路由日志用）
        route_ms = 0.0
        if agent is None:
            t_route = time.perf_counter()
            route = l1_route(body.message, available)
            if route is None:
                route = l2_route(body.message, catalog, diag, user["id"])
            route_ms = round((time.perf_counter() - t_route) * 1000, 1)

        # 确定“这次真正用哪个 agent”：
        #   路由命中技能 → 只加载该技能（skill.tools 才进模型上下文）；
        #   L2 判定纯闲聊 → 通用聊天，不绑任何业务工具；
        #   完全没命中但有活跃技能 → 续用活跃技能（同一会话里的“继续”）。
        if agent is None:
            sid = route.skill_id if route else ""
            if sid and sid in available:
                agent = get_skill(sid) or generic
            elif route is not None and route.skill_id == "":
                agent = generic                    # L2 明说是 chat，不进业务技能
            else:
                active = (thread.meta or {}).get("active_skill", "")
                agent = get_skill(active) if active in available else generic
        if ctx is None:
            ctx = _ctx(thread, agent)
            ctx.system = (agent.system or "").strip()

        # 记住/清除本会话的活跃技能（后续“继续/再改一下”能在同一技能里走）
        if agent.id in available:
            thread.meta["active_skill"] = agent.id
        else:
            thread.meta.pop("active_skill", None)

        # 记一条路由日志（data/station/route_log.jsonl）：跑两周后据此校准 L2 阈值
        # —— ROUTE_THRESHOLD=0.8 目前只是个没实据的默认值。
        log_route({"text": body.message[:200], "agent": agent.id,
                   "level": route.level if route else ("pending" if pend else "fallback"),
                   "skill": route.skill_id if route else "",
                   "route_ms": route_ms, "l2": diag or None})

        # ④ 路由只选了 skill，到这里才真正进入该技能 agent：
        #    由该技能自己的 function calling（或未来技能内快道）决定调用哪个工具。
        try:
            model = _make_model(agent, user["id"])  # 真模型（按本人配置的链）or 假模型
        except Exception as e:                     # noqa
            # 建模型就失败也要留痕：否则日志里只剩"路由成功"、看不到这次压根没跑起来
            log_route({"kind": "chat", "text": body.message[:60], "agent": agent.id,
                       "route_ms": route_ms, "first_delta_ms": None,
                       "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                       "error": f"{type(e).__name__}: {e}"[:200]})
            yield _sse({"type": "error", "text": f"模型不可用：{e}"})
            return
        thread.save()                              # active_skill 先落盘
        # 告诉前端"路由结束、开始生成"了 —— 它在两段等待之间切换文案用（路由那段
        # 可能有好几秒，什么都不显示用户会以为卡死）。纯提示，不影响任何逻辑。
        yield _sse({"type": "stage", "text": "正在思考…"})
        first_delta_ms = None                      # 从收到请求到**吐出第一个字**的耗时
        for ev in run_agent(ctx, thread, agent, model):
            if ev.type == "delta" and first_delta_ms is None:
                first_delta_ms = round((time.perf_counter() - t_start) * 1000, 1)
            if ev.type == "render":
                _remember_card(thread, ev.data.get("card"))
                thread.save()                      # 卡片要立刻落盘（刷新回放靠它的位置锚）
            elif ev.type != "delta":
                # 正文 delta 一次对话会推成百上千条，每条都落盘 = 每条都全量序列化+写库；
                # 而这段时间历史根本没变（答复是在本轮末尾才 add 的），所以跳过。
                thread.save()
            yield ev.to_sse()                      # 把每个 Event 转成 SSE 推给前端
        # 分段耗时落日志：光记 route_ms 分不清"路由慢"还是"模型慢"——
        # 用户报"等十几秒才吐字"时，这三个数一眼就能定位（见 docs/conventions.md）。
        log_route({"kind": "chat", "text": body.message[:60],
                   "agent": agent.id, "route_ms": route_ms,
                   "first_delta_ms": first_delta_ms,
                   "total_ms": round((time.perf_counter() - t_start) * 1000, 1)})

    # StreamingResponse(生成器) 是 FastAPI 的“边生产边发送”响应体
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── pipeline 型：异步任务 ──────────────────────────────────────
# 起任务的入口只在**技能工具内部**（如 archive.recognize 调 manager.submit），
# 不走 HTTP —— 对话式改造后前端不再直接提交任务，POST /api/jobs 已删（09-11）。


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    """前端轮询：查任务当前状态/进度/产物（status、progress、artifacts…）。"""
    u = require_user(request)
    snap = get_manager().get(job_id)
    # 归属判断只走 DB（_job_owner）：manager 里没有归属信息，只能回库查 user_id。
    if snap is None or _job_owner(job_id) != u["id"]:
        raise HTTPException(404, "任务不存在")
    return snap


def _job_owner(job_id: str) -> str:
    """查任务归属（DB 里看 user_id）；没有返回空串。"""
    from station import db as _db
    j = _db.job_load(job_id)
    return getattr(j, "user_id", "") if j else ""


# GET /api/jobs/{id}/events（SSE 推流）已删（09-11）：前端一直是**轮询**
# /api/jobs/{id}，从没有任何 EventSource 连过它。跟着变死的 manager.stream 也已删。


# ── 档案卡片资源（对话式改造：卡片里的页图/产物文件从这里取）────────────
# 设计（09-07）：分拣台 REST（/api/archive/{job}/state|op|proposal…）已删——
# 改类/并拆/换OCR/出件全部走对话里的 archive.* 工具。但卡片要显示的"页图"和
# "产物文件"仍需要 HTTP 端点（浏览器 img/a 标签只能按 URL 拿东西）。
# 鉴权：project 参数=卷的 project.json 绝对路径，归属校验查 archive_states 表。

def _owned_project(project: str, user: dict) -> None:
    """校验这份卷属于当前登录用户；不属于/不存在 → 404（防探测，不用 403）。

    ★ 走 db.archive_state_owner（内部拿 `_lock`），**别在这里直接 `db.conn().execute()`**。
      全景卡一次要拉上百张缩略图，每张都过这道校验；裸用连接会和聊天流里的
      `thread.save()` 抢同一条 sqlite 连接 → 部分缩略图拿到 401/500，
      在浏览器里就是**一片破图**（这是 09-11 报的那个 bug 的另一半）。
    """
    if db.archive_state_owner(project) != user["id"]:
        raise HTTPException(404, "档案卷不存在")


@app.post("/api/archive/reconcile")
async def archive_reconcile(file: UploadFile = File(...), project: str = Form(""),
                            request: Request = None):
    """收下"用户改过的终版目录 xlsx" → 落到 data/station/uploads/final/ → 把路径回给前端。

    ★ 为什么不复用 `/api/photos`：① 那里有个 `purge_old` 会**清空上一次的照片目录** ——
      手上正在整理的那卷照片可能就在里面，传个 xlsx 把照片删了是灾难；② 那里没有
      `require_user`，而这份表要按卷做归属校验；③ 生命周期与落盘目录都不同。
    真正干活的是 `archive.service.learn.reconcile`（由工具 `reconcile` 在对话里调），
    这里只负责"收文件 + 鉴权 + 告诉前端路径"。
    """
    u = require_user(request)
    _owned_project(project, u)
    name = os.path.basename((file.filename or "").strip())     # ★ 去掉任何目录成分，防穿越
    if not name.lower().endswith(".xlsx"):
        raise HTTPException(400, "请上传 .xlsx 文件（Excel 目录）。")
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的。")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件太大了（超过 20MB）。")
    # ★ 放**独立**目录，别塞在 `uploads/` 底下：`POST /api/photos` 会调 `purge_old`
    #   把 `uploads/` 的每个子目录 rmtree 掉（重传照片是常规流程），塞在里面的话
    #   用户一重传照片，刚传上来的终版目录就被删了、而 agent 手上还攥着那个路径
    #   （review 抓到的真 bug：之后对账会报 FileNotFoundError）。
    d = config.sub("reconcile")
    d.mkdir(parents=True, exist_ok=True)
    dst = d / name
    dst.write_bytes(data)
    return {"path": str(dst), "name": name}


@app.get("/api/archive/page/{seq}")
def archive_page(seq: int, project: str, thumb: int = 0, request: Request = None):
    """返回某页图（page-card 的 img 用）；thumb=1 时返回压缩过的缩略图。

    产物按 (卷, 页, 宽度, 旋转角) 缓存在 data/station/thumbs/：全景卡一展开就是
    上百张，每张都现缩会拖死列表，缓存后只有第一次慢；原图被改过就重做。
    带 rotate 的页（人工确认的横躺照片）会转正后再发 —— 那些照片压根没写 EXIF
    方向，浏览器无从判断该怎么转，只能由服务端按 photos.json 里的 rotate 转。
    """
    u = require_user(request)
    _owned_project(project, u)
    from archive.service.interactive import _load_records
    rec = next((r for r in _load_records(project) if r.get("seq") == seq), None)
    src = (rec or {}).get("path") or ""
    if rec is None or not os.path.isfile(src):
        raise HTTPException(404, "页不存在")
    rot = int(rec.get("rotate") or 0)          # 人工确认的整页旋转（横躺照片靠它转正）
    if not thumb and not rot:
        return FileResponse(src)               # 不用转 → 原图直接发（最快）
    from archive import imaging
    w = 160                                    # 显示才 46px 宽，160 够清晰又够小
    key = hashlib.md5(project.encode("utf-8")).hexdigest()[:10]   # 卷路径 → 稳定短键
    # 缓存键带上旋转角：改了方向就是另一个文件，绝不会拿到旧方向的图
    cache = config.sub("thumbs") / f"{key}_{seq}_{w if thumb else 'full'}_{rot}.jpg"
    if not cache.is_file() or cache.stat().st_mtime < os.path.getmtime(src):
        try:
            cache.write_bytes(imaging.thumb_bytes(src, w, rot) if thumb
                              else imaging.rotated_jpeg(src, rot))
        except Exception:                      # noqa：转不出来就退回原图，别让前端显示破图
            return FileResponse(src)
    return FileResponse(str(cache), media_type="image/jpeg")


# GET /api/archive/file 已删（09-11）：产物下载统一走 station 文件区
# /api/files/<id>?dl=1（那条链路带正确 MIME 和下载名），archive_tools 里也明写
# 「不要用 /api/archive/file 拼」。页图仍走上面的 /api/archive/page/{seq}。
#
# GET /api/archives（我的档案库）已删（09-11）：无任何调用方，历史面板走 /api/sessions。


# ── 文件（“我的产物”抽屉数据源：按一次产出分组，不再平铺全部文件）────────
def _kind_rank(name: str) -> int:
    """给同组内文件排展示顺序：档案 4 件套固定顺序，周报 md 在 docx 前。"""
    if "人事档案目录" in (name or ""): return 0
    if "档案原件" in (name or ""):      return 1
    if "待核对清单" in (name or ""):    return 2
    if "判定报告" in (name or ""):      return 3
    if (name or "").lower().endswith(".md"):    return 4
    if (name or "").lower().endswith(".docx"):  return 5
    return 9


def _legacy_group(name: str, fid: str) -> tuple[str, str]:
    """老文件没有来源元数据时，从文件名猜“属于哪一件事”（卷/周报/任务）。"""
    n = name or ""
    for sep in ("-人事档案目录", "-档案原件", "-待核对清单", "-自动判定报告"):
        if sep in n:
            person = n.split(sep, 1)[0]
            return f"legacy-archive:{person}", f"干部档案 · {person}"
    if n.startswith("周报-"):
        week = n[3:].rsplit(".", 1)[0]
        return f"legacy-weekly:{week}", f"写周报 · {week}"
    if n.startswith("周报."):
        return "legacy-weekly:draft", "写周报"
    if n.startswith("demo-"):
        tag = n[5:].rsplit(".", 1)[0] or "demo"
        return f"legacy-demo:{tag}", f"Demo 流水线 · {tag}"
    return f"file:{fid}", (n or "产物文件")[:30]


def _group_products(files: list) -> list:
    """把文件列表按 group_key 聚合成“产物组”（组内同名只留最新一份）。"""
    groups: dict = {}
    for f in files:
        name = f.get("name") or f.get("id") or ""
        gkey, glabel = _legacy_group(name, f.get("id", ""))
        gkey = f.get("group_key") or gkey               # 新文件优先用落盘来源
        glabel = f.get("group_label") or glabel
        created = float(f.get("created") or 0)
        g = groups.setdefault(gkey, {"key": gkey, "label": glabel,
                                     "created": 0, "_files": {}})
        g["label"] = glabel
        g["created"] = max(g["created"], created)
        old = g["_files"].get(name)
        row = {"id": f["id"], "name": name, "created": created}
        if old is None or created > old["created"]:
            g["_files"][name] = row                      # 重出件：同名取最新那份
    out = []
    for g in groups.values():
        rows = sorted(g.pop("_files").values(),
                      key=lambda x: (_kind_rank(x["name"]), x["name"]))
        out.append({"key": g["key"], "label": g["label"],
                    "created": g["created"], "files": rows})
    out.sort(key=lambda g: g["created"], reverse=True)
    return out


# GET /api/station/file 已删（09-13）：它只为全局工具 `station.show` 推图存在，
# 而 show 连同 ls/read/grep/write 一起删了（理由见 station/tools/__init__.py）。
# 少一个"能按路径读仓库任意文件"的对外入口，是这次精简顺带的安全收益。


@app.get("/api/files")
def list_files(request: Request):
    """当前登录用户的产物组（我的产物抽屉数据源）。"""
    u = require_user(request)
    from station.files import store as fs       # 函数内 import，保持 server 顶层轻量
    mine = [f for f in fs.iter_files(u["id"])]  # owner 已由 store 过滤（历史文件兼容可见）
    return {"groups": _group_products(mine)}


@app.get("/api/files/{fid}")
def get_file(fid: str, dl: int = 0, request: Request = None):
    """取一个文件：dl=0 浏览器里预览(inline)，dl=1 触发下载。

    FastAPI 会按 URL 的 query ?dl=1 把值传给参数 dl。FileResponse 直接回文件流。
    """
    from station.files import store as fs
    u = require_user(request)
    meta = fs.meta(fid)
    owner = meta.get("owner") or ""
    if owner and owner != u["id"]:
        raise HTTPException(404, "文件不存在")            # 不是你的 → 404（防探测）
    p = fs.path(fid)
    if p is None:
        raise HTTPException(404, "文件不存在")
    media = meta.get("mime") or "application/octet-stream"
    if dl:
        return FileResponse(p, media_type=media,
                            filename=meta.get("name", "download"))  # 带下载名
    return FileResponse(p, media_type=media)                        # 浏览器预览


# ── 静态 UI（末尾挂载，/api 路由优先）─────────────────────────────
_STATIC = Path(__file__).parent / "static"   # static/index.html 所在目录
if _STATIC.is_dir():
    # 把“除 /api 以外”的请求交给 static 目录 → 访问 http://127.0.0.1:8001 就是 index.html
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


def _serve_options() -> dict:
    """把环境变量翻成 uvicorn.run 的参数。

    单独拎成一个函数是为了**能离线测**：起真服务没法在单测里测，但"环境变量怎么翻"
    可以（见 tests/test_deploy.py）。

    默认值与本机开发**完全一致**（127.0.0.1:8001、不开 TLS）—— 本机用法一个字没变，
    服务器那套（docker-compose.yml）才靠这几个变量把它抬成对外服务：
      STATION_HOST            默认 127.0.0.1
      STATION_PORT            默认 8001
      STATION_SSL_KEYFILE / STATION_SSL_CERTFILE   两个都给了才开 HTTPS（自签也走这里）
    """
    opts = {"host": os.environ.get("STATION_HOST", "127.0.0.1"),
            "port": int(os.environ.get("STATION_PORT", "8001"))}
    keyfile = os.environ.get("STATION_SSL_KEYFILE", "")
    certfile = os.environ.get("STATION_SSL_CERTFILE", "")
    if keyfile and certfile:
        opts.update(ssl_keyfile=keyfile, ssl_certfile=certfile)
    elif keyfile or certfile:
        # 只给一个 = 配错了。**不当场崩**（崩了就完全没服务），以 HTTP 起站并把话说出来；
        # 静默降级才是危险的 —— 用户会以为自己在用加密连接。
        missing = "STATION_SSL_CERTFILE" if keyfile else "STATION_SSL_KEYFILE"
        print(f"[warn] 缺 {missing}，证书没配全 —— 这次以 **HTTP** 起站，连接没有加密。")
    return opts


def main():
    """命令行入口：`python -m station.app.server` 或 `station-web` 都跑到这。

    uvicorn = Python 版"Tomcat"。地址/端口/TLS 见 _serve_options()。
    ★ load_env() 放在**这里**而不是 __main__ 块里：`station-web` 这个入口（Docker 用的
      就是它）不会走 __main__ 块，放那儿等于漏读 .env。
    """
    import uvicorn
    config.load_env()          # 先读 .env 把 key 放进环境变量；没有 .env 就什么都不做
    uvicorn.run("station.app.server:app", reload=False, **_serve_options())
    # reload=False：开发期改成 True 可热重载


# `python -m station.app.server` 直接运行本文件时才执行下面的代码；
# 如果是被别人 import（如 uvicorn 按模块加载）则跳过 —— 这就是 __name__ 的惯用法。
if __name__ == "__main__":
    main()
