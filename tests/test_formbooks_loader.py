"""表册对照（口径/表册对照.md）的解析单测 —— 纯读文件，不联网。

这份 md 有两个读者：**模型**（注入建档提示词，告诉它栏目名长什么样）和**代码**
（sec 按身份归组时查"栏目→册子"）。两个读者共用一份文件，就是不想出现
"文档改了、代码还是老口径"。所以解析必须稳：文件写坏了要能安全退化，而不是崩。
"""
from __future__ import annotations

from archive.skill import loader


def test_formbooks_parses_books_and_order():
    """真实那份对照表要能解析出册子、大类、是否整册、以及栏目版序。"""
    books = loader.formbooks()
    assert "干部履历表" in books and "工人登记表" in books
    hub = books["干部履历表"]
    assert hub["cat"] == "一" and hub["whole"] is True
    assert hub["title"] == "干部履历表"
    assert hub["aliases"] and "履历表" in hub["aliases"]
    # 版序：封面在最前、"其他需要说明的情况"是末页（99），且顺序单调
    assert hub["order"]["封面"] < hub["order"]["工作经历"] < hub["order"]["其他需要说明的情况"]
    # 单页件必须是 whole=False（同一种表会有一份一份很多张，不能整桶并）
    assert books["山西省企业职工岗位技能工资变动审批表"]["whole"] is False


def test_formbooks_skips_non_book_sections():
    """"单页件"那种说明性小标题底下没有 `- 大类：`，不能被当成一本册子。"""
    books = loader.formbooks()
    assert all(b["cat"] for b in books.values())
    assert "单页件" not in books and not any("单页件" in k for k in books)


def test_formbooks_bad_file_returns_empty(monkeypatch):
    """对照表读坏（文件没了/语法烂）→ 返回空表 + 记下原因，**不抛异常**。

    返回空表会让 seg 的覆盖率闸门自动回退到旧逻辑 —— 这是设计好的降级路径。
    """
    monkeypatch.setattr(loader, "_read", lambda rel: "\x00 读不出来 \x00")
    assert loader.formbooks() == {}
    assert loader._PARSE_ERROR


def test_mark_prompt_injects_the_formbook(monkeypatch):
    """建档提示词里必须真的带上对照表内容（否则模型无从判断 form）。"""
    text = loader.mark()
    assert "干部履历表" in text and "整册一份" in text
    assert "{FORMBOOK}" not in text                   # 占位符要被替换掉
    assert "位置无关" in text                          # 提醒模型：照片顺序与实物顺序无关


def test_resolve_prompt_has_both_slots(monkeypatch):
    """残页裁决提示词的两个槽（待归位的页 / 已认出的份）都要渲染进去。"""
    text = loader.resolve("第6张：表头《稿纸》", "- 干部履历表：共3页，日期无")
    assert "第6张" in text and "干部履历表：共3页" in text
    assert "{ORPHANS}" not in text and "{BUCKETS}" not in text
