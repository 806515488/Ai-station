"""文档附件：上传、解析轮次、原件和已发布解析资源的查询下载。"""
from __future__ import annotations

from station.persistence.repositories import attachments as attachment_records
from station.persistence.repositories import jobs as job_records


import os
import uuid
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from station.core.session import Thread
from station.tasks.executor import get_manager
from station.app.auth import require_user

router = APIRouter(tags=["文档附件"])


@router.post("/api/documents/upload")
async def upload_documents(request: Request, files: list[UploadFile] = File(...),
                           client_upload_id: str = Form(...)):
    from station.documents import service, settings
    user = require_user(request)
    if len(files) > settings.limits()["max_files"] or len(client_upload_id) > 96:
        raise HTTPException(400, "附件数量或上传编号超过限制")
    accepted, errors = [], []
    for index, file in enumerate(files):
        try:
            accepted.append(await service.upload(user["id"], file, f"{client_upload_id}:{index}"))
        except ValueError as e:
            errors.append({"name": file.filename, "message": str(e), "code": getattr(e, "code", "upload_failed")})
        finally:
            await file.close()
    return {"documents": accepted, "errors": errors}


@router.get("/api/documents/turns/{turn_id}")
def document_turn(turn_id: str, request: Request):
    from station.documents.service import turn_view
    user = require_user(request)
    turn = attachment_records.attachment_turn_get(turn_id, user["id"])
    if not turn:
        raise HTTPException(404, "附件轮次不存在")
    thread = Thread.load(turn["thread_id"]) if turn["thread_id"] else None
    return {**turn_view(turn), "job": get_manager().get(turn["job_id"]),
            "blocked_by_approval": bool(thread and thread.pending)}


@router.post("/api/documents/turns/{turn_id}/cancel")
def cancel_document_turn(turn_id: str, request: Request):
    user = require_user(request)
    turn = attachment_records.attachment_turn_get(turn_id, user["id"])
    if not turn:
        raise HTTPException(404, "附件轮次不存在")
    if not attachment_records.attachment_turn_update(turn_id, user["id"], {"state": "cancelled"}, expected=("waiting_parse",)):
        raise HTTPException(409, "解析已完成或轮次已结束")
    job_records.task_cancel(turn['job_id'],user['id'])
    return {"ok": True}


def _owned_document(document_id: str, request: Request) -> dict:
    user = require_user(request)
    document = attachment_records.document_get(document_id, user["id"])
    if not document:
        raise HTTPException(404, "文件不存在")
    return document


@router.get("/api/documents/{document_id}")
def document_detail(document_id: str, request: Request):
    from station.documents.service import view
    return view(_owned_document(document_id, request), True)


@router.delete("/api/documents/{document_id}")
def delete_document_draft(document_id: str, request: Request):
    from station.documents.service import delete_draft
    if not delete_draft(_owned_document(document_id, request)):
        raise HTTPException(409, "文件已经被使用，不能作为草稿删除")
    return {"ok": True}


@router.post("/api/documents/{document_id}/process")
def process_document(document_id: str, request: Request):
    from station.documents.service import reprocess, turn_view
    document = _owned_document(document_id, request)
    if document["status"] in {"parsing", "parsed_ready"}:
        raise HTTPException(409, "此文件仍在处理中")
    return turn_view(reprocess(document))


@router.get("/api/documents/{document_id}/files/{resource_id}")
def document_file(document_id: str, resource_id: str, request: Request, dl: int = 0, preview: int = 0):
    from station.documents.service import resource_path
    document = _owned_document(document_id, request)
    try:
        path, name = resource_path(document, resource_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "文档资源不存在或未完成")
    mime = {".json": "application/json", ".jsonl": "application/x-ndjson", ".txt": "text/plain"}.get(path.suffix)
    if preview:
        if mime is None:
            raise HTTPException(400, "此资源仅支持下载")
        with path.open(encoding="utf-8") as stream:
            text = stream.read(64001)
        return {"text": text[:64000], "truncated": len(text) > 64000}
    return FileResponse(path, media_type=mime or "application/octet-stream",
                        filename=name if dl or mime is None else None)


@router.get("/api/documents/{document_id}/archive")
def document_archive(document_id: str, request: Request, run_id: str = ""):
    """流式写 ZIP 到文档数据卷，再下载；包内引用使用相对路径。"""
    import zipfile
    from station.documents import storage
    document = _owned_document(document_id, request)
    try:
        folder, manifest = storage.manifest(document, run_id or None)
    except (ValueError, FileNotFoundError):
        raise HTTPException(409, "暂无可打包的结果")
    manifest_name = document["runs"][manifest["run_id"]]["manifest"]
    source = storage.root(document["owner_id"], document_id) / ("source" + document["suffix"])
    if storage.sha256(source) != document["sha256"]:
        raise HTTPException(409, "原件校验失败")
    archive = folder / (manifest_name + ".zip")
    temporary = archive.with_name(archive.name + "." + uuid.uuid4().hex + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.write(folder / manifest_name, "manifest.json")
        for entry in manifest["resources"]:
            path = (folder / entry["path"]).resolve()
            if not path.is_relative_to(folder.resolve()) or storage.sha256(path) != entry["sha256"]:
                raise HTTPException(409, "资源校验失败")
            package.write(path, entry["path"])
        package.write(source, "original/" + document["name"])
    os.replace(temporary, archive)
    return FileResponse(archive, media_type="application/zip", filename=Path(document["name"]).stem + ".zip")
