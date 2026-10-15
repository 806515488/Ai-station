"""System One HTTP adapter. Runtime routing and connection probes share this path.

The total timeout cancels the HTTP task and closes its connection. No SDK retries,
redirects, Chat Completions parameters, or implicit paid-model fallback are used.
"""
from __future__ import annotations

import asyncio
import json
import math
from urllib.parse import urlsplit


class InvalidDecision(ValueError):
    pass


class SystemOneHTTPError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"System One HTTP {status_code}")


def endpoint(base_url: str) -> str:
    root = base_url.rstrip("/")
    parsed = urlsplit(root)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("System One 接口根地址无效")
    if parsed.path.endswith(("/systemone", "/chat/completions")):
        raise ValueError("请填写接口根地址，例如 https://opencode.ai/zen/v1")
    return root + "/systemone"


def number(value) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise InvalidDecision("Invalid probability or confidence")
    return float(value)


def parse_choice(body: dict, question_id: str, criteria: dict) -> dict:
    """Reject incomplete/unknown distributions before they can influence routing."""
    try:
        answer = body["answers"][question_id]
        choice = answer["choice"]
        if answer["type"] != "choice" or choice not in criteria:
            raise InvalidDecision("Unknown choice")
        probabilities = answer["probabilities"]
        if set(probabilities) != set(criteria):
            raise InvalidDecision("Incomplete distribution")
        values = {key: number(value) for key, value in probabilities.items()}
        confidence = number(answer["confidence"])
        if (abs(sum(values.values()) - 1) > 0.01
                or values[choice] + 1e-6 < max(values.values())):
            raise InvalidDecision("Invalid distribution")
        return {"choice": choice, "confidence": confidence, "probabilities": values}
    except (KeyError, TypeError, AttributeError) as exc:
        raise InvalidDecision("Invalid System One response") from exc


async def _post(url: str, key: str, payload: dict, timeout: float) -> dict:
    import httpx

    async def request():
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("POST", url, json=payload,
                                     headers={"Authorization": f"Bearer {key}"}) as response:
                if response.status_code != 200:
                    # Never include upstream bodies, URLs or credentials in errors.
                    raise SystemOneHTTPError(response.status_code)
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 1024 * 1024:
                        raise InvalidDecision("Response too large")
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise InvalidDecision("Response is not JSON") from exc

    return await asyncio.wait_for(request(), timeout=timeout)


def evaluate(entry: dict, state: str, criteria: dict, *, timeout: float,
             question_id: str = "route", instructions: str = "选择最适合处理用户请求的选项。") -> dict:
    if not entry.get("api_key"):
        raise ValueError("尚未配置 OpenCode Zen API Key")
    if not entry.get("model") or not 2 <= len(criteria) <= 255:
        raise ValueError("模型或候选项配置无效")
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("No decision budget remaining")
    payload = {"model": entry["model"], "state": state,
               "questions": {question_id: {"type": "choice", "instructions": instructions,
                                           "criteria": criteria}}}
    body = asyncio.run(_post(endpoint(entry["base_url"]), entry["api_key"], payload, timeout))
    return parse_choice(body, question_id, criteria)


def error_hint(exc: Exception) -> str:
    if isinstance(exc, SystemOneHTTPError):
        return {401: "密钥无效，请核对 OpenCode Zen API Key",
                403: "该密钥没有使用此模型的权限",
                404: "接口或模型不存在，请核对配置；免费模型可能已下线",
                410: "此模型已停止提供，请调整备用顺序",
                429: "请求受到限流，请稍后再试"}.get(exc.status_code, "决策服务暂时不可用")
    if isinstance(exc, InvalidDecision):
        return "接口返回的决策数据无效，请确认接口类型及模型名称"
    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
        return "决策请求超时，请稍后再试"
    return "无法完成决策接口测试，请核对地址、密钥和模型配置"
