"""内置 MCP server：网页搜索（零 key）。

新手视角（Java 朋友版）：
  这是一个**独立的小程序**，不是一个类库 —— 宿主会 `python -m station.mcp.servers.websearch`
  把它当成子进程起起来，然后在它的标准输入/输出上收发 JSON-RPC（这就是 stdio 传输）。
  它对外只提供一个能力：`web_search(query, count)`。

  它跟宿主的唯一接口是 MCP 协议本身；除此之外它不 import 宿主的任何东西
  （这正是"能力可搬走"的形态 —— 哪天不用宿主了，这个文件换个 MCP 客户端照样能用）。

★ 为什么自己写一个、不用现成的免费搜索 MCP：
  那些现成的（free-search-mcp / agent-search-mcp 等）**全是 stdio 型、且大多是 Node 的**，
  服务器上既没 Node、1.9G 内存也扛不住它们带的 Chromium 回退。而这个站要的是
  "开箱即用、永远加载"的内置能力，所以自己写一个**纯 HTTP、零依赖**的。

★ 数据来源：Bing / 百度 / 搜狗 的结果页 HTML（**都不需要 API key**）。
  ★★ **09-16 在真机上各搜一次实测的结果**（单测用假响应，这里证明了"替身证明不了假设本身"）：
     · **Bing（cn.bing.com）能用** —— 返回真实网址 + 真摘要（实测"北京今天天气"拿到
       中国天气网等真结果）。
     · **百度不可用**：302 到 `wappass.baidu.com/.../captcha`（验证码拦截）。
     · **搜狗不可用**：302 到 `/antispider/`（反爬拦截）。
     两家都是**服务端拦截**，改解析正则救不回来。所以它们现在的定位是"Bing 万一不行时
     碰运气"，正常时候贡献 0 条 —— 但**仍然并行抓**：多一个引擎不多花多少时间，
     而哪天 Bing 也变脸了，至少还有机会。
  ★ **拦截页里也有 `<h3><a>`**，所以必须先把它们认出来（见 `_blocked`）——
    否则会扒出"看起来像结果"的假条目，用户拿到一堆点不开的链接，还以为是搜索质量差。
    **宁可少几条，不可放假结果。**
  代价是"抓 HTML"这件事本身很脆，所以每条解析都是**尽力而为**：解析不到就是没结果，
  绝不抛异常（见 `_fetch` 的注释）。
  ★ 加新引擎时：写一个 `_xxx(query, n) -> list[Hit]`，在 `_ENGINES` 里登记一行，
    **然后在真机上真搜一次**再算完工。
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape

import requests
from mcp.server.fastmcp import FastMCP

# 浏览器 UA：搜索引擎对 python-requests 的默认 UA 很不友好，会直接给你一个验证页。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 单次请求的超时（连接, 读取）。搜东西不该让用户等太久 —— 宁可少几条结果。
_TIMEOUT = (5, 8)

# 一次搜索最多往回带几条（schema 里写的是"最多 10"，这里是最后一道闸）
_MAX_COUNT = 10

# log_level="WARNING"：SDK 默认会在每次收请求时往 stderr 打一行 INFO，而子进程的 stderr
# 是直接接到宿主控制台上的 —— 每搜一次就刷几行噪音。压低到 WARNING，真出问题才出声。
mcp = FastMCP("websearch", log_level="WARNING")


@dataclass
class Hit:
    """一条搜索结果。dataclass ≈ Java 的 record / 只带字段的 POJO。"""
    title: str
    url: str
    snippet: str = ""
    engine: str = ""


# ── 抓取与清洗 ────────────────────────────────────────────────────────

# ★★ 拦截页识别（09-16 在真机上各搜一次才发现的，**单测永远看不到**）：
#   实测结果 —— 百度直接 302 到 `wappass.baidu.com/.../captcha`（验证码），
#   搜狗 302 到 `www.sogou.com/antispider/`（反爬）。**两家都是服务端拦截，
#   不是"版式变了"**，所以改解析正则救不回来。
#   而且更糟：拦截页里**也有 `<h3><a href=…>`**，不认出来就会被解析成
#   "看起来像结果"的假条目 —— 用户拿到一堆点不开的链接，还以为是搜索质量问题。
#   所以宁可"这个引擎没结果"，也绝不放假结果过去。
_BLOCK_URL = re.compile(r"(wappass\.baidu\.com|/antispider|/captcha|/verify|"
                        r"security_check|sorry/index)", re.I)
_BLOCK_TITLE = re.compile(r"(验证码|安全验证|访问过于频繁|请稍后再试|antispider)", re.I)


def _blocked(r) -> bool:
    """这一页是"验证码 / 反爬"拦截页吗？

    先看**最终 URL**（最准：我们没请求那个地址，是被 302 过去的），
    再看 `<title>` 兜底（有些拦截是 200 直接渲染的）。只看标题、不看正文 ——
    正文里出现"验证码"可能只是某条结果的摘要，那会误杀正常结果。
    """
    if _BLOCK_URL.search(getattr(r, "url", "") or ""):
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", (r.text or "")[:2000], re.S)
    return bool(m and _BLOCK_TITLE.search(m.group(1)))


def _fetch(url: str, params: dict) -> str:
    """抓一个页面，返回 HTML；**任何失败都返回空串**。

    ★ 为什么不抛异常：这是个搜索工具，一个引擎抽风不该让整次搜索失败 ——
      三个引擎里有一个回来就够了。上层看"空串 = 这个引擎没结果"。
    """
    try:
        r = requests.get(url, params=params, headers={"User-Agent": _UA,
                                                      "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return ""
        if _blocked(r):
            return ""                      # 验证码/反爬页 —— 当这个引擎没结果（见上面那段）
        # 编码：请求头/页面里常常没写对，靠内容猜一次（不然中文会变乱码）
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text or ""
    except Exception:                      # noqa：网络/超时/编码任何问题都当作"没结果"
        return ""


def _clean(html: str) -> str:
    """把一小段 HTML 变成纯文本：去掉标签、还原 &amp; 这类实体、压掉多余空白。"""
    text = re.sub(r"<[^>]+>", "", html or "")
    text = unescape(text)                  # &amp; → & ；&quot; → " 等
    return re.sub(r"\s+", " ", text).strip()


def _abs_url(href: str, base: str) -> str:
    """把相对地址补成绝对地址（搜狗给的是 /link?url=… 这种）。"""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return href


# ── 三个引擎的解析 ────────────────────────────────────────────────────
# 每个函数都是"尽力而为"：版式变了就返回少几条甚至空列表，不报错。
# 想加新引擎就照着写一个，再在 _ENGINES 里登记一行。

def _bing(query: str, n: int) -> list[Hit]:
    """Bing（cn.bing.com）。三个引擎里唯一直接给真实网址的。"""
    html = _fetch("https://cn.bing.com/search",
                  {"q": query, "count": str(n), "setlang": "zh-hans"})
    out: list[Hit] = []
    # 每条结果是一个 <li class="b_algo">…</li>
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        snippet = ""
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if p:
            snippet = _clean(p.group(1))
        out.append(Hit(title=_clean(m.group(2)), url=unescape(m.group(1)),
                       snippet=snippet, engine="Bing"))
        if len(out) >= n:
            break
    return out


def _baidu(query: str, n: int) -> list[Hit]:
    """百度（www.baidu.com）。

    百度的结果链接通常是 `baidu.com/link?url=…` 的跳转地址，真网址藏在容器的
    `mu="…"` 属性里 —— 有就优先用它（否则用户点开要绕一跳，还可能失效）。
    """
    html = _fetch("https://www.baidu.com/s",
                  {"wd": query, "rn": str(n), "ie": "utf-8"})
    return _h3_engine(html, "Baidu", "https://www.baidu.com", n)


def _sogou(query: str, n: int) -> list[Hit]:
    """搜狗（www.sogou.com）。链接同样是跳转地址，交给 _resolve_redirect 兜。"""
    html = _fetch("https://www.sogou.com/web", {"query": query, "num": str(n)})
    return _h3_engine(html, "Sogou", "https://www.sogou.com", n)


def _h3_engine(html: str, engine: str, base: str, n: int) -> list[Hit]:
    """百度/搜狗的版式碰巧一样：标题都在 `<h3><a href=…>标题</a></h3>` 里。

    摘要不好稳定地按块定位（两家的 class 名都改过好几轮），所以退一步：把页面里所有
    "摘要块"按顺序抓出来，**按位置**配给标题。配不上就留空 —— 宁可少个摘要，
    也不要错配（张冠李戴的摘要比没有摘要更糟）。
    """
    titles = re.findall(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    # 真网址优先：百度会在容器上写 mu="真实地址"
    mus = re.findall(r'\bmu="([^"]+)"', html)
    snippets = [_clean(x) for x in re.findall(
        r'<div[^>]+class="[^"]*(?:c-abstract|text-layout|star-wiki)[^"]*"[^>]*>(.*?)</div>',
        html, re.S)]

    out: list[Hit] = []
    for i, (href, title) in enumerate(titles):
        if i < len(mus) and mus[i].startswith("http"):
            url = unescape(mus[i])
        else:
            url = _abs_url(unescape(href), base)
        out.append(Hit(title=_clean(title), url=url,
                       snippet=snippets[i] if i < len(snippets) else "",
                       engine=engine))
        if len(out) >= n:
            break
    return out


# 引擎清单：(显示名, 函数)。加引擎就在这里加一行。
_ENGINES = (("Bing", _bing), ("Baidu", _baidu), ("Sogou", _sogou))


# ── 跳转地址还原（尽力而为）────────────────────────────────────────────

_REDIRECT_HINT = re.compile(r"(baidu\.com/link|sogou\.com/link|so\.com/link|"
                            r"www\.bing\.com/ck/a)", re.I)


def _resolve(hit: Hit) -> Hit:
    """把"跳转地址"还原成真实网址；还原不了就原样保留。

    只对看起来像跳转的地址动手，且**逐个独立失败**（一个还原不了不影响别的）。
    `stream=True` + `close()`：我们只要最终地址，不下载页面正文。
    """
    if not _REDIRECT_HINT.search(hit.url):
        return hit
    try:
        r = requests.get(hit.url, headers={"User-Agent": _UA},
                         timeout=(4, 4), allow_redirects=True, stream=True)
        final = r.url
        r.close()
        if final.startswith("http"):
            hit.url = final
    except Exception:                      # noqa：还原不了就用原来的跳转地址（照样能点开）
        pass
    return hit


# ── 对外唯一的工具 ────────────────────────────────────────────────────

def _fake_results(query: str, n: int) -> str:
    """测试模式（STATION_FAKE=1）：不发任何网络请求，回一份固定的假结果。

    为什么需要它：离线单测要**真的把这个子进程起起来、真的走一遍 MCP 协议**，
    但又不能联网（本仓的红线：单测不联网、不烧 key）。同 core/model.py 的 FakeModel。
    """
    lines = [f"【测试模式：没有真的联网搜索】关键词「{query}」"]
    for i in range(1, min(n, 3) + 1):
        lines.append(f"{i}. 关于「{query}」的第 {i} 条假结果\n"
                     f"   https://example.com/{i}\n"
                     f"   这是 STATION_FAKE=1 时返回的占位内容。")
    return "\n".join(lines)


@mcp.tool(name="web_search", title="网页搜索",
          # ★ "什么时候用它"要写**具体的例子** —— 这段是给主模型看的，
          #   它每轮都读得到（MCP 工具不走路由，直接进工具面，见 bridge.py 文件头）。
          #   09-16 实测：写法笼统时模型不知道什么时候该搜，写具体一点才稳。
          description=("用关键词搜网页，返回若干条结果的标题/网址/摘要。"
                       "凡是要查资料、找最新信息、确认某个事实的都该用它 —— "
                       "比如今天的天气、最近发生了什么、某个东西是什么、"
                       "某句话是不是真的。"))
def web_search(query: str, count: int = 5) -> str:
    """用关键词搜网页，返回标题 / 网址 / 摘要。

    Args:
        query: 搜索关键词。写得具体一点结果更准（"Python 读取 Excel 的库" 好过 "Python"）。
        count: 要几条结果，默认 5、最多 10。
    """
    query = (query or "").strip()
    if not query:
        return "没给关键词，搜不了。"
    n = max(1, min(int(count or 5), _MAX_COUNT))

    if os.environ.get("STATION_FAKE") == "1":
        return _fake_results(query, n)

    # 三个引擎并行抓（串行的话最慢的那个决定总耗时，而它们彼此独立）
    hits: list[Hit] = []
    with ThreadPoolExecutor(max_workers=len(_ENGINES)) as pool:
        futures = [pool.submit(fn, query, n) for _, fn in _ENGINES]
        for f in futures:
            try:
                hits.extend(f.result() or [])
            except Exception:              # noqa：单个引擎炸了不该拖垮整次搜索
                continue

    if not hits:
        return (f"没搜到「{query}」的结果。可能是关键词太偏，或者搜索引擎这会儿不正常"
                f"（可以换个说法再试一次）。")

    # 去重：标题相同的只留第一条（三个引擎经常返回同一篇文章）
    seen: set[str] = set()
    uniq: list[Hit] = []
    for h in hits:
        key = re.sub(r"\W+", "", h.title)[:40] or h.url
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    uniq = uniq[:n]

    # 还原跳转地址（并行；失败的原样保留）
    with ThreadPoolExecutor(max_workers=len(uniq) or 1) as pool:
        uniq = list(pool.map(_resolve, uniq))

    lines = []
    for i, h in enumerate(uniq, 1):
        lines.append(f"{i}. {h.title}")
        lines.append(f"   {h.url}")
        if h.snippet:
            lines.append(f"   {h.snippet}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 默认走 stdio：在这个进程的标准输入/输出上和宿主收发 JSON-RPC。
    mcp.run()
