"""MCP 配置：本人外部服务连接信息、工具清单缓存和连接测试。"""
from __future__ import annotations

import time
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from station.app.auth import require_user
from station.probes import BLOCKED_HOSTS
from station.probes import test_allowed

router = APIRouter(tags=["MCP 配置"])


class McpConfigIn(BaseModel):
    """保存用户配的 MCP 服务清单（整包覆盖，同模型配置）。"""
    servers: list = []


class McpTestIn(BaseModel):
    """「测试连接」只认**已经保存过**的服务 id，不接裸地址。

    同 /api/modelconfig/test 的理由：不然这个端点就成了一个"让服务器替我请求任意
    URL"的通用跳板（SSRF 面）。必须先落库，校验才拦得住。
    """
    server_id: str


def _mcp_payload(user: dict) -> dict:
    """回给界面的那一份（请求头只回"配过哪些名字"，值一律掩码）。"""
    from station.mcp import config as mcfg
    d = mcfg.view(user["id"])
    return {"servers": d["servers"], "max": d["max"],
            "transports": d["transports"]}


@router.get("/api/mcpconfig")
def get_mcp_config(request: Request):
    """列出：内置的（只读）+ 本人配的（含上次连上看到的工具清单）。"""
    return _mcp_payload(require_user(request))


@router.put("/api/mcpconfig")
def put_mcp_config(body: McpConfigIn, request: Request):
    """保存本人配的 MCP 服务。校验不过回 400 + **人话**问题清单。"""
    u = require_user(request)
    from station.mcp import config as mcfg
    try:
        mcfg.save_servers(u["id"], body.servers)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _mcp_payload(u)


@router.delete("/api/mcpconfig")
def delete_mcp_config(request: Request):
    """清空本人配的 MCP 服务（**内置的不受影响** —— 它们不在库里）。"""
    u = require_user(request)
    from station.mcp import config as mcfg
    mcfg.delete_servers(u["id"])
    return _mcp_payload(u)


@router.post("/api/mcpconfig/test")
def test_mcp_config(body: McpTestIn, request: Request):
    """「测试连接」：连上去、握手、问它有哪些工具，并把清单缓存下来。

    ★ 这是工具清单**唯一的写入口**。别处（尤其前端）提交的工具清单一律不认 ——
      那份清单决定"模型手上有什么武器"。
    """
    u = require_user(request)
    if not test_allowed(u["id"]):
        raise HTTPException(429, "测试过于频繁，请稍后重试")
    from station.mcp import client as mcli
    from station.mcp import config as mcfg
    spec = mcfg.find(u["id"], body.server_id)
    if spec is None:
        raise HTTPException(404, "没有这个 MCP 服务")

    # SSRF 防护：地址是用户填的，而请求由**服务器**发出。和模型配置那个探针同一套黑名单
    # （云元数据地址一律拒）。★ 内网/回环地址这里**不拦** —— 沿用既有口径（本地起的服务
    # 是正当用法），这条边界记在 docs/conventions.md 的 MCP 一节里。
    if spec.transport in ("http", "sse"):
        from urllib.parse import urlparse
        host = (urlparse(spec.url).hostname or "").lower()
        if host in BLOCKED_HOSTS or host.startswith("169.254."):
            raise HTTPException(400, "该地址不允许测试")

    t0 = time.perf_counter()
    tools, err = mcli.probe(spec)
    ms = int((time.perf_counter() - t0) * 1000)
    if not spec.builtin:                 # 内置的不落库（它们在代码里，不归用户配置管）
        mcfg.set_cache(u["id"], spec.id, tools, err)
    if err:
        # 失败也照 200 回：这不是"服务端错误"，是"对端连不上"，界面上要显示这句话。
        return {"ok": False, "error": err, "ms": ms, "tools": []}
    return {"ok": True, "ms": ms, "tools": tools}
