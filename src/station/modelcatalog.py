"""Provider-owned model profiles and compatibility with legacy role fields.

References use '~', safe in Windows OCR cache filenames and absent from IDs.
No credentials are stored in profiles or model references.
"""
from __future__ import annotations

import copy
import math
import re

ROLES = {"chat": "text_model", "route": "route_model", "vision": "vision_model",
         "speech": "speech_model", "video": "video_model"}
FIELDS = ("id", "label", "name", "capabilities", "reasoning_effort", "route_protocol", "route_threshold")
ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def reference(provider_id, model_id):
    return provider_id + "~" + model_id


def split(value):
    if not isinstance(value, str):
        return "", ""
    parts = value.split("~")
    return (parts[0], parts[1]) if len(parts) == 2 else (value, "")


def models(provider):
    if "models" in provider:
        return provider["models"] if isinstance(provider["models"], list) else []
    out = []
    grouped = {}
    for role, field in ROLES.items():
        if provider.get("route_protocol") == "systemone" and role != "route":
            continue
        name = provider.get(field) or (provider.get("text_model") if role == "route" else "") or ""
        if not name:
            continue
        protocol = provider.get("route_protocol", "chat_completions") if role == "route" else "chat_completions"
        signature = (name, protocol)
        if signature not in grouped:
            value = {"id": "legacy-" + role, "label": name, "name": name,
                     "capabilities": [], "reasoning_effort": "", "route_protocol": protocol,
                     "route_threshold": None}
            grouped[signature] = value
            out.append(value)
        value = grouped[signature]
        value["capabilities"].append(role)
        if role == "chat":
            value["reasoning_effort"] = provider.get("reasoning_effort", "")
        if role == "route":
            value["route_threshold"] = provider.get("route_threshold")
    return out


def select(provider, slot, model_id=""):
    for model in models(provider):
        if (isinstance(model, dict) and isinstance(model.get("capabilities"), list)
                and (not model_id or model.get("id") == model_id) and slot in model["capabilities"]):
            return model
    return None


def clean(values):
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("每个来源最多配置 32 个模型")
    result, ids = [], set()
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("模型配置格式不正确")
        model = {key: copy.deepcopy(raw[key]) for key in FIELDS if key in raw}
        mid, name = model.get("id"), model.get("name")
        if not isinstance(mid, str) or not ID.fullmatch(mid) or mid in ids:
            raise ValueError("模型内部标识无效或重复")
        ids.add(mid)
        if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
            raise ValueError("请填写有效的模型名称（最多 256 个字符）")
        model["name"] = name.strip()
        label = model.get("label", "")
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError("模型显示名称最多 80 个字符")
        caps = model.get("capabilities")
        if (not isinstance(caps, list) or not caps or any(not isinstance(c, str) or c not in ROLES for c in caps)
                or len(caps) != len(set(caps))):
            raise ValueError("请为模型选择有效能力")
        protocol = model.get("route_protocol", "chat_completions")
        if protocol not in ("chat_completions", "systemone") or (protocol == "systemone" and caps != ["route"]):
            raise ValueError("Jev 决策模型只能用于意图识别")
        effort = model.get("reasoning_effort", "")
        if effort not in ("", "low", "medium", "high", "max") or (effort and "chat" not in caps):
            raise ValueError("推理强度只适用于通用对话模型")
        threshold = model.get("route_threshold")
        if threshold not in (None, "") and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                                               or not math.isfinite(threshold) or not 0 <= threshold <= 1):
            raise ValueError("模型意图识别阈值需为 0 到 1 的数字")
        result.append(model)
    return result


def upgrade(cfg):
    """View-only conversion; does not write the DB or change saved selections."""
    cfg = copy.deepcopy(cfg)
    providers = {p["id"]: p for p in cfg.get("providers", [])}
    for p in providers.values():
        p["models"] = models(p)
    for role, chain in cfg.get("slots", {}).items():
        upgraded = []
        for value in chain:
            pid, mid = split(value)
            p = providers.get(pid)
            model = select(p, role, mid) if p else None
            if model:
                upgraded.append(reference(pid, model["id"]))
        cfg["slots"][role] = upgraded
    return cfg
