"""Agnes AI 视频接口的薄客户端 —— **只管 HTTP，不懂业务、不碰文件存储**。

新手视角（Java 朋友版）：
  这个文件 ≈ 一个手写的 Feign / Retrofit 客户端，或者一个 `XxxApiClient` 工具类。
  它只做三件事：把请求发出去、把响应解析成 dict、把"出错了"翻译成人话异常。
  "什么时候该建任务、失败了要不要重试、生成了往哪儿存" 全是调用方（video_tools.py）
  的事 —— **把协议细节和业务逻辑分开**，好处是单测里可以整个替身掉这个模块
  （tests/test_video_skill.py 就是这么干的：假掉三个函数，一个字节都不上网）。

Agnes 的接口形状（官方文档，2026-09 核实）：
  创建任务  POST {base_url}/videos          ← base_url 形如 https://apihub.agnes-ai.com/v1
  查询任务  GET  {站点根}/agnesapi?video_id=...&model_name=...
  下载结果  响应里的 metadata.url 指向 mp4（可直接 GET）

★ 本模块最容易写错、且写错了最难查的一处：**查询端点不在 /v1 下面**。
  创建在 `/v1/videos`，查询却在**站点根**的 `/agnesapi` —— 所以不能拿 base_url
  直接拼查询 URL，必须先"求根"（见 `_root`）。拼错的症状是"任务建得成功、查询
  永远查不到"，而且日志里只有一个 404，很容易误判成"key 不对"。
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import requests          # 在 pyproject 的核心依赖里，不是可选项

# 建任务/查询这类小请求的超时（秒）。下载单独给更长的预算（见 download）。
TIMEOUT = 30

# 建任务被 503（队列临时不可用）时，退避多久再试。空元组 = 不重试。
# ★ **只重试 503，别的错一律不重试**，理由要看清：
#   503 `video_queue_unavailable` 是对端明确说"我这边队列满了、任务没收下"，
#   重试没有副作用；而
#     · 429 是按次限流 —— 立刻重试只会继续撞，必须让用户等；
#     · 4xx 是参数/密钥错，重试一万次也一样；
#     · **网络超时/连接断开不能重试**：那种情况下请求可能已经到达对端并建好了任务，
#       重发等于生成两条（白烧一次额度，而且是静默的）。
#   09-14 实测：对端队列不稳，用户连撞两次 503，每次都直接判失败、只能手动重试。
CREATE_RETRY_WAITS = (5.0, 15.0)


class AgnesError(RuntimeError):
    """Agnes 接口返回错误时的统一异常（消息是**人话**，能直接给用户看）。

    为什么单独定义而不是直接 raise_for_status：
      ① 调用方要区分"被限流了（等等再试）"和"真的失败了（该报错）"，
         这两条路的处理完全不同 —— 见 status/retry_after 两个字段；
      ② requests 的原始异常里带着 URL 和响应片段，直接抛给用户看不懂也用不上。
    """

    def __init__(self, message: str, status: int = 0, retry_after: float = 0.0):
        super().__init__(message)
        self.status = status              # HTTP 状态码；0 = 网络层错误（连不上/超时）
        self.retry_after = retry_after    # 建议等多少秒再来（429 时用；0 = 没建议值）


def _root(base_url: str) -> str:
    """把 provider 的 base_url 还原成**站点根**：https://host/v1 → https://host。

    查询端点 /agnesapi 挂在站点根上，不在 /v1 下面 —— 这一行就是为它存在的。
    没有 /v1 后缀的地址（比如用户自己配的代理）原样返回。
    """
    u = (base_url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        return u[:-3].rstrip("/")
    return u


def _headers(key: str) -> dict:
    """请求头：Bearer 认证 + JSON。key 只在请求头里出现，绝不写进日志或 URL。"""
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _retry_after(resp) -> float:
    """从响应头里读 Retry-After（秒）；读不出/不是数字就回 0（调用方用自己的默认值）。"""
    raw = (resp.headers.get("Retry-After") or "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _fail(what: str, resp) -> AgnesError:
    """把一次失败的响应翻成 AgnesError（含状态码与"等多久再来"）。

    刻意**不把响应体塞进消息**：它可能很长、也可能是对端的一段 HTML，
    摆到对话里对用户毫无帮助。状态码 + 人话够了，细节进 job 的日志。
    """
    code = resp.status_code
    if code == 429:
        return AgnesError("Agnes 接口被限流了（免费额度每分钟只允许 1 次请求）",
                          status=code, retry_after=_retry_after(resp))
    if code == 401:
        return AgnesError("Agnes 说密钥不对（401）——检查「视频生成模型」那一行的密钥，"
                          "注意国内站与国际站的密钥是**不通用**的", status=code)
    if code == 402:
        return AgnesError("Agnes 说账户余额/额度不够（402）", status=code)
    return AgnesError(f"{what}失败：Agnes 返回 HTTP {code}", status=code)


def create_task(base_url: str, key: str, model: str, prompt: str,
                seconds: str = "5", aspect_ratio: str = "16:9",
                images: list[str] | None = None,
                timeout: float = TIMEOUT) -> dict:
    """建一条视频生成任务，返回响应 dict（里面有 video_id，取件码）。

    `images`：参考图 URL 列表（**必须是对端能公开访问的直链**）。给了它就自动切到
    `reference` 模式 —— 模式不是调用方要操心的东西，有图就是参考生成。

    ★ 只有**建任务**这一步会真正消耗额度；它一旦成功，后面就必须一路跟到底
      （半途换一家重来 = 两边各生成一次、各烧一次配额）。
    """
    url = f"{(base_url or '').strip().rstrip('/')}/videos"
    body = {
        "model": model,
        "prompt": prompt,
        # ★ seconds 必须是**字符串**（"4"–"12"），传成数字会被拒 —— 官方文档明写，
        #   而且 400 的报错文案是 `seconds must be a string` 这种，不看文档猜不到。
        "seconds": str(seconds),
        # 纯文生视频用 "text"；带参考图用 "reference"（Flash 最多 5 张）。
        "mode": "reference" if images else "text",
        "size": "720P",          # Flash 档只支持 720P，写别的会被 400 挡回来
        "aspect_ratio": aspect_ratio,
    }
    if images:
        # ★ 只在真带了图时才出现这个键 —— 纯文本那条路的请求体与加功能之前**逐字一致**
        #   （别写成 "images": images or []，空数组在 reference 模式下是非法请求）。
        body["images"] = list(images)

    last: AgnesError | None = None
    for i, wait in enumerate((0.0,) + tuple(CREATE_RETRY_WAITS)):
        if wait:
            time.sleep(wait)
        try:
            resp = requests.post(url, headers=_headers(key), json=body, timeout=timeout)
        except requests.RequestException as e:
            # ★ 网络层失败**不重试**：请求可能已经到达对端并建好了任务，
            #   重发就是两条视频、白烧一次额度（见 CREATE_RETRY_WAITS 的说明）。
            raise AgnesError(f"连不上 Agnes（{type(e).__name__}）") from e
        if resp.status_code == 503 and i < len(CREATE_RETRY_WAITS):
            last = _fail("创建视频任务", resp)     # 队列临时不可用 → 退避后再来
            continue
        if resp.status_code >= 400:
            raise _fail("创建视频任务", resp)      # 其余错误不重试
        try:
            return resp.json()
        except ValueError as e:                    # 返回了非 JSON（网关的 HTML 错误页）
            raise AgnesError("Agnes 返回的内容不是 JSON（多半是网关错误页）") from e
    raise last or AgnesError("创建视频任务失败")


def query_task(base_url: str, key: str, model: str, video_id: str,
               timeout: float = TIMEOUT) -> dict:
    """查一条任务的状态。

    返回里关心的字段：
      status   queued / in_progress / completed / failed（**别的值一律当"还在跑"**，
               见 video_tools 里的宽容匹配 —— 漏认一个状态名会表现为"跑到最后超时"）
      progress 0–100（只是对端给的参考值，调用方仍要自己保证进度只涨不跌）
      metadata.url  完成后 mp4 的地址

    ★ 这里是**查询**，不产生新视频、不额外花钱；但会占用请求配额
      （免费档实测 RPM 只有 1），所以调用方要控制轮询间隔。
    """
    q = urlencode({"video_id": video_id, "model_name": model})
    url = f"{_root(base_url)}/agnesapi?{q}"
    try:
        resp = requests.get(url, headers=_headers(key), timeout=timeout)
    except requests.RequestException as e:
        raise AgnesError(f"查任务时连不上 Agnes（{type(e).__name__}）") from e
    if resp.status_code >= 400:
        raise _fail("查询视频任务", resp)
    try:
        return resp.json()
    except ValueError as e:
        raise AgnesError("Agnes 返回的内容不是 JSON（多半是网关错误页）") from e


def download(url: str, timeout: float = 180) -> bytes:
    """把生成好的 mp4 拉成字节，交给 station.files.store 落盘。

    超时给到 180 秒：这一步是真的在传文件（几 MB），比建任务/查询慢得多。
    ★ 按官方文档，metadata.url 是**可公开访问**的直链，所以这里不带 Authorization
      头（带了反而可能被某些 CDN 当成异常请求）。
    """
    u = (url or "").strip()
    if not u:
        raise AgnesError("任务完成了，但响应里没有视频地址（metadata.url 为空）")
    try:
        resp = requests.get(u, timeout=timeout)
    except requests.RequestException as e:
        raise AgnesError(f"下载视频时连不上（{type(e).__name__}）") from e
    if resp.status_code >= 400:
        raise AgnesError(f"下载视频失败：HTTP {resp.status_code}", status=resp.status_code)
    return resp.content


def result_url(status: dict) -> str:
    """从查询响应里取成品视频的地址；没有就返回空串。

    ★★ 这里是**实测纠正文档**的一处（09-14，第一次带真 key 跑才暴露）：
      官方文档的响应示例写的是 `metadata.url`，而**真实响应是个扁平结构**、顶层直接有 `url`：

          {"status": "completed", "progress": 100, "error": null, "seconds": "5",
           "size": "720P",
           "url": "https://platform-outputs.agnes-ai.space/videos/.../task_xxx.mp4"}

      响应里**根本没有 metadata 这个键**。照文档只读 metadata.url 的后果是：视频明明
      生成成功了，却被判成"任务完成了但没给地址"而失败 —— 而且**对端那边已经烧掉了
      一次生成**（这个错误最贵的地方）。
      两个位置都认（顶层优先），哪天对端改回文档那种嵌套结构也不会断。
    """
    for src in (status, status.get("metadata")):
        if isinstance(src, dict):
            u = str(src.get("url") or "").strip()
            if u:
                return u
    return ""


def sleep_for(err: AgnesError, default: float) -> float:
    """被限流时该睡多久：对端给了 Retry-After 就用它，没给用调用方的默认间隔。

    单独抽成一个函数是为了**好测** —— "429 之后睡多久"这条逻辑错了的表现是
    "一直撞限流、任务看起来卡住"，而那和"对端真的慢"长得一模一样。
    """
    return max(float(default), float(err.retry_after or 0.0), 1.0)
