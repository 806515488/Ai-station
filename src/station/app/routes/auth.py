"""登录：注册、登录、退出和当前用户查询。"""
from __future__ import annotations

from station import accounts
from station.persistence.repositories import users as user_records


from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from station.app.auth import current_user

router = APIRouter(tags=["登录"])


class AuthIn(BaseModel):
    """注册/登录请求体。"""
    name: str
    password: str


@router.post("/api/auth/register")
def auth_register(body: AuthIn):
    """注册（首个注册的用户即站长——本地站不做角色区分，仅提示）。"""
    try:
        u = accounts.create_user(body.name, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({"user": u, "first": user_records.user_count() == 1})
    resp.set_cookie("uid", u["id"], httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)   # 30 天免登录
    return resp


@router.post("/api/auth/login")
def auth_login(body: AuthIn):
    """登录：对了种 cookie；错了 401（不区分没此人和密码错，防探测）。"""
    u = accounts.check_login(body.name, body.password)
    if u is None:
        raise HTTPException(401, "用户名或密码不对")
    resp = JSONResponse({"user": u})
    resp.set_cookie("uid", u["id"], httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)
    return resp


@router.post("/api/auth/logout")
def auth_logout():
    """登出：清 cookie。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("uid")
    return resp


@router.get("/api/auth/me")
def auth_me(request: Request):
    """前端刷新后第一步就调它：登录了返回用户，没登录 401 → 弹登录视图。"""
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "未登录")
    return {"user": u}
