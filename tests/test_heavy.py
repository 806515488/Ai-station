"""重活闸的离线单测 —— 不联网、不烧 key、不碰真数据。

新手视角（Java 朋友版）：这一组只测一件事 —— **吃内存的活不许同时跑两件**。

为什么值得单独一组测试：这个闸坏掉的两种方式都**没有症状**——
  · 闸失效（两件并跑）→ 平时看不出，只在某个大卷上被内核 OOM 杀掉，现场什么都不留；
  · 闸卡死（牌子没还）→ 从此所有任务排队到天荒地老，表现是"点了没反应"。
前者靠并发计数钉住，后者靠"抛异常的活也要还牌子"钉住。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from station.core import heavy

# 技能代码目录要手动进 sys.path —— 跟宿主装载技能时做的是同一件事
# （src/station/skills/registry.py 的 _load_module）。tests/conftest.py 只加了 src/。
_CODE = Path(__file__).resolve().parents[1] / "skills" / "archive" / "code"
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))


@pytest.fixture(autouse=True)
def _one_slot(monkeypatch):
    """把闸钉成"只有 1 个坑"。

    模块级的 SLOTS 是**导入时**读环境变量算出来的，跑测试的机器上万一设了
    STATION_HEAVY_SLOTS=2，这些用例就会莫名其妙地红。这里换一把干净的锁，
    monkeypatch 会在用例结束后自动还原。
    """
    monkeypatch.setattr(heavy, "_gate", threading.Semaphore(1))
    monkeypatch.setattr(heavy, "_names", [])


# ── 闸本身 ──────────────────────────────────────────────────────

def test_one_slot_at_a_time():
    """★ 拿到牌子的期间，别人（wait=0 不想等）拿不到。"""
    with heavy.heavy_slot("甲") as first:
        assert first is True
        assert heavy.busy_with() == "甲"
        with heavy.heavy_slot("乙", wait=0) as second:
            assert second is False                 # 没等到 → 调用方自己决定怎么办
        assert heavy.busy_with() == "甲"           # 失败的那次不该把名字留下
    # 还回去之后就能进来了
    with heavy.heavy_slot("乙", wait=0) as third:
        assert third is True
    assert heavy.busy_with() == ""                 # 都出来了，没人占


def test_slot_is_returned_even_when_the_work_raises():
    """★ 干到一半抛异常也必须还牌子 —— 否则闸永久卡死，比内存不够更糟。

    （比内存不够更糟是因为：OOM 至少重启就好，闸卡死是整个服务从此不能出件，
      而且用户看到的现象只是"点了没反应"，最难查。）
    """
    with pytest.raises(ValueError):
        with heavy.heavy_slot("会炸的活"):
            raise ValueError("模拟中途失败")
    assert heavy.busy_with() == ""
    with heavy.heavy_slot("下一个", wait=0) as got:
        assert got is True                          # 牌子确实被还回来了


def test_waiting_caller_gets_it_when_released():
    """等着的那个（wait=None = 后台任务那样一直等）在牌子一还就能续上。"""
    order: list[str] = []

    def later():
        with heavy.heavy_slot("排队者"):
            order.append("排队者进来")

    with heavy.heavy_slot("先来的"):
        t = threading.Thread(target=later, daemon=True)
        t.start()
        time.sleep(0.2)
        assert order == []                          # 还在门外等（闸是 1 个坑）
        order.append("先来的干完了")
    t.join(timeout=2)
    assert order == ["先来的干完了", "排队者进来"]


# ── 接进 JobManager 之后：后台任务真的是串行的 ──────────────────────

def test_jobs_run_one_at_a_time(tmp_path):
    """★ 两个后台任务撞在一起时，**同一时刻只有一个在跑**。

    这是本闸存在的全部理由（见 station/core/heavy.py 的文件注释）：两个任务的内存
    峰值是相加的，1.9G 机器上相加就 OOM。这里用"同时在里面的人数"直接量它。
    """
    from station.jobs.manager import JobManager

    live = 0
    peak = 0
    lock = threading.Lock()

    class _Skill:
        """最小假技能：build_runner 里睡一下（模拟长活），进出各记一次并发数。"""
        id = "demo-heavy"
        keys: list = []

        def build_runner(self, ctx, **kw):
            def gen():
                nonlocal live, peak
                with lock:
                    live += 1
                    peak = max(peak, live)
                try:
                    time.sleep(0.3)                 # 窗口够长，并跑就一定测得出来
                    yield {"type": "progress", "percent": 100, "message": "ok"}
                finally:
                    with lock:
                        live -= 1
            return gen()

    mgr = JobManager()
    jobs = [mgr.submit(_Skill(), {}) for _ in range(3)]
    for _ in range(60):                             # 最多等 6 秒
        if all((mgr.get(j.id) or {}).get("status") in ("done", "failed") for j in jobs):
            break
        time.sleep(0.1)
    assert [mgr.get(j.id)["status"] for j in jobs] == ["done"] * 3
    assert peak == 1, f"同时跑了 {peak} 件 —— 重活闸没生效"


def test_waiting_job_says_it_is_queued(tmp_path):
    """排队中的任务要把"我在等谁"写进 message —— 否则用户看到进度条不动，以为死了。"""
    from station.jobs.manager import JobManager

    started = threading.Event()

    class _Slow:
        id = "demo-slow"
        keys: list = []

        def build_runner(self, ctx, **kw):
            def gen():
                started.set()
                time.sleep(0.5)
                yield {"type": "progress", "percent": 100, "message": "ok"}
            return gen()

    class _Quick(_Slow):
        id = "demo-quick"

    mgr = JobManager()
    slow = mgr.submit(_Slow(), {})
    started.wait(timeout=2)                         # 保证慢的已经拿着牌子了
    quick = mgr.submit(_Quick(), {})
    snap = None
    for _ in range(30):
        snap = mgr.get(quick.id)
        if snap and "排队" in (snap.get("message") or ""):
            break
        time.sleep(0.05)
    assert snap and "排队" in snap["message"]        # 说清在等谁
    for _ in range(60):
        if (mgr.get(quick.id) or {}).get("status") == "done":
            break
        time.sleep(0.1)
    assert mgr.get(quick.id)["status"] == "done"    # 等到了就正常跑完
    assert mgr.get(slow.id)["status"] == "done"


# ── 对话里的导出：忙就当场说清楚，不许干等 ────────────────────────

def test_export_tool_refuses_while_busy(monkeypatch):
    """★ 有别的重活在跑时，`export` 工具**不导出、也不挂死**，而是说清楚让用户过会儿再来。

    对话是同步 HTTP 请求，干等几分钟不合适（让浏览器一直转圈）。等不到就软拒绝 ——
    和 t_recognize 用后台 job 绕开"工具调用干等"是同一个取舍。
    """
    import archive.station_adapter as ad
    import archive_tools as at                      # 技能入口模块（sys.path 见文件头）
    from station.core.context import Context

    called = []
    monkeypatch.setattr(ad, "export_archive",
                        lambda args: called.append(args) or [])
    monkeypatch.setattr(at, "_get_current", lambda ctx: ("项目", "张三"))

    with heavy.heavy_slot("别的活"):                 # 闸被占着
        out = at.t_export(Context(skill_id="archive"))
    assert "重活" in out["text"] and "别的活" in out["text"]
    assert called == [], "忙的时候不该真去导出（那正是会 OOM 的组合）"


def test_export_tool_runs_when_free(monkeypatch):
    """没人占闸时正常导出（别把闸做成了"永远拒绝"）。"""
    import archive.station_adapter as ad
    import archive_tools as at
    from station.core.context import Context

    monkeypatch.setattr(ad, "export_archive", lambda args: [])
    monkeypatch.setattr(at, "_get_current", lambda ctx: ("项目", "张三"))

    out = at.t_export(Context(skill_id="archive"))
    assert "重活" not in out["text"]                 # 走到了真导出
    assert "导出完成" in out["text"]                 # export_archive 返回空 → 这句话
