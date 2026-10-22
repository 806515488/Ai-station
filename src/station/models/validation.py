"""保存前的配置校验：只检查结构与能力，不联网、不读写数据库。"""
from __future__ import annotations
import math
import re
from station.models import catalog

def _validate(cfg: dict) -> list[str]:
    """检查配置是否合理；返回**人话**问题清单（空列表 = 通过）。

    刻意不用 pydantic 的 422 原始结构：这些问题最终要显示在弹窗里给用户看，
    "field required" 这种文案帮不上忙。
    """
    def _name(p: dict) -> str:
        """报错时怎么称呼这一家：优先用用户填的名字，没有才退回落标识。"""
        return str(p.get("label") or "").strip() or str(p.get("id") or "?")

    problems: list[str] = []
    providers = cfg.get("providers")
    if not isinstance(providers, list) or not providers:
        return ["至少要保留一个模型来源"]
    if len(providers) > catalog.MAX_PROVIDERS:
        problems.append(f"模型来源过多（最多 {catalog.MAX_PROVIDERS} 个）")

    # 先看哪些模型来源**真的被上面某一行用上了**。
    # ★ 内容完整性的检查只针对这些"在用"的家：刚加进来、还没来得及填地址的，
    #   应该允许当成草稿存下来（用户的心智是"我先把密钥存着"）。否则他会被两个
    #   自己还没打算填的字段挡住，表现就是"我加了密钥却不能保存"（真发生过）。
    used: set[str] = set()
    for slot in catalog.SLOT_LABELS:
        chain = (cfg.get("slots") or {}).get(slot) or []
        if isinstance(chain, list):
            used |= {catalog.split(x)[0] for x in chain}

    ids: set[str] = set()
    by_id: dict[str, dict] = {}
    for p in providers:
        pid = str(p.get("id") or "").strip()
        # —— 结构检查：所有家都要过（id 是骨架，缺了/重了/脏了会出乱子）——
        if not pid:
            problems.append("有一个模型来源缺少内部标识")
            continue
        if pid in ids:
            problems.append(f"有两家的内部标识重复了：{pid}")
            continue
        # 标识的字符集要卡死：它会出现在前端 DOM 的 id 和内联 onclick 里，
        # 放任引号/括号进来就是一个注入面（别只靠前端转义兜着）。
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", pid):
            problems.append(f"内部标识只能用字母、数字和 . _ -（现在：{pid[:20]}）")
            continue
        ids.add(pid)
        by_id[pid] = p
        typed = "models" in p
        protocol = p.get("route_protocol", "chat_completions") if not typed else "chat_completions"
        if protocol not in catalog.ROUTE_PROTOCOLS:
            problems.append(f"{_name(p)} 的意图识别接口类型不支持")
        if (protocol == "systemone" or any(m.get("route_protocol") == "systemone" for m in catalog.models(p))) and p.get("base_url"):
            from station.models.protocols.systemone import endpoint
            try:
                endpoint(p["base_url"])
            except (ValueError, TypeError, AttributeError):
                problems.append(f"{_name(p)} 需填写接口根地址，例如 https://opencode.ai/zen/v1")
        threshold = p.get("route_threshold") if not typed else None
        if threshold not in (None, ""):
            if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                    or not math.isfinite(threshold) or not 0 <= threshold <= 1):
                problems.append(f"{_name(p)} 的意图识别阈值需为 0 到 1 的数字")
        if pid not in used:
            continue                        # 没用上的：允许是半成品，不挑内容
        # —— 内容检查：只挑"在用"的家 ——
        if not str(p.get("label") or "").strip():
            problems.append(f"{_name(p)} 未填写名称")
        if not catalog._scheme_ok(p.get("base_url")):
            problems.append(f"{_name(p)} 的「接口地址」需以 http:// 或 https:// 开头")
        # ★ 取值卡死在这里，别让脏值流到运行时：发一个对端不认的档位 = 400 →
        #   4xx 不重试 → 降级链**静默换家**，用户只会觉得"怎么换了模型/变笨了"。
        #   留空是合法的（= 一个字都不发），所以只有非空才查。
        eff = str(p.get("reasoning_effort") or "").strip() if not typed else ""
        if eff and eff not in catalog.REASONING_EFFORTS:
            problems.append(
                f"{_name(p)} 的「推理强度」只能填 "
                f"{' / '.join(catalog.REASONING_EFFORTS)}，或留空不发（现在填的是：{eff[:20]}）")
        # ★ 09-14 从这里删掉了"必须填 text_model（通用模型）"那条检查，挪到下面的
        #   槽位循环里按**槽位**查。原来它在 provider 级，于是：
        #     · 只服务视频那一行的家（agnes 没有对话模型）会被要求填「通用模型」；
        #     · 只填了 route_model 的专用小模型也会被误拦。
        #   而它本来想防的"某一行里的家缺那一行要的模型名"，只有 vision 做了（下面）。

    # ── 模型名检查：按**槽位**来（这才是它本来的语义）──
    # 凡是被某条链用上的家，必须提供**那条链**需要的模型名；否则 _entry() 返回 None、
    # resolve() 把它静默丢掉，用户看到的现象是"某一行莫名其妙空着"或"降级到别家"。
    for slot, label in catalog.SLOT_LABELS.items():
        chain = (cfg.get("slots") or {}).get(slot) or []
        if not isinstance(chain, list):
            problems.append(f"「{label}」的配置格式不正确")
            continue
        if len(chain) > catalog.MAX_CHAIN:
            problems.append(f"「{label}」的备用顺序过长（最多 {catalog.MAX_CHAIN} 个）")
        for ref in chain:
            pid, mid = catalog.split(ref)
            if pid not in by_id:
                problems.append(f"「{label}」中包含已不存在的来源：{pid}")
            elif mid and catalog.select(by_id[pid], slot, mid) is None:
                problems.append(f"「{label}」所选模型不存在或不支持该能力")
            elif not catalog._model_for(by_id[pid], slot):
                # 这一行为什么需要它、它就缺什么 —— 直接点名，用户才知道去哪一格填。
                # 例：看图行放了纯文字模型 → 识别时整卷报 400；视频行放了对话模型 →
                # 建任务时 400。都在保存这关拦住，别变成运行时的天书报错。
                # ★ 文案用界面上的行名（SLOT_LABELS），和用户眼睛看到的字对得上。
                problems.append(f"{_name(by_id[pid])} 未填写「{label}」")
    web = cfg.get("web_search", {"enabled": False, "provider_id": ""})
    if not isinstance(web, dict) or not isinstance(web.get("enabled"), bool):
        problems.append("网页检索配置格式不正确")
    else:
        pid = web.get("provider_id")
        if not isinstance(pid, str):
            problems.append("网页检索来源格式不正确")
        elif pid and (pid not in by_id or not catalog.official_search_source(by_id[pid])):
            problems.append("网页检索需选择官方 DeepSeek 来源（https://api.deepseek.com）")
    return problems
