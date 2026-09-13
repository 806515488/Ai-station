"""批准闸（最轻的 PreToolUse 守卫）。

P1 策略：approve 类工具不直接跑——agent 循环把它挂起（thread.pending），
下一回合由用户在对话里“允许/拒绝”后由 server 补执行（in-band 批准）。
out-of-band 批准接口（POST /api/approve）留到后续。

新手视角：为什么危险操作要“问一句”？
  - 模型可能自作主张去删文件/发消息。所以工具标 risk="approve"，
    遇到它 agent 就停下来问用户（像 app 的“是否允许”弹窗）。
  - classify_answer 是把用户回复认成 allow/deny 的小判词器（“可以/好的”→allow）。
"""
from __future__ import annotations

from station.core.tool import Tool

# “同意 / 拒绝”的触发词（用户可能用不同的说法）。
# 存成常量而不是散在各处：以后想加一个说法，只改这一行。
# 超过这个长度就不当成"一句回答"（见 classify_answer 里的注释）—— 中文里
# "允许/可以/同意"这类回答都在几个字以内，12 已经放得很宽。
_ANSWER_MAX = 12
APPROVE_WORDS = ("允许", "同意", "可以", "yes", "是", "确认", "allow", "ok")
DENY_WORDS = ("拒绝", "不同意", "不行", "no", "否", "deny", "取消")


def needs_approval(tool: Tool, auto: bool = False) -> bool:
    """判断某个工具要不要走批准。

    规则：工具标了 approve（危险），并且没开 auto（自动放行）→ 需要批准。
    auto=True 是给“本机调试/自己用”的快捷开关（见 config.AUTO_APPROVE），
    任何对外场景都别开。
    """
    # risk == "approve" 且 未 auto 才返回 True（需要批准）
    return tool.risk == "approve" and not auto


def classify_answer(text: str) -> str:
    """把用户对批准请求的回答归类：allow / deny / ""（空=无关的普通对话）。

    例：用户回“可以” → "allow"；回“不行” → "deny"；
        回“今天天气不错” → ""（既不是同意也不是拒绝，server 就当作没确认）。
    """
    # .strip() 去掉首尾空格；.lower() 变全小写，这样 “YES / Yes / yes” 都算 yes
    t = (text or "").strip().lower()
    # ★ 只有"**就是**一句同意/拒绝"才算回答。整句太长 = 里面还夹着别的指令，
    #   这时候绝不能当批准 —— 补执行分支会拿它去执行 `thread.pending` 里那个工具，
    #   而那个工具可能完全是另一件事（review 抓到：学习卡预填的"确认这条口径…"
    #   会被判成 allow，顺手把挂起的「导出」跑掉）。宁可让他再说一次，
    #   也别替他执行他没要求的操作。
    if len(t) > _ANSWER_MAX:
        return ""
    # 先查拒绝词：因为"不同意"里也含"同意"，必须“拒绝优先”，否则会误判成 allow
    if any(w in t for w in DENY_WORDS):     # any(...) 只要有一个词命中就 True
        return "deny"
    if any(w in t for w in APPROVE_WORDS):
        return "allow"
    return ""                                # 都不是 → 无关对话
