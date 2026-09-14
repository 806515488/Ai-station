"""重活闸 —— 全站同一时刻只允许 N 件"吃内存的活"在跑（默认 N=1）。

新手视角（Java 朋友版）：≈ 一个全局的 `Semaphore(1)`。谁要干吃内存的活，先来领牌子；
干完还回去。领不到牌子的：后台任务**排队等**，对话里的工具调用**当场说"现在忙"**
（因为对话是同步请求，让用户的浏览器干等几分钟不合适 —— 与 t_recognize 用后台 job
绕开"工具调用干等"是同一个道理）。

★ 为什么需要它（09-13 实测，别删）：
  这台站可能跑在 1.9G 内存的小机器上，而两件重活的峰值内存都是**跟页数线性**的 ——
  实测档案 4 件套导出：40 页 524MB / 120 页 1411MB（≈11MB/页，见 archive.export.
  original_pdf）。两个任务撞在一起是**相乘不是相加**：串行跑峰值 = max(单件)，
  并行跑 = sum(所有件) + 两份各自的临时副本 → 机器 OOM，进程被内核杀掉，
  用户看到的是"服务突然没了"（没有任何报错，最难查的那种故障）。

★ 规矩：只许在**最外层**领牌子 —— 一个后台 job 的整条链、或一次对话里的导出动作。
  绝不许在 `export_pdf` / `_export_all` 这种"可能被里层再调用一次"的函数里领：
  同一线程拿着牌子再领一次同一把锁 = **自己等自己，永久死锁**。
  （牌子不可重入，也没打算做成可重入 —— 最外层领是最简单也最不容易写错的做法。）
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

# 同时能跑几件重活。1 = 串行（小内存机器的推荐值，也是默认）。
# 机器升到 4G 以上、想让识别和导出并行，就设 STATION_HEAVY_SLOTS=2。
SLOTS = max(1, int(os.environ.get("STATION_HEAVY_SLOTS", "1") or "1"))

# 对话里等牌子的耐心（秒）：等这么久还没有就直接告诉用户"现在忙"，别把请求挂死。
# 不给 0 是因为有时另一件活就差几秒收尾，等一下比让用户重说一遍更顺。
CHAT_WAIT = float(os.environ.get("STATION_HEAVY_WAIT", "10") or "10")

_gate = threading.Semaphore(SLOTS)      # 牌子本身
_names: list[str] = []                  # 当前拿着牌子的活叫什么（只为提示文案，不参与并发控制）
_names_lock = threading.Lock()          # 保护 _names（列表不是线程安全的）


def busy_with() -> str:
    """现在谁在占着重活闸？没人占返回空串。

    只用来给用户写提示（"识别任务正在跑，等它完了再导出"）——别拿它做并发判断，
    判断一律走 heavy_slot（这里读到的名字随时可能变，读的时候人可能刚好干完）。
    """
    with _names_lock:
        return "、".join(_names)


@contextmanager
def heavy_slot(what: str, wait: float | None = None):
    """领牌子。用法：

        with heavy_slot("导出 4 件套", wait=10) as got:
            if not got:
                return "现在有别的重活在跑……"      # 对话里：软拒绝
            do_the_heavy_thing()

    what：这次活叫什么（进提示文案用），如 "archive 识别任务"。
    wait：最多等几秒。None（默认）= 一直等（后台 job 用这个，排队是天经地义的）。

    yield 出来的是**拿到了没有**（bool）。★ 没拿到时也照样进 with 体，所以调用方
    必须自己看这个值 —— 不写成"拿不到就抛异常"，是因为两条路的处理方式完全不同
    （job 等、对话拒绝），让调用方自己写更清楚。
    """
    # ★ 先把牌子取进**局部变量**，acquire 和 release 都用它。
    #   别写成 acquire 时读一次全局 _gate、release 时再读一次 —— 那样"借的是 A 的牌子、
    #   还的是 B 的牌子"（只要 _gate 中途被换过）。运行期 _gate 从不更换，所以这个写法
    #   在线上无害；但单测的 fixture 会 monkeypatch 它，于是上一个用例留下的后台线程
    #   会在新用例里还牌子 → 白送一个名额 → **闸静默失效**（正是这个模块最怕的故障）。
    #   09-14 被 tests/test_heavy.py 的顺序依赖暴露出来，改成局部变量。
    gate = _gate
    # acquire 的 timeout=None 就是"永远阻塞"，语义正好对上，不用分两个分支
    got = gate.acquire(timeout=wait)
    if not got:
        yield False
        return
    with _names_lock:
        _names.append(what)
    try:
        yield True
    finally:
        # ★ 还牌子必须在 finally 里：活干到一半抛异常也得还，否则闸就永久卡死了
        #   （比内存不够更糟 —— 那是整个服务从此不能出件）。
        with _names_lock:
            if what in _names:
                _names.remove(what)
        gate.release()
