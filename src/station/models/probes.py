"""模型连接测试用例：解析已保存配置、验证目标、按能力执行最小请求。

正式调用和测试共用协议客户端；不执行退避或付费备用切换，错误只返回类型与提示。
HTTP 鉴权、DTO 和共享探针限流由 app/routes/models.py 完成。
"""
from __future__ import annotations

import time

from station.models import catalog as model_catalog
from station.probes import BLOCKED_HOSTS

_ERR_HINTS = (
    ("Authentication", "密钥不对 —— 服务端拒绝了这个 key，回去核对一下是不是粘错了/多粘了空格"),
    ("PermissionDenied", "这个 key 没权限用这个模型"),
    ("NotFound", "地址或模型名不对 —— 服务端说找不到。检查「接口地址」和「模型名」"),
    ("RateLimit", "被限流了 —— 这个 key 请求太频繁，缓一会儿再试"),
    ("Timeout", "超时 —— 地址连得通但没响应，可能是网络慢或服务在忙"),
    ("Connect", "连不上 —— 接口地址写错了，或者本机服务（如 Ollama）没启动"),
    ("BadRequest", "请求被拒 —— 多半是「模型名」填错了，核对一下"),
)


def _err_hint(name: str) -> str:
    """把异常类型名翻成一句人话（翻不出就返回空串，界面只显示类型名）。"""
    for key, hint in _ERR_HINTS:
        if key in (name or ""):
            return hint
    return ""


def test_connection(user_id: str, provider_id: str, slot: str = "", model_id: str = "") -> dict:
    """一次显式连接测试；语音/视频返回跳过，识图发送临时合成图。"""
    from station.models import config as modelcfg
    try:
        if model_id:
            url, key, model, hit_slot, effort = modelcfg.probe_entry(
                user_id, provider_id, slot, model_id)
        else:
            url, key, model, hit_slot, effort = modelcfg.probe_entry(
                user_id, provider_id, slot)
    except ValueError as e:
        raise ValueError(str(e)) from e
    decision_entry = None
    if hit_slot == "route":
        from station.models.catalog import reference
        ref = reference(provider_id, model_id) if model_id else provider_id
        entries = modelcfg.resolve_one(user_id, ref, "route")
        if entries and entries[0].get("route_protocol") == "systemone":
            decision_entry = entries[0]
            # Validate the exact snapshot sent below, even if configuration changed
            # concurrently after probe_entry(). Never resolve again after URL checks.
            url, key, model = (decision_entry["base_url"], decision_entry["api_key"],
                               decision_entry["model"])
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("base_url 必须是 http:// 或 https:// 地址")
    if host in BLOCKED_HOSTS or host.startswith("169.254."):
        raise ValueError("该地址不允许测试")
    if not key:
        return {"ok": False, "error": "NoKey", "ms": 0,
                "hint": "尚未配置密钥，请填写后重试（本地 Ollama 可填任意非空值）"}
    # ★ 命中"不是对话模型"的槽位（语音识别、视频生成）→ **跳过试连**，回一句中性提示。
    #   为什么不试：这条路的探法是拿 max_tokens=1 打 /chat/completions，而这类模型
    #   不走对话接口，必然 400 —— 那**不是"连不上"，是探法不对**。显示成红叉会
    #   让用户去改一个本来正确的模型名（假红灯比不测更糟）。
    #   也别改成 GET /models 探活：无法确认对端实现了它，探不到又是一次假红灯。
    #   顺序放在"没填密钥"之后 —— "你还没填密钥"比"这里测不了"更该先说。
    if model_catalog.PROBE_NOT_CHAT.get(hit_slot):
        return {"ok": None, "skip": True, "ms": 0,
                "hint": model_catalog.PROBE_NOT_CHAT[hit_slot]}
    t0 = time.perf_counter()
    try:
        if decision_entry:
            from station.models.protocols import systemone
            try:
                systemone.evaluate(decision_entry, "你好", {
                    "greeting": "用户向你打招呼", "other": "其它请求"}, timeout=3.0)
                return {"ok": True, "protocol": "systemone",
                        "ms": int((time.perf_counter() - t0) * 1000)}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__,
                        "hint": systemone.error_hint(exc),
                        "ms": int((time.perf_counter() - t0) * 1000)}
        # 正式调用和探针共用 openai.build_client，首次 SDK 导入共用锁和缓存。
        # 首次导入必须串行化，否则这里可能与对话/识别同时撞上"首次并发 import"竞态。
        # ★ 这个探针**刻意不套退避重试**：用户手点的"测试连接"应该立刻说真话，
        #   让他等 31 秒才看到"连不上"只会以为界面卡了。
        from station.models.protocols import openai

        def _try(eff: str) -> None:
            """按给定推理强度打一次最小请求；失败就抛（调用方决定怎么报）。

            eff 为空串 = 不带这个参数（第一趟就是这种）。
            ★ 传参形状走 reasoning_kwargs —— 与真对话**同一处定义**，否则会出现
              "探针说能用、真说话时却发了另一套参数"的偏差。
            """
            llm = openai.build_client(
                {"id": provider_id, "model": model, "api_key": key, "base_url": url,
                 "reasoning_effort": eff}, timeout=8, max_retries=0, max_tokens=1)
            content = "hi"
            if hit_slot == "vision":
                import base64
                import io
                from PIL import Image
                stream = io.BytesIO()
                Image.new("RGB", (128, 128), "red").save(stream, format="PNG")
                content = [{"type": "text", "text": "看图回答一个字"},
                           {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()}}]
            llm.invoke([{"role": "user", "content": content}])

        _try("")                                   # 第一趟：地址/密钥/模型名对不对
        out = {"ok": True, "ms": int((time.perf_counter() - t0) * 1000)}
        if effort:
            # 第二趟：这家认不认「推理强度」。★ 单独一趟、单独报，是为了让用户分得清
            # "地址填错了"和"这个参数对端不收"—— 混在一起报会让他去改一个本来就
            # 正确的地址（同 PROBE_NOT_CHAT 那条"假红灯比不测更糟"）。
            # ★ 只在这一栏非空时才多打一次；留空的家（绝大多数）仍是一次调用。
            t1 = time.perf_counter()
            try:
                _try(effort)
                out["effort"] = {"value": effort, "accepted": True,
                                 "ms": int((time.perf_counter() - t1) * 1000)}
            except Exception as e:                # noqa：对端不收就如实回"不收"，不算错
                out["effort"] = {"value": effort, "accepted": False,
                                 "error": type(e).__name__}
        return out
    except Exception as e:                        # noqa：连不上是常见情况，不算服务端错误
        name = type(e).__name__
        return {"ok": False, "error": name, "hint": _err_hint(name),
                "ms": int((time.perf_counter() - t0) * 1000)}
