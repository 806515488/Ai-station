"""任务状态：本人后台任务、恢复动作与旧进度卡轮询入口。"""
from __future__ import annotations

from station.tasks.runtime import load_job


from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from station.tasks.executor import get_manager
from station.app.auth import require_user

router = APIRouter(tags=["任务状态"])


class TaskActionIn(BaseModel):
    action: str
    client_key: str


@router.get("/api/tasks")
def list_tasks(request: Request, cursor: str = "", category: str = "all"):
    from station.tasks.service import listing
    u = require_user(request)
    try:
        return listing(u["id"], cursor, category)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/tasks/{job_id}")
def task_detail(job_id: str, request: Request):
    from station.tasks.service import owned
    from station.tasks.views import view
    u = require_user(request)
    try:
        return view(owned(job_id, u["id"]), u["id"])
    except LookupError:
        raise HTTPException(404, "任务不存在")


@router.post("/api/tasks/{job_id}/actions")
def task_action(job_id: str, body: TaskActionIn, request: Request):
    from station.tasks.service import perform
    u = require_user(request)
    try:
        return perform(u["id"], job_id, body.action, body.client_key)
    except LookupError:
        raise HTTPException(404, "任务不存在")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    """前端轮询：查任务当前状态/进度/产物（status、progress、artifacts…）。"""
    u = require_user(request)
    snap = get_manager().get(job_id)
    # 归属判断只走 DB（_job_owner）：manager 里没有归属信息，只能回库查 user_id。
    if snap is None or _job_owner(job_id) != u["id"]:
        raise HTTPException(404, "任务不存在")
    return snap


def _job_owner(job_id: str) -> str:
    """查任务归属（DB 里看 user_id）；没有返回空串。"""
    j = load_job(job_id)
    return getattr(j, "user_id", "") if j else ""
