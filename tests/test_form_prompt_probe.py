"""真机探针：新的 `form/pk/pg` 字段到底判得准不准？（默认跳过，要烧 token）

**为什么要有这个文件**：改建档提示词（或改《口径/表册对照.md》）会让 OCR 缓存键
（= 提示词 sha256）整体失效 —— 整卷 126 页重跑要真调 126 次视觉模型。所以不能
"改完直接全量跑，跑完再看好坏"。这个探针先挑**十页覆盖所有形态**的样本跑一遍，
花十次调用的钱就能回答"模型能不能给出可靠的 form"，不达标就回去改提示词重试。

怎么跑（要显式开开关，防止误跑烧钱）：
    ARCHIVE_LIVE=1 <conda python> -m pytest tests/test_form_prompt_probe.py -q -s
数据来源：优先 `data/station/skills/archive/projects/李明2/photos.json`（真实卷 126 页），
找不到就跳过（它不在 git 里）。

判据：可判定的 6 页里至少命中 5 页（≥80%）。剩下的页只打印、不断言 —— 它们本来就
是"两本册子同名栏目"的歧义页，模型答哪一本都不算错，**答 null 也不算错**（宁可留空）。
"""
from __future__ import annotations

import json
import os

import pytest

LIVE = os.environ.get("ARCHIVE_LIVE") == "1"

# 真实卷路径（不在 git；没有就跳过）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PHOTOS = os.path.join(_ROOT, "data", "station", "skills", "archive",
                      "projects", "李明2", "photos.json")

# 抽这十页：两本册子的册名页 / 各自的内页 / 志愿书封面与决议页 / 末页 / 一张单页件
PICK = [76, 84, 88, 110, 111, 16, 55, 64, 125, 83]

# 可判定的页：期望的册名（"哪一本都行"的歧义页不列进来）
EXPECT = {
    76: {"干部履历表"},
    84: {"干部履历表"},
    88: {"干部履历表"},
    110: {"工人登记表"},
    16: {"中国共产党入党志愿书", "入党志愿书"},
    55: {"中国共产党入党志愿书", "入党志愿书"},
    64: {"中国共产党入党志愿书", "入党志愿书"},
    111: {"干部履历表"},
}

pytestmark = pytest.mark.skipif(
    not LIVE, reason="真机探针（会烧 token）：设 ARCHIVE_LIVE=1 才跑")


def test_form_prompt_probe():
    """跑十页，打印 form/pk/pg 对照表，并对可判定的页算命中率。"""
    if not os.path.exists(PHOTOS):
        pytest.skip(f"没有真实卷数据：{PHOTOS}")
    from archive.engine import graph
    from archive.storage.project import load_project

    _, records = load_project(PHOTOS.replace("photos.json", "project.json"))
    by_seq = {r["seq"]: r for r in records}
    picked = [{**by_seq[s], "ocr": {}} for s in PICK if s in by_seq]

    out = graph.node_mark({"records": picked, "refresh": True})   # refresh：跳过缓存真读
    got = {r["seq"]: (r.get("ocr") or {}).get("mark") or {} for r in out["records"]}

    print("\n=== 建档 form 试点（新提示词）===")
    for s in PICK:
        mk = got.get(s) or {}
        print("第%3d张 | form=%-24s | pk=%-22s | pg=%-4s | t=%s"
              % (s, str(mk.get("form"))[:22], str(mk.get("pk"))[:20],
                 str(mk.get("pg"))[:4], str(mk.get("t"))[:18]))

    hits = [s for s, want in EXPECT.items()
            if (got.get(s) or {}).get("form") in want]
    total = len(EXPECT)
    print(f"\n可判定页命中 {len(hits)}/{total}：{[s for s in EXPECT if s in hits]}")
    print("歧义页（答哪本都行、答 null 也行）：",
          {s: (got.get(s) or {}).get("form") for s in PICK if s not in EXPECT})
    assert len(hits) >= total * 0.8, (
        f"form 命中率不足（{len(hits)}/{total}）—— 先改《表册对照》/提示词，别急着重跑全卷")
