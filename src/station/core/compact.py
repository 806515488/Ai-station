"""context 层的最小落地：token 粗估 + 超长折叠成 summary。

（09-12 从 `core/memory.py` 改名过来。原因：叫 memory 会跟**长期记忆** ——
跨会话记得住的事实，见 station/tools/memory.py —— 混成一谈。而这里管的是
"**同一次对话**聊太长了怎么办"，聊完就没了。两件事，两个名字。）

对标三家 harness“上下文工程才是 moat”的认知，P1 先做到：超过阈值时，
把旧条目折叠进一条 system 摘要（有模型用模型压，没模型就截断），
保留最近 K 条给模型。默认关（config.COMPACT_TOKENS=0），可按需开。

新手视角：为什么需要“记忆管理”？
  - 模型一次能读的文本有上限（按 token 计），对话太长会超/变贵/变笨。
  - 解决：聊太久了，就把“前面的旧对话”压成一句摘要当记忆，只把最近几轮完整发给模型。
  - est_tokens 是粗略估算法（字符数换算），够用来决定“要不要触发压缩”。
"""
from __future__ import annotations


def est_tokens(msgs: list[dict]) -> int:
    """粗略估算这段历史约占多少 token（token≈模型眼里的一块词）。

    不需要精确——我们只想知道“是不是快超了”，用字符数粗算即可。
    """
    # 把每条消息的 content 累加出总字符数；str(...) 保险起见防止 content 不是 str
    chars = sum(len(str(m.get("content", ""))) for m in msgs)
    # 每条约按 4 token 的“包装开销”（role 字段、消息分隔符等）
    # 字符数 × 0.6 是常见经验换算（中文一个字≈0.6~1 token 量级）
    return int(chars * 0.6) + 4 * len(msgs)


def should_compact(msgs: list[dict], threshold: int) -> bool:
    """要不要触发压缩？阈值 >0 且 当前估算超阈值 才压（threshold=0 表示关）。"""
    return threshold > 0 and est_tokens(msgs) > threshold


def compact(msgs: list[dict], model=None, keep: int = 8) -> tuple[list[dict], bool]:
    """把 msgs 折成 [system 摘要] + 最近 keep 条。model 可空（空则截断压缩）。

    “折”的意思：把前面太长的旧对话丢掉，换成一句浓缩摘要放在最前当记忆。
    返回 (新 msgs, 是否真的发生了折叠)。
    """
    # 如果总条数还没到 keep+1，就没必要折（keep 是保留的最近条数）
    if len(msgs) <= keep + 1:
        return msgs, False
    # split = 要被“折叠掉”的条数：总长 - 要保留的
    split = len(msgs) - keep
    if split <= 0:                             # 防御：万一 keep 比总长还大
        return msgs, False
    old = msgs[:split]                         # old = 前面的旧对话（要浓缩）
    tail = msgs[split:]                        # tail = 最近 keep 条（原样保留给模型）
    summary = _summarize(old, model)           # 试着把 old 压成一句摘要
    # 新历史 = 一条 system“这是以前的记忆”+ 最近的真实对话
    head = [{"role": "system", "content": f"（此前对话已压缩）{summary}"}]
    return head + tail, True                   # 注意顺序：摘要要在最前面


def _summarize(msgs: list[dict], model) -> str:
    """把 msgs 的内容压成一句中文摘要。model 给了就用模型压；没给就简单截断。

    摘要别太长——它只是“还记得有这回事”的钩子，细节丢了也没关系，
    需要细节时可以再让用户补一句或重新问。
    """
    # 把所有非空 content 拼起来作为“待压缩原文”
    text = "\n".join(f"{m.get('role')}: {m.get('content')}"
                     for m in msgs if m.get("content"))
    if model is None or not text.strip():      # 没模型 / 没内容 → 给句占位
        return "（先前有若干轮对话，为控制上下文已省略）"
    try:
        # 让模型做压缩：给一条“请概括”的 system + 原文，它回一句短的
        resp = model.respond(
            [{"role": "system", "content": "把下面对话压成 ≤180 字中文摘要，只留事实/结论，不要过程。"},
             {"role": "user", "content": text[:4000]}], tools=None)  # tools=None=只聊天不调工具
        return str(resp.get("content") or "")[:200]   # 截 200 字兜底，防它话痨
    except Exception:                            # noqa  # 模型失败就别让压缩把会话搞挂
        return "（先前有若干轮对话，为控制上下文已省略）"
