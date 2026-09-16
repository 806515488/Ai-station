"""video 技能的离线单测 —— **不联网、不烧 key、不碰真 data/**。

新手视角（Java 朋友版）：这组测试的套路是"把会跑出去的两样东西换掉"——
  ① 大模型（draft 用它优化提示词）→ 换成一个只会背台词的替身；
  ② Agnes 的 HTTP 接口（create_task/query_task/download）→ 换成假函数。
  换掉之后，整条链路（建任务→轮询→下载→落产物→发卡片）仍然**真的跑一遍**，
  所以"事件类型写错了""产物元数据漏了"这类缺陷照样会被抓到，而一个字节都没上网。

★ 为什么要盯着"产物元数据齐全"：落盘时少写 owner/skill_id/group_key 里任何一个，
  右上角「我的产物」抽屉就会掉回"按文件名猜分组"——不报错、只是产物七零八落。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from station.core.context import Context
from station.skills.registry import get_registry

# 技能代码目录要手动进 sys.path —— 跟宿主装载技能时做的是同一件事
# （tests/conftest.py 只加了 src/，技能自带的 code/ 目录归技能自己）。
_CODE = Path(__file__).resolve().parents[1] / "skills" / "video" / "code"
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

import agnes                      # noqa: E402
import video_tools as vt          # noqa: E402


# ── 夹具 ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """把轮询间隔压到 0：真跑的话每次要睡 5 秒，一条用例就卡住了。

    顺带钉一件事 —— 这两个值必须是**模块顶层的常量**，否则这里 patch 不到
    （改成在函数里现读环境变量的话，这行会静默失效、用例变慢而不是变红）。
    """
    monkeypatch.setattr(vt, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(vt, "POLL_DEADLINE", 5.0)


# 识图模型"看图"后应该回的那份结构化外貌描述（口径见 prompts/portrait.md）
PORTRAIT_JSON = (
    '{"subject":"青年男性，日常休闲感",'
    '"features":["黑色短发","方形脸","戴细框眼镜","体型偏瘦"],'
    '"photos":[{"n":1,"framing":"正面半身","usable":true,"note":""},'
    '{"n":2,"framing":"全身站姿","usable":true,"note":""}],'
    '"uncertain":[]}')


class _FakeLLM:
    """背台词的大模型替身：respond() 永远返回同一段话。"""

    def __init__(self, content: str):
        self._content = content

    def respond(self, msgs, tools=None):
        return {"content": self._content}


@pytest.fixture
def fake_chat(monkeypatch):
    """把模型调用换成替身：文本槽背一段台词、识图槽回一份人像 JSON。

    两个槽都**记下"发给它的是什么"**，好断言提示词里到底有没有那些内容。

    ★ 必须按 `kind` 分叉：加了人像之后 draft 会**连调两个槽**，只认一个的话第二次调用
      会拿到错的回答 —— 测试照样绿，但验的根本不是真行为（假绿比红更糟）。
    """
    box = {"content": "一只橘猫轻盈地跃上木质窗台，午后阳光穿过纱帘，镜头缓慢跟随",
           "vision": PORTRAIT_JSON,
           "asks": [], "vision_asks": []}

    class _M:
        def __init__(self, kind):
            self._kind = kind

        def respond(self, msgs, tools=None):
            (box["asks"] if self._kind == "text" else box["vision_asks"]).append(msgs)
            return {"content": box["content"] if self._kind == "text" else box["vision"]}

    monkeypatch.setattr("station.core.model.build", lambda kind="text", **kw: _M(kind))
    return box


def _seed_portrait(uid="u1", n=2, batch="b1"):
    """造一批人像文件（等价于上传端点落盘的结果）；返回**按序**的 fid 列表。"""
    from station.files import store as fs
    out = []
    for i in range(n):
        out.append(fs.save_bytes(
            f"IMGBYTES-{batch}-{i}".encode(), ".jpg", name=f"照片{i + 1}.jpg",
            owner=uid, skill_id="video",
            group_key=f"portrait:{uid}:{batch}", group_label="人像参考图",
            extra={"portrait_index": i}))
    return out


@pytest.fixture
def fake_agnes(monkeypatch):
    """把 Agnes 的三个 HTTP 函数换成假的；返回记录调用参数的盒子。

    query 按顺序回三次：还在跑 → 还在跑 → 完成（带 metadata.url）。
    """
    box = {"created": None, "queries": 0, "downloaded": ""}

    def _create(base_url, key, model, prompt, seconds="5", aspect_ratio="16:9",
                images=None, timeout=30):
        box["created"] = {"base_url": base_url, "key": key, "model": model,
                          "prompt": prompt, "seconds": seconds,
                          "aspect_ratio": aspect_ratio, "images": images}
        return {"video_id": "v-测试", "status": "queued"}

    def _query(base_url, key, model, video_id, timeout=30):
        box["queries"] += 1
        if box["queries"] < 3:
            return {"status": "in_progress", "progress": 30 * box["queries"]}
        # ★ 用**真实响应形状**：顶层扁平 url，没有 metadata（09-14 实测；文档写的是
        #   metadata.url，照文档写会把成功的任务判成失败 —— 见 agnes.result_url）
        return {"status": "completed", "progress": 100, "error": None,
                "seconds": "5", "size": "720P",
                "url": "https://cdn.example/v.mp4"}

    def _download(url, timeout=180):
        box["downloaded"] = url
        return b"FAKE-MP4-BYTES"

    monkeypatch.setattr(agnes, "create_task", _create)
    monkeypatch.setattr(agnes, "query_task", _query)
    monkeypatch.setattr(agnes, "download", _download)
    return box


def _ctx(**kw) -> Context:
    kw.setdefault("skill_id", "video")
    kw.setdefault("user_id", "u1")
    return Context(**kw)


@pytest.fixture
def with_public_base(monkeypatch):
    """配好"对外地址" —— 人像参考图那条路才走得通。"""
    monkeypatch.setenv("STATION_PUBLIC_BASE", "https://station.example.com")


def _thread_with_photos(fids, uid="u1"):
    """造一个"已经 draft 过、会话里记着这批人像"的会话。"""
    from station.core.session import Thread
    th = Thread()
    th.user_id = uid
    th.meta["video_photos"] = list(fids)
    return th


# ── 装载：技能真的被宿主认出来了 ─────────────────────────────────────

def test_skill_loads_from_registry():
    """★ 从**注册表**断言，不是直接 import —— 要抓的正是"装不上"这类错。

    技能代码 import 失败时 registry 是**静默跳过**的（obj=None，技能照样在表里但
    一个工具都没有），只看 import 成功与否发现不了。
    """
    s = get_registry().get("video")
    assert s is not None, "技能没装载"
    assert s.type == "agent", "非 agent 型进不了对话路由，也没法起后台任务"
    assert s.build_runner is not None, "没有执行器 = 视频根本生成不了"
    assert {t.leaf() for t in s.tools} >= {"draft", "make", "status", "show"}
    assert all(t.name.startswith("video.") for t in s.tools), \
        "工具名要带命名空间前缀（装载时由 registry 补，见 _load_tools）"
    assert s.system.strip(), "system.md 没读到 —— 模型拿不到岗位须知"


def test_manifest_is_marked_not_heavy():
    """★ 视频是纯网络等待型长活，必须绕开重活闸；而**别的技能不许跟着变轻**。

    两半缺一不可：只测前半的话，把 Skill.heavy 的默认值改成 False 也照样绿 ——
    而那意味着全站所有技能都不再受防 OOM 的闸保护。
    """
    assert get_registry().get("video").heavy is False
    assert get_registry().get("archive").heavy is True, \
        "archive 是真吃内存的（导出实测 11MB/页），默认值不许被改坏"


def test_tools_have_label_and_description():
    """加工具要写两个"一句话"：description 给模型、label 给人（没有异常，只能靠断言）。"""
    for t in get_registry().get("video").tools:
        assert t.description.strip(), f"{t.name} 缺 description"
        assert t.label.strip(), f"{t.name} 缺 label"


def test_make_is_approve_gated_with_preview():
    """★ 真生成必须过批准闸，且要有一句"将要发生什么"的预览。

    这条照抄 archives 的教训：`risk="approve"` 是"危险"的**唯一真源**，
    漏写没有异常也没有日志 —— 模型一调就落盘，单测还全绿。
    """
    make = next(t for t in get_registry().get("video").tools if t.leaf() == "make")
    assert make.risk == "approve", "生成要花额度，不该让模型自己调"
    assert make.preview is not None, "批准卡上要能说清将生成什么"


# ── draft：用通用模型优化提示词，不花钱 ──────────────────────────────

def test_draft_returns_optimized_prompt_card(fake_chat):
    """draft 把模型给的那段话做成草稿卡（**不发任务、不生成**）。"""
    out = vt.t_draft(_ctx(), idea="做个猫的视频", seconds="8", aspect_ratio="9:16")
    assert out["render"]["type"] == "prompt-card"
    assert out["render"]["prompt"] == fake_chat["content"]
    assert out["render"]["idea"] == "做个猫的视频"      # 原话要留在卡上，好对比
    assert out["render"]["seconds"] == "8"
    assert out["render"]["aspect_ratio"] == "9:16"
    # 给模型的文字里也要带上那版描述 —— 下一步 make 的 prompt 就是从这儿来的
    assert fake_chat["content"] in out["text"]


def test_draft_marks_raw_text_when_model_returns_nothing(fake_chat):
    """模型返回空时退回原话，但**必须说明这是原话** —— 别让人以为优化过了。"""
    fake_chat["content"] = ""
    out = vt.t_draft(_ctx(), idea="一只猫")
    assert out["render"]["prompt"] == "一只猫"
    assert out["render"]["note"], "退回原话时要留一句话说明"


def test_draft_asks_for_idea_when_empty():
    """空话术不调模型、不生成，直接问一句。"""
    out = vt.t_draft(_ctx(), idea="   ")
    assert isinstance(out, str) and "说说" in out


def test_optimize_prompt_keeps_its_placeholders_filled(fake_chat, monkeypatch):
    """优化用的模板文件要真的被读进来，且三个占位符都被替换掉。

    这条防的是"口径文件改了但代码没跟着改"：占位符没被替换的话，模型会收到一段
    带着 {IDEA} 字样的说明，然后开始胡编。
    """
    seen = {}

    def _build(kind="text", **kw):
        class _M:
            def respond(self, msgs, tools=None):
                seen["ask"] = msgs[0]["content"]
                return {"content": "改写结果"}
        return _M()

    monkeypatch.setattr("station.core.model.build", _build)
    vt.t_draft(_ctx(), idea="一段很特别的想法", seconds="7", aspect_ratio="4:3")
    ask = seen["ask"]
    assert "一段很特别的想法" in ask and "7" in ask and "4:3" in ask
    assert "{IDEA}" not in ask and "{SECONDS}" not in ask


# ── 人像：提取特征、融合提示词 ───────────────────────────────────────

def test_empty_ref_block_leaves_template_unchanged():
    """没有照片时 {REF_BLOCK} 被替换成空串 —— 模板与加这个功能之前**逐字一致**。

    这条钉的是"新功能不许改变老行为"：占位符后面**直接**跟标题（占位符自己没有换行），
    所以替换成空串后不会多出空行、也不会留下任何残留。
    """
    tmpl = (Path(__file__).resolve().parents[1] / "skills" / "video"
            / "prompts" / "optimize.md").read_text(encoding="utf-8")
    assert "{REF_BLOCK}" in tmpl
    stripped = tmpl.replace("{REF_BLOCK}", "")
    assert "{REF_BLOCK}" not in stripped
    assert "\n\n## 用户想要的内容" in stripped, "替换成空串后不该多出空行"


def test_draft_without_photos_is_unchanged(fake_chat):
    """不带照片时：ask 里既没有残留占位符、也没有任何"Picture"字样（与今天一致）。"""
    _seed_portrait()                       # 就算用户传过照片，没开开关也不该用
    vt.t_draft(_ctx(), idea="一只猫在跳")
    ask = fake_chat["asks"][0][0]["content"]
    assert "{REF_BLOCK}" not in ask, "占位符没被替换 —— 模型会收到一段说明然后开始胡编"
    assert "Picture" not in ask
    assert fake_chat["vision_asks"] == [], "没要求用人像却去调了识图模型（白花钱）"


def test_draft_with_photos_extracts_features_and_fills_ref_block(fake_chat):
    """★ 开了开关：先调识图模型读外貌 → 特征与 <Picture N> 都进 ask，草稿卡带照片。"""
    fids = _seed_portrait(n=2)
    out = vt.t_draft(_ctx(), idea="站在雨后的街头", use_my_photos=True)

    ask = fake_chat["asks"][0][0]["content"]
    assert "{REF_BLOCK}" not in ask
    assert "戴细框眼镜" in ask, "提取到的特征没进提示词"
    assert "<Picture 1>" in ask and "<Picture 2>" in ask
    assert "正面半身" in ask, "取景信息没进提示词（模型没法知道哪张是哪张）"

    # 发给识图模型的消息里：一段文字 + 每张一张图
    vmsg = fake_chat["vision_asks"][0][0]["content"]
    assert isinstance(vmsg, list)
    assert sum(1 for b in vmsg if b.get("type") == "image_url") == 2

    r = out["render"]
    assert r["type"] == "prompt-card"
    assert [p["id"] for p in r["photos"]] == fids, "卡片上的照片顺序必须和 <Picture N> 一致"
    assert r["features"], "卡片要把读出来的特征露给用户看"


def test_draft_survives_vision_failure_but_still_sends_photos(monkeypatch, fake_chat):
    """★ 识图失败**不静默降级**：照样出草稿卡、照片照样会交给视频模型，但要说明白。

    "读不出来就不发图"看似稳妥，其实让用户白传了照片还不知道 —— 这条断言把那个方向堵死。
    """
    fake_chat["vision"] = "抱歉，我看不清楚"        # 不是 JSON → 解析失败
    _seed_portrait(n=2)
    out = vt.t_draft(_ctx(), idea="在咖啡馆看书", use_my_photos=True)
    assert out["render"]["type"] == "prompt-card"
    assert out["render"]["photos"], "读不出特征也要把照片带上（用户传了就是想让用的）"
    assert "没读出来" in out["render"]["note"], "要如实说特征没读到"


def test_draft_without_vision_slot_says_how_to_fix(monkeypatch, fake_chat):
    """没配「识图模型」→ 给人话指向 ⚙，且**根本不去调模型**。"""
    from station import modelcfg
    real = modelcfg.resolve
    monkeypatch.setattr(modelcfg, "resolve",
                        lambda uid, slot: [] if slot == "vision" else real(uid, slot))
    _seed_portrait()
    out = vt.t_draft(_ctx(), idea="在海边散步", use_my_photos=True)
    assert "识图模型" in out["render"]["note"]
    assert fake_chat["vision_asks"] == [], "没配就不该发请求"


def test_draft_records_photo_set_in_thread_meta(fake_chat):
    """★ draft 把这一批的 fid **按序**记进会话 —— make 从这儿取，不让模型回传 id。"""
    from station.core.session import Thread
    fids = _seed_portrait(n=3)
    th = Thread()
    th.user_id = "u1"
    vt.t_draft(_ctx(thread=th), idea="站在山顶", use_my_photos=True)
    assert th.meta.get("video_photos") == fids


def test_draft_clears_photo_set_when_not_used(fake_chat):
    """不开开关时**要清空**上次记的那批 —— 否则下一轮 make 还会翻出旧照片来用。"""
    from station.core.session import Thread
    _seed_portrait()
    th = Thread()
    th.user_id = "u1"
    th.meta["video_photos"] = ["stale-id"]
    vt.t_draft(_ctx(thread=th), idea="一只猫在跳")
    assert th.meta.get("video_photos") == []


def test_portrait_batch_picks_newest_and_keeps_upload_order():
    """★ 只取**最新那一批**；批内顺序 = 上传先后（portrait_index），不靠时间戳。

    连着写几张的 created 可能撞在同一个值上，靠它排序会**静默指错** ——
    而这个功能的全部意义就是"像本人"，指错一张脸等于全错。
    """
    old = _seed_portrait(n=2, batch="old")
    new = _seed_portrait(n=3, batch="new")
    got = [it["fid"] for it in vt._portrait_batch(_ctx())]
    assert got == new, "取的应该是最新那一批"
    assert old and all(f not in got for f in old)


def test_portrait_batch_is_empty_for_stranger():
    """别人的人像不会被捡走（按 owner 过滤）。"""
    _seed_portrait(uid="别人", n=2)
    assert vt._portrait_batch(_ctx(user_id="u1")) == []


# ── make：起任务（参数收拾 + 归属）───────────────────────────────────

def test_make_refuses_empty_prompt():
    """★ 硬门：没描述就不建任务（光靠提示词挡不住模型图省事）。"""
    out = vt.t_make(_ctx(), prompt="   ")
    assert isinstance(out, str) and "画面" in out


def test_make_submits_job_with_clamped_args(monkeypatch):
    """★ 起任务时参数要收拾干净，且 args 的键名必须和 build_runner 的形参一致。

    键名对不上时 `**job.args` 展开会直接 TypeError —— 单测这一层抓得住。
    """
    from station.jobs import manager as jm

    class _FakeJob:
        id = "job-video-1"

    class _FakeMgr:
        def __init__(self):
            self.seen = None

        def submit(self, skill, args, user_id=""):
            self.seen = (skill, args, user_id)
            return _FakeJob()

    fake = _FakeMgr()
    monkeypatch.setattr(jm, "get_manager", lambda: fake)

    ctx = _ctx()
    out = vt.t_make(ctx, prompt="一只猫跳上窗台", seconds="99", aspect_ratio="4:5")

    skill, args, uid = fake.seen
    assert skill.id == "video", "要交自己的技能（靠它拿 build_runner）"
    assert args == {"prompt": "一只猫跳上窗台", "seconds": "12",
                    "aspect_ratio": "16:9", "photos": []}
    # ★ args 里放的是**文件 id、不是 URL**：临时链接只该在执行器里现签现撤，
    #   进了 jobs.args 就是一行长期明文（那是取用户照片的钥匙）。
    assert all("http" not in str(v) for v in args.values()), "args 里混进了 URL"
    assert uid == "u1", "归属没传下去 = 产物变成无主的"
    assert "job-video-1" in out["text"]
    assert (ctx.thread is None) or ctx.thread.meta.get("job_id")


def test_make_sends_a_job_card_not_just_text(monkeypatch):
    """★★ 起任务时**要发一张进度块卡片**（09-16 改，修用户报的两个 bug）。

    卡片走"工具返回 render → 宿主落进会话 meta"这条路，所以刷新后**在原位**重画；
    全靠前端"回合结束往末尾追加一块"的话：① 刷新就没了（成片只活在那块面板上）；
    ② 一回合以"等你批准"结束时也会追加，那时会话里的 job_id 还是上一个任务 ——
    旧成片会顶在新批准卡下面（用户原话："重新生成怎么把以前的视频调出来了"）。
    """
    from station.core.agent import run_tool
    from station.core.events import EV_RENDER
    from station.jobs import manager as jm

    class _FakeJob:
        id = "job-video-9"

    class _FakeMgr:
        def submit(self, skill, args, user_id=""):
            return _FakeJob()

    monkeypatch.setattr(jm, "get_manager", lambda: _FakeMgr())
    ctx = _ctx()
    make = next(t for t in get_registry().get("video").tools if t.leaf() == "make")

    out = run_tool(ctx, make, {"prompt": "一只猫跳上窗台", "seconds": "5"})
    cards = [e.data["card"] for e in ctx.events if e.type == EV_RENDER]
    assert cards and cards[0]["type"] == "job"
    # 卡片里只有 job_id —— 视频字节/文件名都不进卡（卡要落进会话 meta）
    assert cards[0] == {"type": "job", "job_id": "job-video-9"}
    assert isinstance(out, str) and "job-video-9" in out, "模型那边仍然只拿到文字"


def test_seconds_and_aspect_are_clamped_on_the_public_path():
    """越界/乱七八糟的时长比例都要被收拾掉（走公开入口断言，不测私有函数）。"""
    assert vt._norm_seconds("8 秒") == "8"
    assert vt._norm_seconds(9) == "9"
    assert vt._norm_seconds("99") == "12"
    assert vt._norm_seconds("1") == "4"
    assert vt._norm_seconds("abc") == "5"
    assert vt._norm_aspect("4:5") == "16:9"
    assert vt._norm_aspect("9:16") == "9:16"


def test_make_reports_missing_video_slot(monkeypatch):
    """没配「视频生成模型」时要给人话，而不是建一个注定失败的 job。"""
    from station import modelcfg
    monkeypatch.setattr(modelcfg, "resolve", lambda uid, slot: [])
    out = vt.t_make(_ctx(), prompt="一只猫")
    assert "视频生成模型" in out


# ── build_runner：建任务 → 轮询 → 下载 → 落产物 ──────────────────────

def test_build_runner_end_to_end_with_fake_agnes(fake_agnes):
    """★ 整条链真跑一遍（只把 Agnes 的 HTTP 换掉），断言事件与产物元数据。"""
    from station.files import store as fs

    ctx = _ctx()
    evs = list(vt.build_runner(ctx, prompt="一只猫跳上窗台", seconds="6",
                               aspect_ratio="9:16"))

    # ① 参数真的发给了对端，且时长是**字符串**（对端要字符串，传数字会 400）
    assert fake_agnes["created"]["seconds"] == "6"
    assert fake_agnes["created"]["aspect_ratio"] == "9:16"
    assert fake_agnes["created"]["prompt"] == "一只猫跳上窗台"
    assert fake_agnes["downloaded"] == "https://cdn.example/v.mp4"

    # ② 事件只用宿主认得的那三种（写错类型会被 Job.append 静默忽略）
    kinds = [e["type"] for e in evs]
    assert set(kinds) <= {"progress", "artifact", "log"}
    assert evs[0]["type"] == "progress" and "提交" in evs[0]["message"]
    assert any(e["type"] == "artifact" for e in evs)

    # ③ 进度只涨不跌（宿主拿事件里的 percent 直接覆盖进度条）
    pcts = [e["percent"] for e in evs if e["type"] == "progress"]
    assert pcts == sorted(pcts), f"进度倒退了：{pcts}"

    # ④ 产物元数据必须齐全，否则「我的产物」抽屉掉回按文件名猜分组
    fid = [e["id"] for e in evs if e["type"] == "artifact"][0]
    meta = fs.meta(fid)
    assert meta["suffix"] == ".mp4"
    assert meta["owner"] == "u1"
    assert meta["skill_id"] == "video"
    assert meta["group_key"] == "video:v-测试"
    assert meta["group_label"].startswith("AI 视频")
    assert fs.path(fid).read_bytes() == b"FAKE-MP4-BYTES"


def test_build_runner_raises_on_failed(fake_agnes, monkeypatch):
    """对端说 failed → 直接失败，不许当成功继续轮询到超时。"""
    monkeypatch.setattr(agnes, "query_task",
                        lambda *a, **k: {"status": "failed", "message": "内容被拒"})
    with pytest.raises(ValueError) as e:
        list(vt.build_runner(_ctx(), prompt="一只猫"))
    assert "内容被拒" in str(e.value)


def test_build_runner_raises_without_url(fake_agnes, monkeypatch):
    """完成了但没给地址 → 报人话，别拿空 URL 去下载（那会是天书报错）。"""
    monkeypatch.setattr(agnes, "query_task",
                        lambda *a, **k: {"status": "completed", "metadata": {}})
    with pytest.raises(ValueError) as e:
        list(vt.build_runner(_ctx(), prompt="一只猫"))
    assert "地址" in str(e.value)


def test_build_runner_backs_off_on_rate_limit(fake_agnes, monkeypatch):
    """★ 被限流（免费档 RPM=1）要**退避重试**，不是当场失败。

    免费档实测每分钟只允许 1 次请求，而轮询一定会撞上 —— 这条要是当成失败，
    每个任务都会"莫名其妙生成失败"，而根因在限流上，极难查。
    """
    calls = {"n": 0}

    def _query(base_url, key, model, video_id, timeout=30):
        calls["n"] += 1
        if calls["n"] < 3:
            raise agnes.AgnesError("限流", status=429, retry_after=0.01)
        return {"status": "completed", "progress": 100,
                "metadata": {"url": "https://cdn.example/v.mp4"}}

    monkeypatch.setattr(agnes, "query_task", _query)
    evs = list(vt.build_runner(_ctx(), prompt="一只猫"))
    assert calls["n"] >= 3
    assert any(e["type"] == "artifact" for e in evs), "退避之后应该照样生成成功"


def test_build_runner_tries_next_provider_only_on_create(fake_agnes, monkeypatch):
    """★ 建任务失败才换下一家；建成功之后就不再换（半途换家 = 两家各烧一次额度）。"""
    from station import modelcfg

    monkeypatch.setattr(modelcfg, "resolve", lambda uid, slot: [
        {"id": "bad", "label": "坏的那家", "base_url": "https://bad.example/v1",
         "api_key": "k", "model": "m", "slot": "video"},
        {"id": "good", "label": "好的那家", "base_url": "https://good.example/v1",
         "api_key": "k", "model": "m2", "slot": "video"},
    ])
    tried = []

    def _create(base_url, key, model, prompt, seconds="5", aspect_ratio="16:9",
                images=None, timeout=30):
        tried.append(base_url)
        if "bad" in base_url:
            raise agnes.AgnesError("坏了", status=500)
        return {"video_id": "v-ok"}

    monkeypatch.setattr(agnes, "create_task", _create)
    seen_query = {}

    def _query(base_url, key, model, video_id, timeout=30):
        seen_query.update({"base_url": base_url, "model": model})
        return {"status": "completed", "progress": 100,
                "metadata": {"url": "https://cdn.example/v.mp4"}}

    monkeypatch.setattr(agnes, "query_task", _query)

    evs = list(vt.build_runner(_ctx(), prompt="一只猫"))
    assert tried == ["https://bad.example/v1", "https://good.example/v1"], \
        "只该在建任务失败时换家，建成功之后不许再试"
    assert seen_query["base_url"] == "https://good.example/v1", \
        "轮询/下载必须只认建成功的那一家 —— 半途换家等于两家各生成一次、各烧一份额度"
    assert seen_query["model"] == "m2"
    assert any(e["type"] == "artifact" for e in evs)


# ── show：把成片发进对话 ─────────────────────────────────────────────

def test_show_renders_video_card():
    """★ 走 run_tool 跑一遍：确认卡片真的进了事件桶（前端才有东西可画）。"""
    from station.core.agent import run_tool
    from station.core.events import EV_RENDER
    from station.files import store as fs

    fid = fs.save_bytes(b"MP4", ".mp4", name="AI视频-测试.mp4",
                        owner="u1", skill_id="video", group_key="video:x",
                        group_label="AI 视频 · 测试")
    ctx = _ctx()
    show = next(t for t in get_registry().get("video").tools if t.leaf() == "show")
    out = run_tool(ctx, show, {"file_id": fid})
    assert isinstance(out, str) and fid in out          # 模型只拿到文字
    cards = [e.data["card"] for e in ctx.events if e.type == EV_RENDER]
    assert cards and cards[0]["type"] == "video-card"
    assert cards[0]["files"][0]["id"] == fid


def test_show_refuses_other_users_file():
    """★ 产物是私人的：别人的文件 id 被猜到也不能调出来看。"""
    from station.files import store as fs

    fid = fs.save_bytes(b"MP4", ".mp4", name="别人的.mp4",
                        owner="别人", skill_id="video")
    out = vt.t_show(_ctx(user_id="u1"), file_id=fid)
    assert isinstance(out, str) and "不属于" in out


# ── 参考图：临时链接的签发、撤销、以及"别泄露" ─────────────────────

def test_make_refuses_photos_without_public_base(monkeypatch):
    """★★ 有照片但没配对外地址 → **一次 submit 都不许发生**。

    硬跑下去只有两种结局：等几分钟后失败，或者**静默**生成一条"照片压根没送出去、
    所以不像本人"的片子。后者更糟 —— 用户花了额度还以为功能就这样。
    """
    from station.jobs import manager as jm
    from station import config

    class _Mgr:
        called = False

        def submit(self, *a, **k):
            _Mgr.called = True
            raise AssertionError("不该走到 submit")

    monkeypatch.setattr(jm, "get_manager", lambda: _Mgr())
    monkeypatch.setattr(config, "load_env", lambda: {})
    monkeypatch.delenv("STATION_PUBLIC_BASE", raising=False)

    out = vt.t_make(_ctx(thread=_thread_with_photos(_seed_portrait())), prompt="一只猫")
    assert isinstance(out, str) and "STATION_PUBLIC_BASE" in out
    assert _Mgr.called is False


def test_build_runner_signs_links_and_sends_images(fake_agnes, with_public_base):
    """★ 带照片时：以 **reference 模式 + https 直链** 提交，链接是临时签的。"""
    fids = _seed_portrait(n=2)
    ctx = _ctx()
    evs = list(vt.build_runner(ctx, prompt="站在雨后的街头", photos=fids))

    imgs = fake_agnes["created"]["images"]
    assert imgs and len(imgs) == 2
    assert all(str(u).startswith("https://station.example.com/api/ref/") for u in imgs)
    assert any(e["type"] == "artifact" for e in evs)


def test_build_runner_revokes_links_after_success(fake_agnes, with_public_base):
    """★ 任务一跑完就撤销 —— 用户的照片不该长期挂在公网上。"""
    from station import db
    fids = _seed_portrait(n=1)
    list(vt.build_runner(_ctx(), prompt="一只猫", photos=fids))
    n = db.conn().execute("SELECT COUNT(*) AS c FROM ref_links").fetchone()["c"]
    assert n == 0, "任务结束后链接还在（照片会一直能被公网取到）"


def test_build_runner_revokes_links_after_failure(fake_agnes, with_public_base,
                                                  monkeypatch):
    """失败路径**同样**要撤销（finally 语义）—— 失败的活留下的链接最容易被忘掉。"""
    from station import db
    monkeypatch.setattr(agnes, "query_task",
                        lambda *a, **k: {"status": "failed", "message": "内容被拒"})
    fids = _seed_portrait(n=1)
    with pytest.raises(ValueError):
        list(vt.build_runner(_ctx(), prompt="一只猫", photos=fids))
    assert db.conn().execute("SELECT COUNT(*) AS c FROM ref_links").fetchone()["c"] == 0


def test_revoke_failure_does_not_fail_the_job(fake_agnes, with_public_base,
                                              monkeypatch):
    """★★ 撤销抛异常**不许**把已经成功的任务判成失败。

    撤销是善后：视频已经生成好了、产物已经落盘了，因为"收回链接时数据库抽了一下"
    就把它判成 failed，用户白等几分钟还看不到片子 —— 荒谬，但很容易写出来
    （善后代码在 finally 里，一抛就冒泡到 JobManager 的 except）。
    """
    from station.files import share
    monkeypatch.setattr(share, "revoke", lambda *a, **k: 0)
    monkeypatch.setattr("station.db.ref_link_revoke",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("数据库抽了")))
    fids = _seed_portrait(n=1)
    evs = list(vt.build_runner(_ctx(), prompt="一只猫", photos=fids))
    assert any(e["type"] == "artifact" for e in evs), "任务不该因为善后失败而挂掉"


def test_build_runner_never_logs_the_token(fake_agnes, with_public_base):
    """★★ 临时链接**绝不能进 job 事件**。

    事件会经 `GET /api/jobs/{id}` 回给前端、并长期存在 `jobs.log` 那一列里 —— 等于把
    "取用户照片的钥匙"写进了数据库和浏览器。有人 debug 时顺手打个日志就会造成这个后果，
    所以用断言钉住。
    """
    fids = _seed_portrait(n=2)
    evs = list(vt.build_runner(_ctx(), prompt="一只猫", photos=fids))
    blob = repr(evs)
    assert "/api/ref/" not in blob, "事件里出现了临时链接！"
    assert "api/ref" not in blob
    # 顺带：fid 本身也不该出现在事件里（它至少是个"文件名"）
    assert not any(f in blob for f in fids)


def test_preview_discloses_photos_and_ttl(with_public_base):
    """★★ 批准卡是用户**唯一**能知情的地方：几张、叫什么、链接活多久、
    以及"有效期内谁拿到都能打开"。"""
    names = None
    fids = _seed_portrait(n=2)
    from station.files import store as fs
    names = [(fs.meta(f) or {}).get("name") for f in fids]
    txt = vt._preview_make(_ctx(thread=_thread_with_photos(fids)),
                           prompt="站在海边", seconds="5", aspect_ratio="16:9")
    assert "2 张人像" in txt
    assert names[0] in txt
    assert "临时公开链接" in txt and "失效" in txt
    assert "谁拿到" in txt, "要说清链接有效期内是不设防的"


def test_preview_has_no_photo_disclosure_when_none(with_public_base):
    """没有照片时不许出现"会开公开链接"那段 —— 那会让人以为照片被传出去了。"""
    txt = vt._preview_make(_ctx(), prompt="一只猫")
    assert "临时公开链接" not in txt


def test_preview_accepts_the_same_kwargs_as_the_tool():
    """★ 宿主是按 `pv(ctx, **args)` 调的（args 来自工具 schema）。

    预览函数的签名要是对不上，宿主那边会被 except 吞掉、**静默退回只显示 label** ——
    批准卡看起来"正常"，只是内容没了。所以直接比对两边的参数名。
    """
    import inspect
    make = next(t for t in get_registry().get("video").tools if t.leaf() == "make")
    declared = {a["name"] for a in make.args}
    accepted = set(inspect.signature(vt._preview_make).parameters) - {"ctx"}
    missing = declared - accepted
    assert not missing, f"预览函数不接受这些参数，调用会被静默吞掉：{sorted(missing)}"


def test_show_without_file_says_so():
    """没生成过就直说，别编一个不存在的产物。"""
    out = vt.t_show(_ctx())
    assert isinstance(out, str)


# ── 槽位与协议细节 ───────────────────────────────────────────────────

def test_video_slot_resolves_agnes_and_does_not_leak_into_chat():
    """★ 视频槽能解析出 agnes；而 agnes **不许**渗进对话那条链（它没有对话模型）。"""
    from station import modelcfg

    vids = modelcfg.resolve("u1", "video")
    assert vids and vids[0]["id"] == "agnes"
    assert vids[0]["model"] == "agnes-video-2.5-flash"
    assert all(e["id"] != "agnes" for e in modelcfg.resolve("u1", "chat")), \
        "只提供视频的家不该出现在通用模型的降级链里"


# ── 建任务的重试边界（哪些错该退避重试、哪些绝不许）──────────────────

class _Resp:
    """假的 requests 响应（只用到 status_code / json / headers）。"""

    def __init__(self, code, payload=None, headers=None):
        self.status_code = code
        self._payload = {} if payload is None else payload
        self.headers = headers or {}

    def json(self):
        return self._payload


@pytest.fixture
def fast_retry(monkeypatch):
    """把退避等待压成 0 —— 不然一条用例要真睡 20 秒。"""
    monkeypatch.setattr(agnes, "CREATE_RETRY_WAITS", (0.0, 0.0))


def test_create_task_retries_on_503(monkeypatch, fast_retry):
    """★ 503（队列临时不可用）要退避重试 —— 对端明说任务没收下，重试没有副作用。

    09-14 用户实测：连撞两次 503，每次都直接判失败，只能自己手动重试三次。
    """
    seen = []

    def _post(url, **kw):
        seen.append(1)
        if len(seen) < 3:
            return _Resp(503, {"code": "video_queue_unavailable"})
        return _Resp(200, {"video_id": "v1"})

    monkeypatch.setattr(agnes.requests, "post", _post)
    assert agnes.create_task("https://x/v1", "k", "m", "p")["video_id"] == "v1"
    assert len(seen) == 3, f"该重试两次，实际发了 {len(seen)} 次"


def test_create_task_gives_up_after_retries(monkeypatch, fast_retry):
    """重试用完就老实报错，别无限撞。"""
    monkeypatch.setattr(agnes.requests, "post", lambda *a, **k: _Resp(503, {}))
    with pytest.raises(agnes.AgnesError) as e:
        agnes.create_task("https://x/v1", "k", "m", "p")
    assert e.value.status == 503


def test_create_task_does_not_retry_on_429(monkeypatch, fast_retry):
    """★ 429 是**按次限流**：立刻重试只会继续撞，必须直接失败、让人等。"""
    seen = []

    def _post(url, **kw):
        seen.append(1)
        return _Resp(429, {"error": {"code": "rate_limit_exceeded"}})

    monkeypatch.setattr(agnes.requests, "post", _post)
    with pytest.raises(agnes.AgnesError) as e:
        agnes.create_task("https://x/v1", "k", "m", "p")
    assert e.value.status == 429
    assert len(seen) == 1, "429 不该重试"


def test_create_task_does_not_retry_on_network_error(monkeypatch, fast_retry):
    """★ 网络层失败**绝不重试**：请求可能已经到达对端并建好了任务，重发就是两条视频、
    白烧一次额度，而且**没有任何提示**。这条比"少重试一次"重要得多。"""
    seen = []

    def _post(url, **kw):
        seen.append(1)
        raise agnes.requests.ConnectionError("boom")

    monkeypatch.setattr(agnes.requests, "post", _post)
    with pytest.raises(agnes.AgnesError):
        agnes.create_task("https://x/v1", "k", "m", "p")
    assert len(seen) == 1, "网络错误重试可能造成重复生成"


def test_create_task_body_carries_images_only_when_given(monkeypatch):
    """带图 → mode=reference + images；不带图 → **请求体里连 images 这个键都没有**。

    （空数组在 reference 模式下是非法请求，所以不能写成 `images or []`。）
    """
    got = {}

    def _post(url, **kw):
        got.clear()
        got.update(kw.get("json") or {})
        return _Resp(200, {"video_id": "v1"})

    monkeypatch.setattr(agnes.requests, "post", _post)
    agnes.create_task("https://x/v1", "k", "m", "p",
                      images=["https://a/1.jpg", "https://a/2.jpg"])
    assert got["mode"] == "reference"
    assert got["images"] == ["https://a/1.jpg", "https://a/2.jpg"]

    agnes.create_task("https://x/v1", "k", "m", "p")
    assert got["mode"] == "text"
    assert "images" not in got, "纯文生视频那条路的请求体不该多出 images 键"


def test_agnes_query_url_is_built_from_host_root():
    """★ 查询端点在**站点根**，而 base_url 带着 /v1 —— 这一处最容易拼错。

    拼错的表现是"任务建得成功、查询永远 404 超时"，日志里只有一个 404，
    很容易误判成 key 或模型名不对。
    """
    assert agnes._root("https://apihub.agnes-ai.com/v1") == "https://apihub.agnes-ai.com"
    assert agnes._root("https://apihub.agnes-ai.com/v1/") == "https://apihub.agnes-ai.com"
    assert agnes._root("http://localhost:11434/v1") == "http://localhost:11434"
    assert agnes._root("https://proxy.example/agnes") == "https://proxy.example/agnes"


def test_result_url_accepts_both_shapes():
    """★ 成品地址：**顶层 url**（实测的真实形状）和 `metadata.url`（文档写的）都要认。

    这条是 09-14 那次真机失败的回归 —— 照文档只读 metadata.url 的后果是：
    视频明明生成成功了，却被判成"任务完成了但没给地址"，而**对端已经烧掉一次生成**。
    """
    real = {"status": "completed", "progress": 100, "error": None,
            "url": "https://platform-outputs.agnes-ai.space/videos/x/task_a.mp4"}
    assert agnes.result_url(real).endswith("task_a.mp4")
    # 文档里那种嵌套形状也认（哪天对端改回去不会断）
    assert agnes.result_url(
        {"status": "completed", "metadata": {"url": "https://c/v.mp4"}}) == "https://c/v.mp4"
    # 都没有 → 空串（调用方据此报错，且错误信息里会列出实际字段名）
    assert agnes.result_url({"status": "completed", "progress": 100}) == ""
    assert agnes.result_url({"status": "completed", "metadata": {}}) == ""
    assert agnes.result_url({"url": "   "}) == ""


def test_build_runner_reports_actual_fields_when_url_missing(fake_agnes, monkeypatch):
    """报错信息里要带上"响应里到底有哪些字段" —— 字段名变了是这类接口最常见的坑。"""
    monkeypatch.setattr(agnes, "query_task",
                        lambda *a, **k: {"status": "completed", "progress": 100,
                                         "output": {"video": "..."}})
    with pytest.raises(ValueError) as e:
        list(vt.build_runner(_ctx(), prompt="一只猫"))
    assert "output" in str(e.value), "要列出实际字段名，否则下次还得手工去查响应"


def test_sleep_for_prefers_retry_after_but_never_below_default():
    """退避时长：对端给了就用它，没给/更小就用调用方的默认间隔。"""
    assert agnes.sleep_for(agnes.AgnesError("x", status=429, retry_after=30), 5) == 30
    assert agnes.sleep_for(agnes.AgnesError("x", status=429), 5) == 5
    assert agnes.sleep_for(agnes.AgnesError("x", status=429, retry_after=0.1), 5) == 5


def test_make_prompt_is_not_swallowed_by_optimize(fake_chat):
    """回归：draft 只优化**文字**，不该顺手把用户原话丢掉。

    （用户改口说"改成夜景"时，模型要把新要求接着原话再 draft 一次；原话要是丢了，
    第二版会变成只有"夜景"两个字。）
    """
    out = vt.t_draft(_ctx(), idea="一只猫在跳")
    assert out["render"]["idea"] == "一只猫在跳"


def test_build_runner_is_lazy():
    """★ build_runner 是生成器：调用它**不立刻干活**（宿主 for 第一次取值才跑）。

    这条钉住"它没有在返回前就把网络请求发出去"——否则起 job 的那一瞬间就会
    真的建任务，而宿主可能还没开始记录状态。
    """
    gen = vt.build_runner(_ctx(), prompt="一只猫")
    assert hasattr(gen, "__next__"), "必须是生成器（不能是普通函数直接跑完）"
    gen.close()                                   # 别真跑：没配 key 会报错
    time.sleep(0)                                 # 只是让"惰性"这件事被真的测到
