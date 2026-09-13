"""station 宿主离线单测（不联网、不烧 key；用 FakeModel 验证 harness 循环本身）。

新手视角（Java 朋友版）：这就是一套“单元测试”，像 JUnit。所有测试都**离线**：
  - 不调真模型（用 FakeModel 替身）→ 所以不花一分钱、也不会因网络失败而飘
  - 数据目录被 conftest 改到临时目录 → 不会污染你真机 data/
每个 test_ 开头的函数就是一个用例：准备输入 → 跑被测代码 → assert 断言结果。
看懂这批用例 = 看懂宿主最核心的几条行为契约（工具 schema、批准闸、agent 循环、流式输出…）。
"""
from __future__ import annotations

import json
import time

import pytest

from station import config
from station.core import compact, guards
from station.core.context import Context
from station.core.events import EV_APPROVAL, EV_DONE, EV_TOOL   # 事件类型常量
from station.core.session import Thread
from station.core.tool import Tool
from station.skills.registry import get_registry

# ── tool / guards ────────────────────────────────────────────────


def _t(name="demo.x"):
    """快捷造一个假工具对象（Tool）给测试用。run 只是个会返回 "ok" 的空函数。"""
    return Tool(name=name, description="d", run=lambda ctx, **kw: "ok")


def test_tool_schema_namespaced():
    """验证：Tool.schema() 生成的说明书里，名字是带命名空间的完整名 demo.x。"""
    s = _t().schema()                              # 把假工具转成 OpenAI schema
    assert s["function"]["name"] == "demo.x"       # 名字应该是 demo.x 而不是裸 x
    req = s["function"]["parameters"]["required"]  # 参数结构里该有 required 字段
    assert isinstance(req, list)                   # 且它是一个列表


def test_guard_classify_answer():
    """验证：批准词的判词器能把“允许/拒绝/无关”说对话分成三类。"""
    assert guards.classify_answer("可以，执行吧") == "allow"   # “可以”→ 允许
    assert guards.classify_answer("我拒绝") == "deny"          # “拒绝”→ 拒绝
    assert guards.classify_answer("随便聊聊") == ""            # 无关 → 空


# ── Thread / memory ──────────────────────────────────────────────


def test_thread_roundtrip(tmp_path):
    """验证：Thread（会话）能存盘再读回，且 pending（待批准）不丢。"""
    root = tmp_path / "sess"                       # 用一个临时目录当存储根
    t = Thread(skill_id="demo-agent")              # 建一个会话
    t.add_user("你好")                             # 加一条用户消息
    t.add({"role": "assistant", "content": "在的"})  # 加一条助手消息
    t.set_pending("demo.fake_risky", {})           # 挂一个“待批准”状态
    t.save(root)                                   # ① 存盘
    t2 = Thread.load(t.id, root)                   # ② 从磁盘读回
    assert t2 is not None and t2.skill_id == "demo-agent"   # 基本字段还在
    assert len(t2.msgs) == 2                       # 两条消息都读回来了
    assert t2.pending and t2.pending["tool"] == "demo.fake_risky"  # 待批准也没丢


def test_memory_compact_keeps_tail():
    """验证：超长历史能触发压缩，且压缩后保留最近几条、最前变成一条 system 摘要。"""
    msgs = [{"role": "user", "content": f"第{i}条内容内容内容" * 20}   # 30 条很长的消息
            for i in range(30)]
    assert compact.should_compact(msgs, 2000)       # 超过阈值 2000 → 应该触发
    out, changed = compact.compact(msgs, model=None, keep=6)   # 不真调模型压（截断兜底）
    assert changed and len(out) < len(msgs)        # 确实变短了
    assert out[0]["role"] == "system"              # 最前是压出来的 system 摘要


# ── registry ─────────────────────────────────────────────────────


def test_registry_loads_skills():
    """验证：宿主注册表能扫到全部技能，且 agent 技能的工具被正确装载。"""
    reg = get_registry()
    ids = {s.id for s in reg.list()}
    assert {"demo-agent", "demo-pipe", "archive",
            "weekly-report"} <= ids                # 4 个技能都在（archive 已合并为一个）
    demo = reg.get("demo-agent")
    assert demo is not None and demo.type == "agent"          # 是 agent 型
    assert {t.name for t in demo.tools} >= {"demo.now", "demo.echo"}  # 工具被命名空间化
    arc = reg.get("archive")
    assert arc.type == "agent" and arc.tools                 # 档案技能 = agent + 一套工具
    assert "archive.export" in {t.name for t in arc.tools}   # 出件工具在（risk=approve）
    assert arc.system                                        # 系统提示词（system.md）被读进


def test_skill_can_carry_both_tools_and_runner():
    """一个技能可以同时有工具面和执行器入口（09-12 合并 archive/archive-engine）。

    回归背景：registry._load 原来是 `if type==agent: tools elif type==pipeline: runner`
    —— 二选一，所以 agent 型的 archive 永远拿不到 build_runner，t_recognize 只能按
    字符串去够另一个技能 "archive-engine"。**当时没有任何测试能发现这件事**（adapter
    的两个用例都是直接 import build_runner，绕过注册表）。这条从注册表这一侧断言，
    把装载逻辑改回二选一就会红。
    """
    reg = get_registry()
    arc = reg.get("archive")
    assert arc.build_runner is not None            # 执行器挂在**技能自己**身上
    assert arc.tools                               # 工具面没被执行器挤掉
    assert reg.get("archive-engine") is None       # 拆出去的那个技能目录已删除
    # 另外两种形态不受影响：纯后台只有 runner，纯对话只有 tools
    assert reg.get("demo-pipe").build_runner is not None
    assert not reg.get("demo-pipe").tools
    assert reg.get("weekly-report").tools
    assert reg.get("weekly-report").build_runner is None


def test_recognize_submits_with_own_skill(tmp_path, monkeypatch):
    """t_recognize 起 job 时交给宿主的是**本技能自己**的执行器，不是别的技能 id。

    用一个假 manager 接住 submit（不起真线程、不碰模型），断言交出去的 skill.id
    就是 ctx.skill_id，且它的 build_runner 真的在。
    """
    from station.jobs import manager as jm

    data_dir = tmp_path / "skilldata"
    data_dir.mkdir()
    pj = tmp_path / "卷" / "project.json"
    pj.parent.mkdir()
    pj.write_text(json.dumps({"name": "卷", "records_file": "photos.json"},
                             ensure_ascii=False), encoding="utf-8")
    # 登记"手头这卷"——等价于刚刚 scan_photos 成功（工具靠 current.json 找当前卷）
    (data_dir / "current.json").write_text(
        json.dumps({"project": str(pj), "person": "测试"}, ensure_ascii=False),
        encoding="utf-8")

    class _FakeJob:
        id = "job-test"

    class _FakeMgr:
        def __init__(self):
            self.seen = None
        def submit(self, skill, args, user_id=""):    # 签名与真 manager 一致
            self.seen = (skill, args, user_id)
            return _FakeJob()

    fake = _FakeMgr()
    monkeypatch.setattr(jm, "get_manager", lambda: fake)

    ctx = Context(skill_id="archive", data_dir=data_dir, user_id="u1")
    recognize = next(t for t in get_registry().get("archive").tools
                     if t.leaf() == "recognize")
    out = recognize.run(ctx)

    assert out.startswith("识别已开始")            # 不再是"识别引擎没装载"
    skill, args, uid = fake.seen
    assert skill.id == "archive"                  # ← 合并前这里是 archive-engine
    assert skill.build_runner is not None         # 交出去的执行器是真家伙
    assert args["project"] == str(pj)             # 跑的就是手头这卷
    assert uid == "u1"                            # 归属用户带下去了


def _photos_dir(tmp_path, dirname="uploads/张三-6a7ebab3", n=2):
    """造一个"上传目录"：名字带 `<人名>-<随机后缀>`，里面是 n 张真 JPEG。

    （必须真图：create_project 会用 PIL 读图算 phash，写假字节直接 UnidentifiedImageError。）
    """
    import io

    from PIL import Image
    d = tmp_path / dirname
    d.mkdir(parents=True)
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 180, 160)).save(buf, "JPEG")
    for i in range(1, n + 1):
        (d / f"{i}.jpg").write_bytes(buf.getvalue())
    return d


def _archive_tool(leaf):
    return next(t for t in get_registry().get("archive").tools if t.leaf() == leaf)


def test_scan_photos_asks_before_creating_when_name_missing(tmp_path):
    """没填姓名就**不建卷**，只回一句"先确认姓名"（并附上从目录名猜的名字）。

    ★ 这是"留空就问一句再建卷"这条产品决定**落进代码**的证据：光靠提示词说"要先问"
      挡不住模型图省事 —— 它会拿目录名当姓名直接建卷（用户实测反馈：消息里直接写着
      "126 张，李明"，而他根本没填）。卡在工具这一层，模型忘了问也建不成。
    """
    ctx = Context(skill_id="archive", data_dir=tmp_path / "skilldata", user_id="u1")
    out = _archive_tool("scan_photos").run(ctx, dir=str(_photos_dir(tmp_path)))

    assert "先不建卷" in out
    assert "张三" in out                     # 目录名里的名字要提示出来，供"就用文件夹名"用
    assert "6a7ebab3" not in out             # ★ 随机后缀要洗掉，别漏给用户看
    assert not (ctx.data_dir / "current.json").exists()   # 没建卷 → 也没登记"手头这卷"


def test_scan_photos_registers_ownership(tmp_path):
    """给了姓名就建卷，且**建卷那一刻**归属就可查（识别途中页图才取得到）。

    ★ 归属必须**提前**登记：页图端点 `/api/archive/page` 按 archive_states 的归属
      鉴权，而识别结果是跑完才写的 —— 不提前登记，识别过程里进度面板每张缩略图
      都拿 404（浏览器里=一片破图）。
    """
    from station import db
    from archive.service.interactive import load_state

    ctx = Context(skill_id="archive", data_dir=tmp_path / "skilldata", user_id="u1")
    out = _archive_tool("scan_photos").run(ctx, dir=str(_photos_dir(tmp_path)),
                                           person="张三")

    assert "已建卷《张三》" in out
    cur = json.loads((ctx.data_dir / "current.json").read_text(encoding="utf-8"))
    pj = cur["project"]
    assert db.archive_state_owner(pj) == "u1"         # ★ 建卷即登记（此刻还没开始识别）
    assert load_state(pj)["stage"] == "created"       # 用 stage 与"识别产出的现场"区分


def test_recognize_keeps_the_scan_registration(tmp_path, monkeypatch):
    """t_recognize 不许把建卷登记清掉 —— 清了识别途中页图又变 404。

    回归背景：它原来对"空现场"一律 `invalidate_state`（本意是清掉上一轮识别留下的
    0 材料残留，免得被误判成"已识别过"）。加了建卷登记之后，那条空现场正是**刚登记
    的归属**，被清掉鉴权就没了 → 所以改成只清 `stage != "created"` 的。
    """
    from station import db
    from station.jobs import manager as jm

    ctx = Context(skill_id="archive", data_dir=tmp_path / "skilldata", user_id="u1")
    _archive_tool("scan_photos").run(ctx, dir=str(_photos_dir(tmp_path)), person="张三")
    pj = json.loads((ctx.data_dir / "current.json").read_text(encoding="utf-8"))["project"]
    assert db.archive_state_owner(pj) == "u1"

    class _FakeJob:
        id = "job-test"

    class _FakeMgr:
        def submit(self, skill, args, user_id=""):
            return _FakeJob()

    monkeypatch.setattr(jm, "get_manager", lambda: _FakeMgr())
    out = _archive_tool("recognize").run(ctx)

    assert out.startswith("识别已开始")
    assert db.archive_state_owner(pj) == "u1"         # ★ 登记还在（没被 invalidate 清掉）


def test_render_card_flows_through_tool():
    """验证渲染契约：工具返回 {"text","render"} → 事件桶里有 EV_RENDER + 文字回给模型。"""
    from station.core.agent import run_tool
    from station.core.events import EV_RENDER
    ctx = Context()                                # 最小上下文（事件桶在 ctx.events）
    tool = Tool(name="demo.show", description="d",
                run=lambda c: {"text": "给模型看", "render": {"type": "page-card"}})
    out = run_tool(ctx, tool, {})
    assert out == "给模型看"                        # 模型只拿到 text
    assert any(e.type == EV_RENDER and e.data["card"]["type"] == "page-card"
               for e in ctx.events)                # 前端拿到的卡片在事件桶


# ── agent 循环（FakeModel 离线）──────────────────────────────────


def _ctx(skill):
    """按技能造一份最小“运行上下文+会话+假模型”，供下面几个测试复用。"""
    from station.core.model import FakeModel
    thread = Thread(skill_id=skill.id)             # 新开空会话
    ctx = Context(thread=thread, skill_id=skill.id,
                  data_dir=config.sub("skills", skill.id),
                  allowed_keys=list(skill.keys or []),
                  auto_approve=config.AUTO_APPROVE)
    return ctx, thread, FakeModel()                # 假模型替身（离线）


def _run(ctx, thread, skill, model):
    """把 agent 一轮跑完，返回它 yield 的所有事件（列表）。"""
    from station.core.agent import run_agent
    return list(run_agent(ctx, thread, skill, model))


def test_agent_tool_loop():
    """验证核心闭环：agent 能“调工具 → 拿到结果 → 给最终答复”。"""
    skill = get_registry().get("demo-agent")
    ctx, thread, model = _ctx(skill)
    thread.add_user("现在几点？")                    # 用户提问
    events = _run(ctx, thread, skill, model)
    types = [e.type for e in events]
    assert EV_TOOL in types and EV_DONE in types    # 有调工具、也有正常结束
    tool_ev = next(e for e in events if e.type == EV_TOOL)   # 找到那次工具事件
    assert tool_ev.data["names"] == ["demo.now"]    # 调的是命名空间后的 demo.now
    assert thread.msgs[-1]["role"] == "assistant"   # 最终答复已写回历史


def test_long_sentence_is_not_an_approval():
    """★ 只有"**就是**一句同意/拒绝"才算回答 —— 夹着别的指令的长句不能当批准。

    补执行分支排在路由之前、执行的是 `thread.pending` 里那个工具。若长句也算批准，
    "确认这条口径，写进《文种对照》吧"（学习卡预填的话术）就会被判成 allow，
    顺手把**另一个**挂起的危险操作跑掉（09-13 review 抓到）。
    """
    from station.core import guards
    assert guards.classify_answer("允许") == "allow"
    assert guards.classify_answer("可以，就这样") == "allow"
    assert guards.classify_answer("拒绝") == "deny"
    assert guards.classify_answer("确认这条口径，写进《文种对照》吧") == ""
    assert guards.classify_answer("允许，另外帮我把那个也导一下") == ""


def test_agent_approval_gate():
    """验证批准闸：想调危险工具时被挂起，未获准前不会真的执行。"""
    skill = get_registry().get("demo-agent")
    ctx, thread, model = _ctx(skill)
    thread.add_user("fake_risky")                   # 触发危险工具
    events = _run(ctx, thread, skill, model)
    assert any(e.type == EV_APPROVAL for e in events)        # 发了 approval_request
    assert thread.pending and thread.pending["tool"] == "demo.fake_risky"  # 挂起了 pending
    assert all(m.get("role") != "tool" for m in thread.msgs)  # 历史里没有工具结果=没执行


def test_approval_event_carries_a_human_label():
    """★ 批准事件必须带上 `label`（人话），前端要拿它当标题。

    没有它，批准卡上显示的就是 `archive.apply_learning` 这种内部名 —— 用户看不懂
    （conventions「加工具要写两个'一句话'」）。这类"字段漏传"没有异常、没有日志，
    表现只是**界面难看/看不懂**，所以只能靠断言钉住。
    """
    skill = get_registry().get("demo-agent")
    ctx, thread, model = _ctx(skill)
    thread.add_user("fake_risky")
    ev = next(e for e in _run(ctx, thread, skill, model) if e.type == EV_APPROVAL)
    assert ev.data["tool"] == "demo.fake_risky"      # 原始名仍要在（title/回放用）
    assert ev.data.get("label"), "批准事件没带 label —— 前端只能显示内部工具名"


def _risky_tool(skill):
    """demo-agent 里那个 approve 类工具（测试里给它的 preview 打桩用）。"""
    return next(t for t in skill.tools if t.leaf() == "fake_risky")


def test_approval_preview_is_computed_by_the_tool(monkeypatch):
    """★ 工具声明了 `preview` → 挂起时把"这一步要做什么"算出来给用户看，并**存进 pending**。

    为什么必须存进 pending：刷新页面后要用**同一份**内容重画那张卡 —— 重算可能跟
    当时不一致（比如口径文件在这期间被改过）。
    为什么是工具自己算：宿主不认识业务，说不出"要往《文种对照》里加哪一条"。
    """
    skill = get_registry().get("demo-agent")
    monkeypatch.setattr(_risky_tool(skill), "preview",
                        lambda ctx, **kw: "我要动的东西：X 和 Y")
    ctx, thread, model = _ctx(skill)
    thread.add_user("fake_risky")
    ev = next(e for e in _run(ctx, thread, skill, model) if e.type == EV_APPROVAL)
    assert ev.data["preview"] == "我要动的东西：X 和 Y"
    assert thread.pending["preview"] == "我要动的东西：X 和 Y"   # 存下来给刷新后重画用


def test_approval_preview_failure_does_not_block(monkeypatch):
    """预览算不出来（抛异常）**不该拦住批准流程** —— 退回只显示 label 就行。"""
    skill = get_registry().get("demo-agent")

    def _boom(ctx, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(_risky_tool(skill), "preview", _boom)
    ctx, thread, model = _ctx(skill)
    thread.add_user("fake_risky")
    ev = next(e for e in _run(ctx, thread, skill, model) if e.type == EV_APPROVAL)
    assert ev.data["preview"] == ""                  # 空预览，但批准流程照走
    assert thread.pending and thread.pending["tool"] == "demo.fake_risky"


def test_real_archive_tools_have_previews():
    """★ archive 那两个 approve 工具**都写了 preview** —— 否则批准卡上只有"要做什么"，
    用户不知道自己在批准什么（用户原话："不然用户都不知道要写入什么内容"）。"""
    from station.skills.registry import get_registry
    ts = {t.leaf(): t for t in get_registry().get("archive").tools}
    for leaf in ("export", "apply_learning"):
        assert ts[leaf].risk == "approve"
        assert ts[leaf].preview is not None, f"{leaf} 少了 preview"


def test_weekly_multi_tool_chain(tmp_path):
    """验证多工具串联：写周报 = collect_highlights → render_report → 产物落盘。"""
    from station.files import store as fs
    skill = get_registry().get("weekly-report")
    ctx, thread, model = _ctx(skill)
    thread.add_user("帮我写本周周报")
    events = _run(ctx, thread, skill, model)
    names = [e.data.get("names", [])[0]
             for e in events if e.type == EV_TOOL]
    assert "weekly.collect_highlights" in names    # 第一步收素材
    assert "weekly.render_report" in names          # 第二步成稿
    assert events[-1].type == EV_DONE
    # 产物应落在（被 conftest 指到 tmp 的）文件区里，且是“周报-xxx.md”
    have = list(fs.iter_files())
    assert any(f["name"].startswith("周报") and f["name"].endswith(".md")
               for f in have)


# ── 流式输出（模型逐字吐字）──────────────────────────────────────


def test_stream_parts_aggregates_tool_call_chunks():
    """验证流式聚合：文字分片不重复，工具调用的分片（名字/参数被切开）能拼回一次完整调用。"""
    from langchain_core.messages import AIMessageChunk
    from station.core.model import _stream_parts
    # 模拟真实流式：name/id 只在首片，args（JSON）被切成三段
    chunks = [
        AIMessageChunk(content="你好",
                       tool_call_chunks=[{"name": "archive.set_category", "args": "",
                                          "id": "c1", "index": 0}]),
        AIMessageChunk(content="，世界",
                       tool_call_chunks=[{"name": None, "args": '{"target"',
                                          "id": None, "index": 0}]),
        AIMessageChunk(content="",
                       tool_call_chunks=[{"name": None, "args": ': "m3", "category": "九-2"}',
                                          "id": None, "index": 0}]),
    ]
    parts = list(_stream_parts(iter(chunks)))
    # delta 必须只发“新增的”，拼起来刚好是全文（发累积值会重复）
    assert "".join(p["delta"] for p in parts if "delta" in p) == "你好，世界"
    final = [p["final"] for p in parts if "final" in p][0]
    assert final["content"] == "你好，世界"
    assert final["tool_calls"] == [{"id": "c1", "name": "archive.set_category",
                                    "arguments": {"target": "m3", "category": "九-2"}}]


def test_stream_parts_empty_raises():
    """验证空流防御：模型一个分片都没吐时要报错，而不是静默返回一个空气泡。"""
    from station.core.model import _stream_parts
    with pytest.raises(RuntimeError):
        list(_stream_parts(iter([])))


def test_fake_stream_matches_respond():
    """验证离线流式与一次性调用同构：同一输入，stream 的 final 等于 respond 的返回。"""
    from station.core.model import FakeModel
    skill = get_registry().get("demo-agent")
    msgs = [{"role": "user", "content": "现在几点？"}]
    one_shot = FakeModel().respond(msgs, skill.tools)
    parts = list(FakeModel().stream(msgs, skill.tools))
    assert [p["final"] for p in parts if "final" in p][0] == one_shot


def test_agent_stream_no_duplicate_delta():
    """验证流式不重复显示：答复逐字流过，且只出现一次（旧实现会在末尾再发一整段）。"""
    from station.core.events import EV_DELTA
    skill = get_registry().get("weekly-report")
    ctx, thread, model = _ctx(skill)
    thread.add_user("帮我写本周周报")
    events = _run(ctx, thread, skill, model)
    streamed = "".join(e.data["text"] for e in events if e.type == EV_DELTA)
    final = thread.msgs[-1]["content"]
    assert events[-1].type == EV_DONE
    assert streamed.endswith(final)
    assert streamed.count(final) == 1          # 发两遍就会变成 2


def test_agent_stream_midway_error():
    """验证流中途报错：发 error 收尾，且不把半截文字写进历史。"""
    from station.core.events import EV_ERROR
    skill = get_registry().get("demo-agent")
    ctx, thread, model = _ctx(skill)
    thread.add_user("现在几点？")

    class BoomModel:
        """假模型：先正常吐一片文字，然后炸掉。"""
        def stream(self, msgs, tools=None):
            yield {"delta": "说到一半"}
            raise RuntimeError("网络断了")

    events = _run(ctx, thread, skill, BoomModel())
    assert events[-1].type == EV_ERROR
    assert all(m.get("role") != "assistant" for m in thread.msgs)   # 不留半截答复


def test_agent_preamble_goes_into_tool_message():
    """验证“边说话边调工具”：模型调工具前说的那句要写进历史（刷新回放才不丢）。"""
    from station.core.events import EV_DELTA
    tool = Tool(name="t.look", description="d", run=lambda c, **kw: "结果")

    class Skill:
        """最小的 agent 技能壳（只要有 tools 白名单就够 run_agent 跑）。"""
        id, ns, tools, system, keys, model = "t", "t", [tool], "", [], "text"

    class PreambleModel:
        """第 1 轮：说一句前言 + 调工具；第 2 轮：给最终答复。"""
        def __init__(self):
            self.n = 0

        def stream(self, msgs, tools=None):
            self.n += 1
            if self.n == 1:
                yield {"delta": "我先查一下"}
                yield {"final": {"content": "我先查一下", "tool_calls": [
                    {"id": "c1", "name": "t.look", "arguments": {}}]}}
            else:
                yield {"delta": "查完了"}
                yield {"final": {"content": "查完了", "tool_calls": []}}

    thread = Thread(skill_id="t")
    thread.add_user("开始")
    events = _run(Context(), thread, Skill(), PreambleModel())
    calls = [m for m in thread.msgs
             if m.get("role") == "assistant" and m.get("tool_calls")]
    assert calls and calls[0]["content"] == "我先查一下"        # 前言进了那条工具消息
    assert [e.data["text"] for e in events if e.type == EV_DELTA] == ["我先查一下", "查完了"]


def test_agent_approval_keeps_preamble():
    """验证批准闸挂起时，模型这轮说过的话也写进历史（刷新后不会凭空消失）。"""
    from station.core.events import EV_APPROVAL
    risky = Tool(name="t.danger", description="d", run=lambda c, **kw: "做了",
                 risk="approve")                     # risk=approve → 要过批准闸

    class Skill:
        id, ns, tools, system, keys, model = "t", "t", [risky], "", [], "text"

    class PreModel:
        """说一句前言，然后要求调危险工具。"""
        def stream(self, msgs, tools=None):
            yield {"delta": "这就动手"}
            yield {"final": {"content": "这就动手", "tool_calls": [
                {"id": "c1", "name": "t.danger", "arguments": {}}]}}

    thread = Thread(skill_id="t")
    thread.add_user("删了它")
    events = _run(Context(), thread, Skill(), PreModel())
    assert any(e.type == EV_APPROVAL for e in events)
    assert thread.pending and thread.pending["tool"] == "t.danger"
    assert thread.msgs[-1]["role"] == "assistant"
    assert thread.msgs[-1]["content"] == "这就动手"    # 前言落盘，回放不丢
    assert all(m.get("role") != "tool" for m in thread.msgs)   # 没批准就没执行


# ── 路由（L2 超时降级 / 路由日志）────────────────────────────────


def test_l2_route_timeout_degrades(monkeypatch):
    """验证 L2 判词超时/报错时快速降级：返回 None 并把原因记进诊断，不拖住对话。"""
    from station.core import router

    class SlowModel:
        """假模型：一调就超时（模拟判词链路偶发的 5 秒级卡顿）。"""
        def __init__(self, *a, **kw):
            pass

        def respond(self, msgs, tools=None):
            raise TimeoutError("mock 超时")

    monkeypatch.delenv("STATION_FAKE", raising=False)
    monkeypatch.setattr("station.core.model.Model", SlowModel)
    diag = {}
    assert router.l2_route("随便说点什么", [], diag) is None   # 降级，不抛
    assert diag["reason"].startswith("error:")                 # 原因记了下来
    assert "ms" in diag                                        # 耗时也记了


def test_l2_route_hard_deadline(monkeypatch):
    """★ L2 必须被**墙钟**兜住，而不是靠 HTTP 客户端的 timeout。

    背景（09-11 查真机 route_log 发现）：ROUTE_TIMEOUT=1.5 是交给 httpx 的，而
    httpx 的 timeout 是**分阶段**的（connect/read/write 各算各的），不是"整个请求
    最多 N 秒"。实测一次调用能跑满 3.3 秒才返回，真机上还出现过 23 秒的路由卡顿。
    现在 router 自己用线程 join 兜一层硬截止 —— 这条测试钉住它真的生效。
    """
    import time as _t
    from station.core import router

    class HangModel:
        """假模型：**一直不返回**（模拟端点卡死）。"""
        def __init__(self, *a, **kw):
            pass

        def respond(self, msgs, tools=None):
            _t.sleep(30)
            return {"content": '{"skill":"","confidence":0.9}'}

    monkeypatch.delenv("STATION_FAKE", raising=False)
    monkeypatch.setenv("ROUTE_TIMEOUT", "0.4")
    monkeypatch.setattr("station.core.model.Model", HangModel)

    diag = {}
    t0 = _t.perf_counter()
    hit = router.l2_route("你好", [], diag)
    el = _t.perf_counter() - t0
    assert hit is None                       # 超了就降级，不抛
    assert el < 2.0, f"预算是 0.4s，实测 {el:.2f}s —— 硬截止没生效"
    assert diag["reason"] == "timeout"       # 和"对端自己超时"区分开
    assert diag["ms"] < 2000


def test_with_deadline_passes_result_and_errors_through():
    """_with_deadline 只是"最多等 N 秒"，正常返回值和异常都要原样透传。"""
    from station.core import router

    assert router._with_deadline(lambda: 42, 1) == 42
    try:
        router._with_deadline(lambda: (_ for _ in ()).throw(ValueError("boom")), 1)
    except ValueError as e:
        assert str(e) == "boom"
    else:
        raise AssertionError("异常应该原样抛出")


def test_route_log_writes_jsonl(monkeypatch, tmp_path):
    """验证路由日志追加成 JSONL（一行一条），且 ROUTE_LOG=0 时安静跳过。"""
    from station import config
    from station.core import router
    monkeypatch.setattr(config, "ROUTE_LOG", True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    router.log_route({"text": "你好", "agent": "station", "level": "l1"})
    p = tmp_path / "route_log.jsonl"
    assert p.is_file()
    row = json.loads(p.read_text(encoding="utf-8").strip())
    assert row["text"] == "你好" and row["agent"] == "station" and "ts" in row
    monkeypatch.setattr(config, "ROUTE_LOG", False)     # 关掉开关 → 不再写
    router.log_route({"text": "第二条"})
    assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 1


# ── jobs（pipeline）──────────────────────────────────────────────


def test_job_pipeline(tmp_path):
    """验证异步任务：提交 → 后台跑 → 状态 done、进度 100%、有产物。"""
    from station.jobs.manager import JobManager
    skill = get_registry().get("demo-pipe")
    mgr = JobManager()
    job = mgr.submit(skill, {"steps": 1})           # 只跑 1 步（快）
    for _ in range(50):                             # 轮询最多 50×0.1s = 5 秒
        snap = mgr.get(job.id)
        if snap and snap["status"] in ("done", "failed"):
            break                                   # 到终态就停
        time.sleep(0.1)
    snap = mgr.get(job.id)
    assert snap and snap["status"] == "done"        # 应该成功而不是失败
    assert snap["progress"] >= 100                  # 进度走满
    assert snap["artifacts"], "应产出至少一个产物"   # 至少产出一个文件
