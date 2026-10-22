"""执行追踪：只读查询本人 trace 及会话关联追踪。"""
from __future__ import annotations

from station.persistence.repositories import traces as trace_records


from fastapi import APIRouter, HTTPException, Request

from station.core.session import Thread
from station.app.auth import require_user

router = APIRouter(tags=["执行追踪"])


@router.get("/api/traces/{trace_id}")
def trace_detail(trace_id: str, request: Request):
    from station.observability.service import detail
    u = require_user(request)
    value = detail(trace_id, u["id"])
    if value is None:
        raise HTTPException(404, "执行记录不存在")
    return value


@router.get("/api/sessions/{tid}/traces")
def session_traces(tid: str, request: Request):
    u = require_user(request)
    thread = Thread.load(tid)
    if thread is None or thread.user_id != u["id"]:
        raise HTTPException(404, "会话不存在")
    return {"traces": trace_records.trace_list(tid, u["id"])}
