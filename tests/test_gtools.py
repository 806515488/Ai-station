"""全局工具（station/tools/）的离线单测 —— 不联网、不烧 key、不碰真数据。

新手视角（Java 朋友版）：这一组只覆盖 09-13 精简后**剩下的 4 个**全局工具
（记长期记忆 / 查产物 / 报能力），外加一条"**别把文件工具加回来**"的回归护栏。

★ 09-13 删掉的东西，测试也跟着删了（连同它们守的那批用例）：
  · `ls` / `read` / `grep` / `write`（原来的 tools/fs.py）
  · `show`（把仓库里的图推到卡片上）
  · `tools/guard.py` 的路径闸、`/api/station/file` 只读端点
  理由见 `src/station/tools/__init__.py` 的模块说明：它们服务的是"让 AI 看**工作站
  自己**"，业务场景（档案整理）用不到，用户的文件是**上传**上来的。

  所以原来那批"路径穿越 / `.env` 不许读 / `samples` 只读 / 写开关 / 覆盖前备份"的
  用例**不是被忽略了，是随功能一起没了** —— 现在没有任何工具能读写服务器仓库。
"""
from __future__ import annotations

import types

import pytest

from station import config


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把数据目录换到临时目录（长期记忆落在那里），返回 (仓库根, 数据目录)。"""
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    return repo, data


def ctx(user: str = "u1"):
    """最小的 Context 桩：这几个全局工具只用到 user_id 一个字段。"""
    return types.SimpleNamespace(user_id=user, allowed_keys=[], skill_id="station")


# ══ ① 长期记忆 ══════════════════════════════════════════════════════

def test_remember_then_recall_roundtrip(sandbox):
    from station.tools import memory
    out = memory.t_remember(ctx(), "导出 PDF 一律 A4 竖版，页面尺寸必须一致", kind="口径")
    assert "已记住" in out
    assert (config.sub("memory") / "MEMORY.md").is_file()          # 索引被维护
    assert "A4" in memory.t_recall(ctx(), "A4")
    assert "没找到" in memory.t_recall(ctx(), "根本不存在的词")


def test_recall_without_query_lists_index(sandbox):
    from station.tools import memory
    assert "还没有任何长期记忆" in memory.t_recall(ctx())
    memory.t_remember(ctx(), "用户是初学者，代码注释要详细", kind="偏好")
    idx = memory.t_recall(ctx())
    assert "共 1 条" in idx and "初学者" in idx


def test_remember_dedupes_identical_text(sandbox):
    """同一件事说两遍不该存两条（模型很容易重复记）。"""
    from station.tools import memory
    memory.t_remember(ctx(), "同一句话")
    assert "已经记过" in memory.t_remember(ctx(), "同一句话")
    assert "共 1 条" in memory.t_recall(ctx())


def test_memory_survives_across_tool_calls(sandbox):
    """★ "跨会话"的本质就是落在磁盘上 —— 换个 ctx（相当于换一次会话）照样读得到。"""
    from station.tools import memory
    memory.t_remember(ctx("u1"), "无犯罪记录证明归十类", kind="口径")
    assert "十类" in memory.t_recall(ctx("u2"), "无犯罪")


# ══ ② 自我认知 + 产物 ════════════════════════════════════════════════

def test_skills_lists_mounted_skills(sandbox):
    """`skills` 报的能力必须来自真正的注册表 —— 不许模型自己编。"""
    from station.tools import selfview
    out = selfview.t_skills(ctx())
    assert "干部档案整理" in out and "写周报" in out


def test_artifacts_says_what_it_cannot_do(sandbox):
    """★ 产物多到列不完时，返回值要**明说自己不能按类型过滤**、并把用户领到产物抽屉。

    09-13 实测：模型顺着"你目前累计有 47 个产物"主动提出"要不要我按周报/4件套分类
    帮你过滤一下" —— 而 `artifacts` 只接一个 limit，**根本没有过滤能力**，这是一句
    空头承诺。★ 纠正在**返回值**里（模型每轮都读得到），不只写在工具描述里 ——
    描述是"事先的说明"，返回值才是它每轮真实看到的东西。
    """
    from station.files import store as fsstore
    from station.tools import selfview
    for i in range(5):
        fsstore.save_text(f"内容{i}", ".md", name=f"周报-{i}.md", owner="u1",
                          group_key=f"g{i}", group_label=f"第{i}周")
    out = selfview.t_artifacts(ctx("u1"), limit=2)["text"]
    assert "不能按类型过滤" in out            # 说清做不到什么
    assert "我的产物" in out                  # 并指到真能按分组翻的地方（界面抽屉）
    assert "最近 2 个" in out
    # 列得完时不用啰嗦
    assert "不能按类型过滤" not in selfview.t_artifacts(ctx("u1"), limit=50)["text"]


def test_artifacts_empty_then_lists_products(sandbox):
    from station.files import store as fsstore
    from station.tools import selfview
    assert "还没有产物" in selfview.t_artifacts(ctx())
    fid = fsstore.save_text("周报正文", ".md", name="周报.md", owner="u1")
    out = selfview.t_artifacts(ctx("u1"))
    assert isinstance(out, dict) and out["render"]["type"] == "file-card"
    assert out["render"]["files"][0]["url"] == f"/api/files/{fid}?dl=1"


# ══ ③ 装载与合并 ════════════════════════════════════════════════════

def test_global_tools_are_namespaced_and_unique():
    """全局工具全名必须是 `station.<叶子>` 且不重名（宿主按全名索引）。"""
    from station.tools import GLOBAL_TOOLS
    names = [t.name for t in GLOBAL_TOOLS]
    assert len(names) == len(set(names))
    assert all(n.startswith("station.") for n in names)
    assert names == ["station.remember", "station.recall",
                     "station.skills", "station.artifacts"]


def test_no_file_tools_come_back():
    """★ 回归护栏：全局工具里**不许再出现**文件类工具。

    09-13 删掉了 `ls`/`read`/`grep`/`write`/`show` —— 它们服务的是"让 AI 看工作站自己"
    （服务器仓库），而业务场景用不到，还给每个会话白烧 schema、多开一条写服务器仓库的
    口子。哪天有人想加回来，先回去读 `station/tools/__init__.py` 那段说明：
    **要文件能力应该挂在具体的技能下（或做客户端执行器），不是提成"人人都有"的全局工具。**
    """
    from station.tools import GLOBAL_TOOLS
    leaves = {t.leaf() for t in GLOBAL_TOOLS}
    assert not (leaves & {"ls", "read", "grep", "write", "show"})


def test_generic_prompt_only_mentions_tools_that_exist():
    """★ 提示词与工具表会**各自漂移**，而漂移没有任何症状 —— 直到模型照着自己背的
    旧提示词答应用户。09-13 实测就是这个：`ls`/`read` 已经删了，通用对话还在说
    "我可以直接用 ls/read 帮你翻"。用户看到的是一句**答应得好好的空话**。

    做法：把提示词里**用 `（a/b/c）` 列出来的工具名**全抓出来，逐个比对真实清单。
    """
    import re
    from station.app.unified import GENERIC_SYSTEM
    from station.tools import GLOBAL_TOOLS
    leaves = {t.leaf() for t in GLOBAL_TOOLS}
    for grp in re.findall(r"（([a-z][a-z/]*)）", GENERIC_SYSTEM):
        for name in grp.split("/"):
            assert name in leaves, f"通用提示词提到了不存在的工具：{name}"
    # 也不许再声称"能读写仓库里的文件"（那是已删的能力）
    assert "读写仓库里的文件" not in GENERIC_SYSTEM


def test_no_global_tool_is_risky():
    """★ 全局工具**都不该**是 approve 类：它们每个会话都挂，动不动弹确认会很烦。

    （原来有个例外 `station.write`，09-13 随文件工具一起删了。危险动作应该由
    具体技能用自己的 approve 工具来做 —— 比如 archive 的 `export` 和 `apply_learning`。）
    """
    from station.tools import GLOBAL_TOOLS
    assert [t.name for t in GLOBAL_TOOLS if t.risk == "approve"] == []


def test_tools_for_merges_without_duplicates():
    """★ 通用聊天的 tools 本身就是全局工具 —— 合并时必须去重，否则同名 schema 发两份。"""
    from station.core.tool import Tool
    from station.core.agent import tools_for
    from station.app.unified import build_generic_agent
    from station.tools import GLOBAL_TOOLS

    generic = build_generic_agent()
    merged = tools_for(generic)
    assert [t.name for t in merged] == [t.name for t in GLOBAL_TOOLS]

    # ★ 挑被重复的那个名字**按 GLOBAL_TOOLS 实际的第一项**来，别写死一个字面量 ——
    #   写死的话哪天全局工具顺序一变（09-13 就变过），断言会指向一个根本没被重复的
    #   工具，于是这条测试**永远绿**，去重坏掉了也没人知道（review 抓到）。
    dup = GLOBAL_TOOLS[0].name

    class _Skill:                                  # 假装一个技能，只带一个自有工具
        tools = [GLOBAL_TOOLS[0],
                 Tool(name="archive.demo", description="x", run=lambda ctx: "")]
    names = [t.name for t in tools_for(_Skill())]
    assert names.count(dup) == 1                    # 去重过
    assert "archive.demo" in names
