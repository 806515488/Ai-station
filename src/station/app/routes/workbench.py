"""工作台 HTTP 适配：本人登录校验、目录查询和开始前的输入校验。"""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import FileResponse

from station import workbench
from station import workbench_cases
from station.app.auth import require_user

router = APIRouter(tags=["工作台"])


class PrepareIn(BaseModel):
    # 不接收技能强制选择、路径、历史会话或工具参数，避免首页另开一条执行链。
    model_config = ConfigDict(extra="forbid")
    draft: str = Field(default="", max_length=workbench.DRAFT_LIMIT)


@router.get("/api/workbench")
def get_workbench(request: Request, response: Response):
    require_user(request)
    response.headers["Cache-Control"] = "no-store"
    return workbench.catalog()


@router.post("/api/workbench/{skill_id}/prepare")
def prepare_message(skill_id: str, body: PrepareIn, request: Request, response: Response):
    require_user(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return workbench.prepare(skill_id, body.draft)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/workbench/{skill_id}/case")
def get_case(skill_id: str, request: Request, response: Response):
    require_user(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return workbench_cases.payload(skill_id, workbench.case_definition(skill_id))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/api/workbench/{skill_id}/case/files/{asset_id}")
def get_case_file(skill_id: str, asset_id: str, request: Request, preview: int = 0):
    require_user(request)
    try:
        path, media, filename = workbench_cases.artifact(skill_id, workbench.case_definition(skill_id), asset_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return FileResponse(path, media_type=media, filename=filename, headers={"Cache-Control": "no-store"},
                        content_disposition_type="inline" if preview and media.startswith("image/") else "attachment")


@router.get("/api/workbench/{skill_id}/case/materials")
def get_case_materials(skill_id: str, request: Request):
    require_user(request)
    try:
        data = workbench_cases.materials(skill_id, workbench.case_definition(skill_id))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return Response(data, media_type="application/zip", headers={"Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="{skill_id}-example-images.zip"'})
