"""模型配置：配置读写与显式连接测试；不会从聊天请求隐式发起探针。"""
from __future__ import annotations

import station.models.catalog as model_catalog


from station.persistence.repositories import users as user_records


import os
import time
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from station.core.context import Context
from station.app.auth import require_user
from station.probes import test_allowed

router = APIRouter(tags=["模型配置"])


class ModelConfigIn(BaseModel):
    """PUT /api/modelconfig 的请求体：整包 provider 列表 + 各槽位的降级链。"""
    providers: list = []
    slots: dict = {}
    web_search: dict | None = None


class ProviderTestIn(BaseModel):
    """POST /api/modelconfig/test 的请求体。slot 留空=自动挑一个这家有的模型名。"""
    provider_id: str
    slot: str = ""
    model_id: str = ""


def _cfg_payload(user: dict, cfg: dict | None = None) -> dict:
    """组装给前端的配置响应。

    reveal=True：带上**本人**的密钥明文，界面里默认显示成 ****、点「显示」才明文
    （用户要求"保存后能回显、能核对"）。只走登录态、只回当前用户自己的那份 ——
    所以这个响应里绝不能加缓存，也别忘了它是 require_user 后面的。
    """
    from station.models import config as modelcfg
    cfg = cfg if cfg is not None else modelcfg.load_config(user["id"])
    # .env 里的密钥只兜底给站长（最早注册的账号）。非站长这里就当作"什么都没有"，
    # 界面显示黄点 +「还没填密钥」，逼他自带密钥 —— 站是站长掏钱开的，不该被白用。
    is_owner = user_records.is_owner(user["id"])
    web_entry, web_hint = modelcfg.search_entry(user["id"])
    from station.models.catalog import upgrade
    return {"config": modelcfg.mask_config(upgrade(cfg), reveal=True, allow_env=is_owner),
            "web_search_status": {"available": web_entry is not None, "hint": web_hint},
            "slots": model_catalog.slots_view(),
            "presets": model_catalog.presets(),      # 「＋ 添加模型来源」的一键模板
            # 界面据此选引导文案：站长"不改也能用"，别人"先去填密钥"。
            "env_keys": is_owner,
            # 离线假模型模式下整条配置链路都被绕过（chat.runtime.make_model 返回 FakeModel），
            # 告诉前端一声，免得用户配了半天以为没生效。
            "fake": os.environ.get("STATION_FAKE", "0") == "1"}


@router.get("/api/modelconfig")
def get_model_config(request: Request):
    """读取本人可编辑配置；按既有约定在认证后的响应中回显本人使用的密钥。"""
    return _cfg_payload(require_user(request))


@router.put("/api/modelconfig")
def put_model_config(body: ModelConfigIn, request: Request):
    """保存整包配置。校验不过回 400 + **人话**问题清单（不抛 pydantic 的 422）。"""
    u = require_user(request)
    from station.models import config as modelcfg
    try:
        incoming = {"providers": body.providers, "slots": body.slots}
        if "web_search" in body.model_fields_set:
            incoming["web_search"] = body.web_search
        cfg = modelcfg.save_config(u["id"], incoming)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _cfg_payload(u, cfg)


@router.delete("/api/modelconfig")
def delete_model_config(request: Request):
    """恢复默认：删掉本人这份配置（下次读就又回到内置默认）。"""
    u = require_user(request)
    from station.models import config as modelcfg
    modelcfg.delete_config(u["id"])
    return _cfg_payload(u)


@router.post("/api/modelconfig/test")
def test_model_config(body: ProviderTestIn, request: Request):
    """HTTP 适配：认证与限流后交给模型探针用例，不在路由里构造 SDK。"""
    user = require_user(request)
    if body.model_id and "~" in body.provider_id:
        raise HTTPException(400, "请分别提供来源标识和模型标识")
    if not test_allowed(user["id"]):
        raise HTTPException(429, "测试过于频繁，请稍后重试")
    from station.models.probes import test_connection
    try:
        return test_connection(user["id"], body.provider_id, body.slot, body.model_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/websearch/test")
def test_web_search(request: Request):
    """The explicit UI probe makes one search using this user's saved credentials."""
    user = require_user(request)
    if not test_allowed(user["id"]):
        raise HTTPException(429, "测试过于频繁，请稍后重试")
    from station.search import service
    from station.search.client import SearchError
    tick = time.monotonic()
    try:
        result = service.search(Context(user_id=user["id"]), "DeepSeek 官方文档", 1)
        return {"ok": True, "results": result["results"], "cache_hit": result["cache_hit"],
                "ms": round((time.monotonic()-tick)*1000, 1)}
    except SearchError as exc:
        return {"ok": False, "error": exc.code, "hint": exc.message,
                "ms": round((time.monotonic()-tick)*1000, 1)}
    except Exception:
        return {"ok": False, "error": "internal_error", "hint": "网页检索暂时不可用"}
