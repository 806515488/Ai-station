"""Web 登录态查询；供各路由共用，不让业务服务依赖 HTTP Request。"""

from station.persistence.repositories import users as user_records

from fastapi import HTTPException, Request




def current_user(request: Request) -> dict | None:
    """按当前 Cookie 回查用户；用户被删除后不继续沿用旧登录态。"""
    user_id = request.cookies.get("uid") or ""
    return user_records.get_user(user_id) if user_id else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "未登录")
    return user
