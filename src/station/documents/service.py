"""附件用例：固定上传/轮次身份，提交现有任务，查询经过归属校验的结果。

正文与原件只放数据卷；数据库保存有界元数据、已发布版本和会话关联。这里不直接
使用数据库连接，所有读写通过 db.py 的带锁助手，保持宿主的并发约定。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from station import db
from station.documents import loaders, settings, storage


async def upload(owner: str, file, client_key: str) -> dict:
    name = Path((file.filename or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    if not name or len(name) > 200 or "\0" in name:
        raise ValueError("文件名无效或过长")
    if suffix not in loaders.SUPPORTED:
        raise loaders.DocumentError("unsupported", "首版支持 TXT、MD、CSV、TSV、PDF、DOCX、XLSX 和 HTML")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", client_key):
        raise ValueError("上传请求编号无效")
    document_id = uuid.uuid4().hex
    folder = storage.root(owner, document_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("source" + suffix)
    size, digest = 0, hashlib.sha256()
    try:
        with path.open("wb") as stream:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > settings.limits()["max_bytes"]:
                    raise ValueError("上传文件超过大小限制")
                digest.update(block)
                stream.write(block)
        if not size:
            raise ValueError("上传文件为空")
        loaders.verify_signature(path, suffix)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        value = {"id": document_id, "owner_id": owner, "name": name, "suffix": suffix,
                 "size": size, "sha256": digest.hexdigest(), "date": now.date().isoformat(),
                 "uploaded_at": now.isoformat(), "created": time.time(), "status": "uploaded",
                 "runs": {}, "latest_run": ""}
        registered = db.document_register(value, client_key)
        if registered["sha256"] != value["sha256"] or registered["name"] != name:
            raise ValueError("此上传编号已用于另一份文件，请重新选择文件")
        if registered["id"] != document_id:
            path.unlink()       # 仅删除本次产生的未登记原件，不动既有文档
            folder.rmdir()
        return view(registered)
    except Exception:
        if path.exists():
            path.unlink()
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
        raise


def submit(thread, message: str, attachment_ids: list[str], client_key: str) -> tuple[dict, bool]:
    owner = thread.user_id
    if not client_key or len(client_key) > 128:
        raise ValueError("附件发送必须包含有效的请求编号")
    ids = list(dict.fromkeys(attachment_ids))
    if not ids or len(ids) > settings.limits()["max_files"]:
        raise ValueError("附件数量不符合限制")
    for document_id in ids:
        if db.document_get(document_id, owner) is None:
            raise ValueError("附件不存在或不属于当前用户")
    cfg = settings.snapshot()
    cfg["components"] = loaders.versions()
    record = {"id": uuid.uuid4().hex, "owner_id": owner, "thread_id": thread.id,
              "client_key": client_key, "message": message, "attachment_ids": ids,
              "config": cfg, "state": "waiting_parse", "created": time.time(),
              "job_id": uuid.uuid4().hex[:16], "run_refs": {}, "failures": {},
              "active_skill": (thread.meta or {}).get("active_skill", "")}
    turn, created = db.attachment_turn_create(record)
    if not created and (turn["message"] != message or turn["attachment_ids"] != ids):
        raise ValueError("此请求编号已用于不同的文字或附件")
    if not created and thread.id != turn["thread_id"]:
        from station.core.session import Thread
        old = Thread.load(turn["thread_id"])
        if old is not None and old.user_id != owner:
            raise ValueError("原会话归属不一致")
        if old is not None:
            thread.__dict__.update(old.__dict__)
        else:
            thread.id = turn["thread_id"]
    # 先保存轮次/会话，再启动 worker；用户输入不会被解析结果或重试改写。
    if not any(m.get("attachment_turn_id") == turn["id"] for m in thread.msgs):
        thread.add({"role": "user", "content": message, "attachment_turn_id": turn["id"]})
        thread.save()
    if created:
        from station.documents.pipeline import Executor
        from station.jobs.manager import get_manager
        get_manager().submit(Executor(), {"turn_id": turn["id"]}, owner, job_id=turn["job_id"])
    return turn, created


def mark_ready(turn_id: str, owner: str) -> None:
    turn = db.attachment_turn_get(turn_id, owner)
    if not turn or turn["state"] != "waiting_parse":
        return
    for document_id in turn["attachment_ids"]:
        run_id = turn.get("run_refs", {}).get(document_id)
        document = db.document_get(document_id, owner)
        run = document.get("runs", {}).get(run_id, {})
        if not run.get("manifest") and document_id not in turn.get("failures", {}):
            return
    db.attachment_turn_update(turn_id, owner, {"state": "ready"}, expected=("waiting_parse",))


def turn_view(turn: dict) -> dict:
    """返回前端需要的状态，不公开真实目录或整份后台配置。"""
    return {"id": turn["id"], "thread_id": turn["thread_id"], "job_id": turn["job_id"],
            "state": turn["state"], "error": turn.get("error", ""),
            "documents": [view(db.document_get(x, turn["owner_id"]), False)
                          for x in turn["attachment_ids"]], "failures": turn.get("failures", {})}


def view(document: dict, include_files: bool = False) -> dict:
    result = {k: document.get(k) for k in ("id", "name", "suffix", "size", "date", "sequence",
                                          "status", "error", "latest_run")}
    result["runs"] = list(document.get("runs", {}).values())
    if include_files:
        files = [{"id": original_id(document), "name": document["name"], "kind": "original"}]
        for run in document.get("runs", {}).values():
            if not run.get("manifest"):
                continue
            folder, manifest = storage.manifest(document, run["id"])
            for resource in manifest["resources"]:
                files.append({**resource, "run_id": run["id"]})
            for relative in dict.fromkeys((run["manifest"], "parsed-manifest.json", "manifest.json")):
                if (folder / relative).is_file():
                    files.append({**storage.resource(document["id"], run["id"], folder, relative), "run_id": run["id"]})
        for entry in files:
            entry["url"] = f"/api/documents/{document['id']}/files/{entry['id']}"
        result["files"] = files
    return result


def original_id(document: dict) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, document["id"] + ":original").hex


def resource_path(document: dict, resource_id: str) -> tuple[Path, str]:
    """仅从已登记清单找资源；resource_id 不能被解释成路径。"""
    if resource_id == original_id(document):
        source = storage.root(document["owner_id"], document["id"]) / ("source" + document["suffix"])
        if storage.sha256(source) != document["sha256"]:
            raise ValueError("原件校验失败")
        return source, document["name"]
    for run in document.get("runs", {}).values():
        if not run.get("manifest"):
            continue
        folder, manifest = storage.manifest(document, run["id"])
        candidates = list(manifest["resources"])
        for relative in dict.fromkeys((run["manifest"], "parsed-manifest.json", "manifest.json")):
            if (folder / relative).is_file():
                candidates.append(storage.resource(document["id"], run["id"], folder, relative))
        for entry in candidates:
            if entry["id"] != resource_id:
                continue
            path = (folder / entry["path"]).resolve()
            if not path.is_relative_to(folder.resolve()) or storage.sha256(path) != entry["sha256"]:
                raise ValueError("文档资源校验失败")
            return path, Path(entry["path"]).name
    raise ValueError("文件不存在或尚未发布")


def recover() -> None:
    """宿主单进程重启恢复纯本地解析；agent 外部副作用不自动重放。"""
    from station.documents.pipeline import BOOT_ID, Executor
    from station.jobs.manager import get_manager
    manager = get_manager()
    for turn in db.attachment_turn_list():
        if turn["state"] == "running_agent" and turn.get("agent_boot_id") != BOOT_ID:
            db.attachment_turn_update(turn["id"], turn["owner_id"],
                                      {"state": "interrupted", "error": "对话执行中断，请明确重试"})
        job = manager.get(turn["job_id"])
        if job and job["status"] in {"queued", "running"} and not manager.is_active(turn["job_id"]):
            if turn.get("worker_boot_id") != BOOT_ID and turn["state"] != "cancelled":
                manager.resume(turn["job_id"], Executor())
    for document in db.document_expired_drafts(time.time() - 7 * 86400):
        delete_draft(document)


def delete_draft(document: dict) -> bool:
    if not db.document_delete_draft(document["id"], document["owner_id"]):
        return False
    folder = storage.root(document["owner_id"], document["id"])
    path = folder / ("source" + document["suffix"])
    path.unlink(missing_ok=True)
    if not any(folder.iterdir()):
        folder.rmdir()
    return True


def reprocess(document: dict) -> dict:
    """重新处理保留原件和全部历史版本，仍复用宿主任务。"""
    cfg = settings.snapshot()
    cfg["components"] = loaders.versions()
    record = {"id": uuid.uuid4().hex, "owner_id": document["owner_id"], "thread_id": "",
              "client_key": uuid.uuid4().hex, "message": "重新处理附件", "attachment_ids": [document["id"]],
              "config": cfg, "state": "waiting_parse", "created": time.time(),
              "job_id": uuid.uuid4().hex[:16], "run_refs": {}, "failures": {}}
    turn, _ = db.attachment_turn_create(record)
    from station.documents.pipeline import Executor
    from station.jobs.manager import get_manager
    get_manager().submit(Executor(), {"turn_id": turn["id"]}, document["owner_id"], job_id=turn["job_id"])
    return turn
