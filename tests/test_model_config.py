"""模型配置层测试（station/modelcfg.py + /api/modelconfig 背后的那套逻辑）。

这一层最怕两件事，所以都单独立了护栏：
  1) **改动把老行为改坏** —— 没配过的人原来是什么样，改造后必须还是什么样
     （默认链首读 env、库里不能凭空多出一行）；
  2) **明文 key 泄到前端** —— GET 的响应里必须连 api_key 这个字段名都不出现。
"""
from __future__ import annotations

import json

import pytest

from station import db, modelcfg

# 三个槽位的固定顺序（跟 SLOT_LABELS 对齐，改这里要一起改）。
# ★ 09-11 起没有 "text" 槽了：它和 "chat" 取的是同一个 text_model，合并成一个。
SLOTS = ("chat", "route", "vision")


def _slots(*pids: str) -> dict:
    """四个槽位都用同一条链（测试通常只关心其中某一个）。"""
    return {s: list(pids) for s in SLOTS}


def _p(pid: str, **kw) -> dict:
    """造一个 provider 条目（默认字段齐全，便于被 _validate 放行）。"""
    p = {"id": pid, "label": pid, "base_url": "https://x.example/v1",
         "text_model": "t-" + pid, "vision_model": "v-" + pid,
         "route_model": "", "key_env": ""}
    p.update(kw)
    return p


# ★ 09-11 起 .env 里的密钥**只兜底给站长**（最早注册的账号，见 db.is_owner）。
#   所以凡是要验证"回落到 .env"的测试，用的 user_id 必须是**库里真有的第一个用户**——
#   随手编的 "u1" 在库里查无此人，会被判成非站长、永远拿不到 .env。
@pytest.fixture
def owner() -> str:
    """建一个真用户并让他当上站长（库里第一个 = 最早注册的 = owner）。"""
    return db.create_user("站长", "pw")["id"]


@pytest.fixture
def guest(owner) -> str:
    """再建一个：注册得晚，不是站长，拿不到 .env 兜底。依赖 owner 保证顺序。"""
    return db.create_user("访客", "pw")["id"]


# ── ① 没配置 = 行为不变（零迁移的护栏）────────────────────────────────

def test_no_config_means_builtin_defaults(monkeypatch):
    """验证"从没配过"的人拿到的就是我们内置的默认，且**不往库里写东西**。"""
    monkeypatch.setenv("TEXT_CHANNEL", "glm")
    monkeypatch.setenv("VISION_CHANNEL", "qwen")
    monkeypatch.setenv("ROUTE_CHANNEL", "qwen-flash")

    cfg = modelcfg.load_config("u1")
    assert [p["id"] for p in cfg["providers"]] == ["glm", "deepseek", "qwen"]
    assert cfg["slots"]["chat"] == ["glm"]
    assert cfg["slots"]["vision"] == ["qwen"]
    # 只认三个槽位：老的 slots.text 不该在新配置里冒出来
    assert set(cfg["slots"]) == {"chat", "route", "vision"}
    # route 默认必须还是"用 qwen 家的 qwen-flash 模型"——与改造前 ROUTE_CHANNEL 完全一致
    assert cfg["slots"]["route"] == ["qwen"]
    assert [e["model"] for e in modelcfg.resolve("u1", "route")] == ["qwen-flash"]
    # ★ 只是"读"，不能合成一行落库：否则每个用户一登录库就长出一堆默认配置
    assert db.model_config_load("u1") is None


def test_env_still_steers_default_chain_head(monkeypatch):
    """验证 .env 里的通道选择仍然管用（以前只能靠它，现在它是"默认值的默认值"）。"""
    monkeypatch.setenv("TEXT_CHANNEL", "deepseek")
    monkeypatch.setenv("VISION_CHANNEL", "deepseek")
    cfg = modelcfg.load_config("u1")
    assert cfg["slots"]["chat"] == ["deepseek"]
    assert cfg["slots"]["vision"] == ["deepseek"]


def test_route_legacy_alias_and_unknown_falls_back(monkeypatch):
    """验证老写法 qwen-flash 被正确映射，且通道名写错时**回默认而不是崩**。

    这条不能松：.env.example 里写着 ROUTE_CHANNEL=qwen-flash，用户本机随时可能填它；
    没有别名映射的话判词会静默失效（不报错，只是每次都降级给主 agent，变慢变贵）。
    """
    monkeypatch.setenv("ROUTE_CHANNEL", "qwen-flash")
    assert modelcfg.load_config("u1")["slots"]["route"] == ["qwen"]
    monkeypatch.setenv("ROUTE_CHANNEL", "根本没有这家")
    assert modelcfg.load_config("u1")["slots"]["route"] == ["qwen"]


# ── ② key 三态 + 掩码（安全护栏）──────────────────────────────────────

def test_key_tristate(monkeypatch):
    """验证 key 的三种写法：不给=不改 / 空串=清空 / 非空=覆盖。"""
    monkeypatch.setenv("GLM_API_KEY", "")          # 让 env 侧为空，好区分来源

    base = {"providers": [_p("glm")], "slots": _slots("glm")}

    with_key = json.loads(json.dumps(base))
    with_key["providers"][0]["api_key"] = "sk-明文AAA"
    modelcfg.save_config("u1", with_key)
    assert modelcfg.mask_config(modelcfg.load_config("u1"))["providers"][0]["key_source"] == "db"

    # 不带 api_key 字段 → 保留库里的那份（前端"留空不改"）
    modelcfg.save_config("u1", json.loads(json.dumps(base)))
    assert modelcfg.load_config("u1")["providers"][0]["api_key"] == "sk-明文AAA"

    # 空串 → 显式清空（前端点"清除 key"）
    clear = json.loads(json.dumps(base))
    clear["providers"][0]["api_key"] = ""
    modelcfg.save_config("u1", clear)
    assert modelcfg.load_config("u1")["providers"][0]["api_key"] == ""
    assert modelcfg.mask_config(modelcfg.load_config("u1"))["providers"][0]["key_source"] == "none"


def test_saved_key_wins_over_env(monkeypatch, owner):
    """★ 验证"填了就优先用你填的那把"：库里存了 key 就不再回落到 src/.env。"""
    monkeypatch.setenv("GLM_API_KEY", "sk-来自env的")
    # key_env 要指向那个环境变量名，才有"回落"可言（内置三家就是这么设的）
    p = _p("glm", key_env="GLM_API_KEY")
    modelcfg.save_config(owner, {"providers": [dict(p, api_key="sk-我填的")],
                                 "slots": _slots("glm")})
    assert modelcfg.resolve(owner, "chat")[0]["api_key"] == "sk-我填的"
    # 清掉填的那把 → 又回落到 .env（这才是"留空 = 用环境变量"该有的样子）
    modelcfg.save_config(owner, {"providers": [dict(p, api_key="")],
                                 "slots": _slots("glm")})
    assert modelcfg.resolve(owner, "chat")[0]["api_key"] == "sk-来自env的"


# ── ②' .env 兜底只给站长（09-11 用户拍板）─────────────────────────────
# 站是站长掏钱开的，.env 里那几把只该他用。别人必须自带密钥，否则访客在花站长的钱。

def test_env_key_only_for_owner(monkeypatch, owner, guest):
    """★ 同一份配置、同一个 .env，站长解析出 key、访客解析出空串。"""
    monkeypatch.setenv("GLM_API_KEY", "sk-站长的env密钥")
    cfg = {"providers": [_p("glm", key_env="GLM_API_KEY", api_key="")],
           "slots": _slots("glm")}
    modelcfg.save_config(owner, cfg)
    modelcfg.save_config(guest, cfg)

    assert modelcfg.resolve(owner, "chat")[0]["api_key"] == "sk-站长的env密钥"
    assert modelcfg.resolve(owner, "chat")[0]["allow_env"] is True
    # 访客：拿不到 .env，且这条 entry 要带上"不许读 env"的标记
    # （Model 靠它选报错文案 —— 跟访客提 src/.env 只会让他一头雾水）
    assert modelcfg.resolve(guest, "chat")[0]["api_key"] == ""
    assert modelcfg.resolve(guest, "chat")[0]["allow_env"] is False


def test_guest_own_key_still_works(monkeypatch, owner, guest):
    """访客拿不到 .env，但**自己填的那把**必须照常生效（别把兜底和正路一起砍了）。"""
    monkeypatch.setenv("GLM_API_KEY", "sk-站长的env密钥")
    modelcfg.save_config(guest, {"providers": [_p("glm", key_env="GLM_API_KEY",
                                                  api_key="sk-访客自己填的")],
                                 "slots": _slots("glm")})
    assert modelcfg.resolve(guest, "chat")[0]["api_key"] == "sk-访客自己填的"


def test_guest_sees_no_key_in_ui_payload(monkeypatch, owner, guest):
    """界面那一份：访客看到的是"还没填密钥"（黄点），而不是站长 .env 里的那把。

    这条同时钉住"明文别泄"——访客的响应里不能出现站长 .env 的密钥。
    """
    monkeypatch.setenv("GLM_API_KEY", "sk-站长的env密钥")
    cfg = {"providers": [_p("glm", key_env="GLM_API_KEY", api_key="")],
           "slots": _slots("glm")}
    modelcfg.save_config(guest, cfg)

    got = modelcfg.mask_config(modelcfg.load_config(guest),
                               reveal=True, allow_env=False)["providers"][0]
    assert got["has_key"] is False and got["key_source"] == "none"
    assert got["api_key"] == ""
    blob = json.dumps(modelcfg.mask_config(modelcfg.load_config(guest),
                                           reveal=True, allow_env=False),
                      ensure_ascii=False)
    assert "sk-站长的env密钥" not in blob


def test_legacy_text_slot_in_stored_config_is_dropped(owner):
    """老库里存着 slots.text（09-11 之前配的）也不能炸，读出来只留三个槽位。

    这是真机上一定会走到的迁移路径：用户改版前保存过配置，库里就有这个键。
    """
    db.model_config_save(owner, {
        "version": modelcfg.CURRENT_VERSION,
        "providers": [_p("glm", key_env="GLM_API_KEY", api_key="sk-老的")],
        "slots": {"chat": ["glm"], "route": ["glm"],
                  "vision": ["glm"], "text": ["glm"]}})     # ← 直接写原始配置，绕过 save_config
    cfg = modelcfg.load_config(owner)
    assert set(cfg["slots"]) == {"chat", "route", "vision"}
    assert cfg["slots"]["chat"] == ["glm"]
    # 老配置里的 key 不能被顺手丢掉
    assert modelcfg.load_config(owner)["providers"][0]["api_key"] == "sk-老的"


def test_no_user_id_still_reads_env():
    """★ 无登录态的路径（archive CLI / 后台 job）必须继续认 .env。

    conventions 写着「无用户 → 走 .env 默认」，这条别被"只给站长"的改造顺手砍掉。
    """
    assert db.is_owner("") is True
    assert db.is_owner("查无此人") is False


def test_mask_hides_key_unless_revealed(monkeypatch):
    """★ 钉住"默认不外泄、显式 reveal 才带出来"。

    默认那一份（给日志/调试）里连 `api_key` 这个字段名都不该出现；
    reveal=True 那一份（给界面"显示"按钮用）才带**实际会用的那把** ——
    用户自己存的优先，没有才轮到 src/.env。别把 reveal 的默认值改成 True。
    """
    monkeypatch.setenv("GLM_API_KEY", "sk-来自env的")
    cfg = {"providers": [_p("glm", key_env="GLM_API_KEY", api_key="sk-绝密-9f3a2b")],
           "slots": _slots("glm")}
    modelcfg.save_config("u1", cfg)

    blob = json.dumps(modelcfg.mask_config(modelcfg.load_config("u1")),
                      ensure_ascii=False)
    assert "sk-绝密" not in blob and "9f3a2b" not in blob
    assert "sk-来自env的" not in blob
    assert "api_key" not in blob                        # 连字段名都不给
    assert json.loads(blob)["providers"][0]["has_key"] is True   # 但要说"配过了"

    # 界面用的那一份：带出实际会用的那把（存过的优先）
    got = modelcfg.mask_config(modelcfg.load_config("u1"),
                               reveal=True)["providers"][0]
    assert got["api_key"] == "sk-绝密-9f3a2b" and got["key_source"] == "db"

    # 没存过 → 带出 .env 里读到的
    modelcfg.save_config("u1", {"providers": [_p("glm", key_env="GLM_API_KEY",
                                                 api_key="")],
                                "slots": _slots("glm")})
    got = modelcfg.mask_config(modelcfg.load_config("u1"),
                               reveal=True)["providers"][0]
    assert got["api_key"] == "sk-来自env的" and got["key_source"] == "env"


# ── ③ 校验与容错 ──────────────────────────────────────────────────────

def test_unused_provider_may_be_incomplete():
    """★ 验证"还没用上的家"允许是半成品。

    用户的心智是"我先把密钥存下来，回头再配" —— 如果他刚加的一家还没填地址/模型名、
    也还没被任何一行选进去，保存就不该被拦（真踩过：表现是"我加了密钥却不能保存"）。
    """
    cfg = {"providers": [_p("glm"),
                         {"id": "custom-draft", "label": "还没配好",
                          "base_url": "", "text_model": "", "vision_model": "",
                          "route_model": "", "key_env": "", "api_key": "sk-先存着"}],
           "slots": _slots("glm")}          # 只用了 glm，草稿那家没被用上
    out = modelcfg.save_config("u1", cfg)
    assert "custom-draft" in {p["id"] for p in out["providers"]}
    assert modelcfg.load_config("u1")["providers"][1]["api_key"] == "sk-先存着"

    # 但一旦把它选进某一行，内容就必须补齐了
    bad = {"providers": cfg["providers"], "slots": _slots("glm", "custom-draft")}
    with pytest.raises(ValueError) as e:
        modelcfg.save_config("u1", bad)
    assert "接口地址" in str(e.value)


def test_draft_provider_dropped_from_chain_still_saves():
    """验证"加了草稿家 → 又被移出链"之后仍然能保存（别留下解不开的死结）。"""
    cfg = {"providers": [_p("glm"),
                         {"id": "custom-x", "label": "草稿", "base_url": "",
                          "text_model": "", "vision_model": "", "route_model": "",
                          "key_env": "", "api_key": ""}],
           "slots": _slots("glm")}
    assert modelcfg.save_config("u1", cfg)


def test_save_strips_dangling_ids():
    """验证删 provider 后链里的悬空 id 被静默剔除（坏配置不该让整个对话不可用）。"""
    cfg = {"providers": [_p("glm")], "slots": _slots("glm", "ghost")}
    out = modelcfg.save_config("u1", cfg)
    assert out["slots"]["chat"] == ["glm"]


@pytest.mark.parametrize("bad,needle", [
    (lambda: {"providers": [_p("glm", base_url="ftp://x")], "slots": _slots("glm")},
     "接口地址"),
    (lambda: {"providers": [_p("glm", vision_model="")], "slots": _slots("glm")},
     "识图模型"),
    (lambda: {"providers": [_p("x'y")], "slots": _slots("x'y")}, "内部标识"),
    (lambda: {"providers": [_p("glm"), _p("glm")], "slots": _slots("glm")}, "重复"),
    (lambda: {"providers": [_p("glm", text_model="")], "slots": _slots("glm")}, "通用模型"),
], ids=["非法接口地址", "看图列缺模型名", "标识字符集", "标识重复", "缺模型名"])
def test_save_rejects_bad_config_with_readable_reason(bad, needle):
    """验证校验失败抛 ValueError 且**文案是人话**（要直接显示在弹窗里给用户看）。"""
    with pytest.raises(ValueError) as e:
        modelcfg.save_config("u1", bad())
    assert needle in str(e.value)


def test_builtin_backfill():
    """验证库里存的旧配置会自动补上"代码里新加的内置家"（用户没动过它就用默认值）。"""
    db.model_config_save("u1", {"version": 1, "providers": [_p("glm")],
                                "slots": _slots("glm")})
    got = modelcfg.load_config("u1")
    assert {p["id"] for p in got["providers"]} == {"glm", "deepseek", "qwen"}


def test_unknown_version_falls_back_with_warn():
    """验证配置版本比程序新时不猜语义：回默认 + 带 warn 让前端提示，而不是抛异常。"""
    db.model_config_save("u1", {"version": 99, "providers": [_p("glm")],
                                "slots": _slots("glm")})
    got = modelcfg.load_config("u1")
    assert "warn" in got
    assert {p["id"] for p in got["providers"]} == {"glm", "deepseek", "qwen"}


def test_delete_restores_defaults():
    """验证"恢复默认"就是把那一行删掉，下次读又回到内置默认。"""
    modelcfg.save_config("u1", {"providers": [_p("glm", label="我改过的")],
                                "slots": _slots("glm")})
    assert modelcfg.load_config("u1")["providers"][0]["label"] == "我改过的"
    modelcfg.delete_config("u1")
    assert db.model_config_load("u1") is None
    assert modelcfg.load_config("u1")["providers"][0]["label"] == "智谱 GLM"


# ── ④ 识别侧真的按"当前用户"的配置取模型 ──────────────────────────────

def test_build_runner_uses_user_chain(tmp_path, monkeypatch):
    """★ 验证识别 job 取的是**发起人**的模型配置，不是 .env 默认。

    建档走「识别·建档(视觉)」槽、定类走「识别·定类(文本)」槽 —— 这里把视觉槽
    指到一个自定义 provider，看 station_adapter 是否把这条链交给了 providers。
    """
    import io

    from PIL import Image

    import archive.station_adapter as ad
    from archive.engine import providers
    from archive.storage.project import create_project
    from station.core.context import Context

    monkeypatch.setenv("GLM_API_KEY", "")
    # ★ 故意让 chat 和（已废弃的）text 指向**不同的家**：定类必须走 chat。
    #   要是哪天有人把 station_adapter 改回 resolve(uid,"text")，这里立刻红。
    modelcfg.save_config("u1", {
        "providers": [_p("glm"),
                      _p("custom-abc", label="本地", key_env="")],
        "slots": {"chat": ["glm"], "route": ["glm"],
                  "vision": ["custom-abc"], "text": ["custom-abc"]}})

    seen: dict = {}
    flow: dict = {}

    def fake_make_model(kind, **kw):
        seen[kind] = kw.get("entries")
        return object()                    # 占位客户端：真正去跑之前就被下面拦住

    def fake_flow(records, llm_text, llm_vision, vision_channel="", sink=None,
                  refresh=False):
        # 签名要与 _iter_flow 对齐：第 5 个参数是"回填最终状态"的可变 dict（见其 docstring）
        flow["vision_channel"] = vision_channel
        return iter(())                    # 空流程：不跑 LangGraph、不联网

    monkeypatch.setattr(providers, "make_model", fake_make_model)
    monkeypatch.setattr(ad, "_iter_flow", fake_flow)

    # 造一个 1 页真项目（create_project 会用 PIL 读图算 phash，假字节不行）
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="JPEG")
    src = tmp_path / "in"; src.mkdir()
    (src / "1.jpg").write_bytes(buf.getvalue())
    pj = create_project("测试卷", str(src), str(tmp_path))

    ctx = Context(data_dir=tmp_path / "skilldata", user_id="u1")
    list(ad.build_runner(ctx, project=pj))               # 能走完，不联网

    assert [e["id"] for e in seen["vision"]] == ["custom-abc"]   # 按用户配的来
    # 定类/合并走「通用模型」槽（chat），**不是**那个已废弃的 text 槽
    assert [e["id"] for e in seen["text"]] == ["glm"]
    # ★ 建档链的标签要传下去 —— OCR 缓存键靠它，不传就会出现"换了 provider
    #   却仍命中上一家缓存"的静默错误结果。
    assert flow["vision_channel"] == "custom-abc"


def test_chain_label_is_chain_head():
    """验证给 OCR 缓存用的链标签取链首 id（换 provider 就换标签，不会串味）。"""
    assert modelcfg.chain_label([{"id": "glm"}, {"id": "qwen"}]) == "glm"
    assert modelcfg.chain_label([]) == ""


# ── 两份 provider 表的漂移护栏 ───────────────────────────────────────

# 两边字段名的对应关系：archive 侧字段 → station(modelcfg) 侧字段。
_FIELDS = (("base_url", "base_url"), ("key", "key_env"),
           ("text", "text_model"), ("vision", "vision_model"))

# ★ 已知、且**没核实过**的差异。见 docs/status.md 挂起问题「deepseek 的模型名两处不一致」
#   —— 没带 key 实测过哪个名字对，所以**不许**擅自统一（统一错了 = 降级到 deepseek 时
#   静默失败）。这里只登记、不修。
#   这张例外表是**自毁式**的：哪天实测出正确名字、两边改成一致了，断言会因为
#   "差异消失了"而变红，提示你把这条删掉 —— 删掉之后护栏就覆盖它了。
_KNOWN_DIFF = {("deepseek", "text"), ("deepseek", "vision")}


def _provider_diff() -> set:
    """算出两份表**实际**不一致的字段集合，元素形如 ("deepseek", "text")。"""
    from archive.engine import providers
    station_by_id = {p["id"]: p for p in modelcfg.BUILTIN_PROVIDERS}
    out = set()
    if set(providers.PROVIDERS) != set(station_by_id):
        out.add(("__providers__", "只有一边有的服务商"))
    for pid, arc in providers.PROVIDERS.items():
        st = station_by_id.get(pid)
        if st is None:
            continue
        for a, b in _FIELDS:
            if (arc.get(a) or "") != (st.get(b) or ""):
                out.add((pid, a))
    return out


def test_provider_tables_do_not_drift():
    """archive 的 PROVIDERS 表与 station 的 BUILTIN_PROVIDERS **不许漂移**。

    为什么是"两份表 + 护栏"而不是"合并成一份"：两边都要能**独立跑**（宿主 Web 一份、
    archive 的 CLI / 将来的 MCP 进程一份），合并就得让 archive import station，跟
    "技能要能搬走"的目标反着走。但地址、密钥环境变量、模型名是**同一批服务商**，
    必须一致 —— 这两张表历史上真漂移过（同是 deepseek，一边 deepseek-flash、
    一边 deepseek-v4-flash），正是当初引入 modelcfg 统一配置的起因。
    """
    diff = _provider_diff()
    new = diff - _KNOWN_DIFF            # 新出现的差异 = 有人只改了其中一边
    gone = _KNOWN_DIFF - diff           # 已消失的差异 = 那条悬案被解决了
    assert not new and not gone, (
        "\n  provider 表的差异变了："
        f"\n    新出现（去统一这两边）：{sorted(new)}"
        f"\n    已消失（请从 _KNOWN_DIFF 里删掉这些）：{sorted(gone)}"
        "\n  背景见 docs/status.md 挂起问题「deepseek 的模型名两处不一致」")


def test_provider_drift_guard_actually_bites():
    """护栏自检：把 archive 侧某家的字段改坏，_provider_diff 必须报出来。

    为什么单独测一条：这个护栏**如今是绿的**（差异恰好等于登记的那两条），而"绿"
    有两种可能 —— 真的没漂移，还是它根本没在比对。这里临时改一份表把第二种可能
    排除掉：摘掉比对逻辑，这条必红。
    """
    import archive.engine.providers as pv
    original = pv.PROVIDERS["glm"]
    try:
        pv.PROVIDERS["glm"] = {**original, "base_url": "https://example.invalid/v1"}
        assert ("glm", "base_url") in _provider_diff(), "改了地址却报不出来=护栏没在比对"
        # 模型名那一列也要是真的在比（挑一个不在例外表里的）
        pv.PROVIDERS["glm"] = {**original, "vision": "改坏的名字"}
        assert ("glm", "vision") in _provider_diff()
    finally:
        pv.PROVIDERS["glm"] = original      # 还原，别污染别的用例
