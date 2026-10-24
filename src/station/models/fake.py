"""离线演示模型：按固定规则模拟工具调用与流式输出，不访问供应商。

业务关键词仅用于演示，不参与真实模型调用、配置或协议适配。
"""
from __future__ import annotations
import os
import time

class FakeModel:
    """离线确定性“模型”：只服务 demo 冒烟/单元测试，不联网不烧 key。

    规则（按优先级）：
      - 刚有工具结果：若来源是 collect_highlights → 接着调 render_report（多步串联演示）；
        否则直接给一句最终答复。
      - 用户话里带“fake_risky/批准” → 调危险工具（验证批准闸）
      - 带“周报” → 调 collect_highlights（写周报链第一步）
      - 带“几点/时间” → 调 now
      - 否则调 echo 回显
    stream() 是 respond() 的流式外壳：决策还是上面这套规则，只是把文字切片发出去，
    让离线路径也能验证“逐字显示 / 边说话边调工具”的界面行为。
    一句话：FakeModel 是给你“看清楚 harness 循环本身”的替身，不是真 AI。
    """

    def __init__(self, skill_hint: str = "demo"):
        self._skill = skill_hint
        self._n = 0                       # 自增计数，生成不重复的假 tool_call_id

    def _prev_leaf(self, msgs: list[dict]) -> str:
        """往回找最近一次 assistant 调过的工具名（叶子），用于决定“下一步调谁”。"""
        for m in reversed(msgs):
            calls = m.get("tool_calls") if m.get("role") == "assistant" else None
            if calls:
                return str((calls[0] or {}).get("name", "")).split(".")[-1]
        return ""

    def respond(self, msgs: list[dict], tools=None) -> dict:
        by_leaf = {t.name.rsplit(".", 1)[-1]: t for t in tools or []}   # 只消费模型工具协议
        last = msgs[-1] if msgs else {}

        # 情形一：历史最后一条是“工具结果”→ 看要不要接着调下一个工具
        if last.get("role") == "tool":
            prev = self._prev_leaf(msgs)
            if prev == "collect_highlights" and "render_report" in by_leaf:
                # 素材到手了 → 把素材喂给 render_report 成稿（多工具串联演示）
                return self._call(by_leaf["render_report"],
                                  {"content": str(last.get("content", ""))[:600]})
            return {"content": f"(fake) 已得到工具结果：{last.get('content')}",
                    "tool_calls": []}

        # 情形二：用户刚说话 → 按关键词“假装思考”决定调哪个工具
        # （只看【最后一条】用户消息——历史里出现过"照片已上传"不代表这轮还在说它）
        text = next((m.get("content", "") for m in reversed(msgs)
                     if m.get("role") == "user"), "")
        if tools and ("fake_risky" in text or "批准" in text) and "fake_risky" in by_leaf:
            return self._call(by_leaf["fake_risky"], {})
        if tools and "周报" in text and "collect_highlights" in by_leaf:
            return self._call(by_leaf["collect_highlights"], {})
        if tools and ("几点" in text or "时间" in text) and "now" in by_leaf:
            return self._call(by_leaf["now"], {})
        # 口径学习：用户确认后调 apply_learning 落盘（不用传内容 —— 条文由技能自己算）。
        # 离线把这一跳跑通，整条链（改类→提案→确认→写盘）才算真通。
        # ★ 匹配的是**学习卡按钮预填的那句话**（index.html 的 learnGo）—— 它改过一次
        #   （原来带"确认"，会被 classify_answer 误判成对**别的**挂起操作的批准，09-13 修掉），
        #   两边必须一起改，否则离线链路会静默断在这里。
        if tools and "apply_learning" in by_leaf and "写进《文种对照》" in text:
            return self._call(by_leaf["apply_learning"], {})
        # archive 技能的离线规则（端到端冒烟：上传卡→建卷→全景→改类→出件批准）
        # ★ 对账规则必须排在 ask_photos **前面**：上传终版目录的消息里带着
        #   "…人事档案目录.xlsx" 这种路径，含"档案"二字，会被下面那条泛化规则接走。
        if tools and "reconcile" in by_leaf and "对账" in text:
            import re as _re
            m = _re.search(r"终版目录已上传：(.+?)（", text)
            return self._call(by_leaf["reconcile"],
                              {"path": m.group(1)} if m else {})
        if tools and "ask_photos" in by_leaf and "照片已上传" not in text \
                and ("上传" in text
                     or ("照片" in text and "改成" not in text)
                     or ("档案" in text and "改成" not in text)):
            return self._call(by_leaf["ask_photos"], {})
        if tools and "scan_photos" in by_leaf and "照片已上传" in text:
            import re as _re
            # 消息由上传卡片拼，两种形态（见 index.html 的 upGo）：
            #   照片已上传：<目录>（126 张，姓名：张三）
            #   照片已上传：<目录>（126 张，未填姓名，文件夹名：李明）
            # → dir 取"（"之前那段；只有"姓名：X"才当 person，"未填姓名"就**不传**
            #   （让 scan_photos 走"先问一句"的分支 —— 这条规则链在离线冒烟里也要一致）。
            m = _re.search(r"照片已上传：(.+?)（", text)
            p = _re.search(r"张\s*[，,]\s*姓名：\s*(.+?)\s*）", text)
            args = {"dir": m.group(1)} if m else {}
            if p:
                args["person"] = p.group(1)
            return self._call(by_leaf["scan_photos"], args)
        if tools and "recognize" in by_leaf and ("开始识别" in text or "识别" in text):
            return self._call(by_leaf["recognize"], {})
        # 换识图模型：报菜单 / 切到某一家（离线冒烟用；真模型下由 description 驱动）
        if tools and "list_vision_models" in by_leaf and (
                "有哪些模型" in text or "换个模型" in text or "换一家" in text):
            return self._call(by_leaf["list_vision_models"], {})
        if tools and "use_model" in by_leaf:
            import re as _re
            m = _re.search(r"用\s*([A-Za-z0-9_.\-]+)\s*(?:读的|那家|的)", text)
            if m:
                return self._call(by_leaf["use_model"], {"provider": m.group(1)})
        if tools and "show_overview" in by_leaf and ("全景" in text or "核对" in text):
            return self._call(by_leaf["show_overview"], {})
        if tools and "set_category" in by_leaf and "改成" in text:
            import re as _re
            m = _re.search(r"把\s*(.+?)\s*改成\s*([一二三四五六七八九十]+(?:-\d+)?)", text)
            if m:
                return self._call(by_leaf["set_category"],
                                  {"target": m.group(1), "category": m.group(2)})
        if tools and "export" in by_leaf and "出件" in text:
            return self._call(by_leaf["export"], {})
        if tools and "echo" in by_leaf:
            arg = text.strip()[:40] or "你好"
            return self._call(by_leaf["echo"], {"text": arg})
        return {"content": "(fake) 我没有可用的工具来回应", "tool_calls": []}

    _CHUNK = 4          # 离线流式把文字切成几字一片（只为让“逐字出现”肉眼可见）

    def stream(self, msgs, tools=None):
        """离线流式：决策完全交回 respond()，只把它的文字切片发出去。

        这样假模型和真模型走【同一套】流式协议（agent.py 不用分叉），
        而“这一步该调哪个工具”的规则只在 respond 里写一遍，不会两边走样。
        STATION_STREAM_DELAY=0.05 可放慢切片速度，方便肉眼确认 SSE 真在流。
        """
        resp = self.respond(msgs, tools)          # 唯一的决策源（含 _n 计数）
        text = resp.get("content") or ""
        delay = float(os.environ.get("STATION_STREAM_DELAY", "0") or 0)
        for i in range(0, len(text), self._CHUNK):
            if delay:
                time.sleep(delay)                 # 默认 0：不拖慢离线单测
            yield {"delta": text[i:i + self._CHUNK]}
        yield {"final": resp}

    def _call(self, tool, args: dict) -> dict:
        """伪造一条“模型想调工具”的返回（真实模型会基于语义选工具，这里按规则选）。

        带一句固定前言：真模型常常先说一句“我来看一下”再调工具，离线也照此模拟——
        否则“边说话边调工具”的界面行为（气泡怎么切、前言要不要进历史）离线测不到。
        """
        self._n += 1
        return {"content": "（fake）我来看一下。", "tool_calls": [
            {"id": f"fake-{self._n}", "name": tool.name, "arguments": args}]}
