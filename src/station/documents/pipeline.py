"""宿主附件执行器：受控子进程解析文件，父进程负责状态和已完成快照发布。

生成器接入既有 JobManager。整批只在 manager 最外层领取一次重活闸，内部不再领锁；
超时、取消或资源超限会终止自己启动的解析进程及其子进程，不影响其它业务任务。
"""
from __future__ import annotations

from station.persistence.repositories import attachments as attachment_records


import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

from station import config
from station.documents import loaders, settings, storage


BOOT_ID = uuid.uuid4().hex


def publish(document: dict, run_id: str, manifest_name: str, state: str) -> dict:
    folder = storage.run_root(document["owner_id"], document["id"], run_id)
    value = json.loads((folder / manifest_name).read_text(encoding="utf-8"))
    from station.documents.quality import validate
    validate("manifest", value)
    # 分片读完校验之后才切指针；半写结果无法被下载接口或 agent 取到。
    for shard_group in [value["parsed"]["shards"], *(x["shards"] for x in value["chunk_sets"])]:
        for _ in storage.records(folder, shard_group):
            pass
    current = attachment_records.document_get(document["id"], document["owner_id"])
    runs = dict(current.get("runs", {}))
    runs[run_id] = {"id": run_id, "manifest": manifest_name, "state": state,
                   "parse_status": value["parsed"]["status"],
                   "chunk_status": value["chunk_sets"][0]["status"] if value["chunk_sets"] else "building",
                   "quality": value["quality"]["status"], "blocks": value["parsed"]["block_count"],
                   "chunks": sum(x["chunk_count"] for x in value["chunk_sets"]),
                   "parse_fingerprint": value["processing"]["parse_fingerprint"],
                   "parsed_snapshot_id": value["parsed"]["snapshot_id"], "updated": time.time()}
    return attachment_records.document_update(document["id"], document["owner_id"],
                              {"runs": runs, "latest_run": run_id,
                               "status": "failed" if value["quality"]["status"] == "unusable" else state,
                               "error": "没有可用正文，请核对文件或接入 OCR" if value["quality"]["status"] == "unusable" else ""})


def stop(process):
    """仅回收自己启动的进程树，避免 Loader 派生进程在超时后继续占内存。"""
    import psutil
    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
        for child in descendants:
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def cache_for(document: dict, cfg: dict) -> dict | None:
    parse_fp, _ = settings.fingerprints(document["sha256"], cfg, loaders.versions())
    for candidate in attachment_records.document_list(document["owner_id"], 1000):
        if candidate["sha256"] != document["sha256"]:
            continue
        for run in reversed(list(candidate.get("runs", {}).values())):
            if run.get("parse_fingerprint") != parse_fp or run.get("quality") == "unusable":
                continue
            if run.get("manifest"):
                return {"folder": str(storage.run_root(document["owner_id"], candidate["id"], run["id"])),
                        "manifest": run["manifest"]}
    return None


def run_one(document: dict, cfg: dict, turn: dict, cancel_check=None):
    """启动一个解析进程，短事件经队列交给父进程；日志不承载原文。"""
    import psutil
    run_id = uuid.uuid4().hex
    folder = storage.run_root(document["owner_id"], document["id"], run_id)
    folder.mkdir(parents=True, exist_ok=True)
    source = storage.root(document["owner_id"], document["id"]) / ("source" + document["suffix"])
    if storage.sha256(source) != document["sha256"]:
        raise ValueError("原文件校验失败")
    payload = {"document": document, "run_id": run_id, "source": str(source), "folder": str(folder),
               "config": cfg, "cache": cache_for(document, cfg)}
    storage.atomic_json(folder / "request.json", payload)
    refs = dict(attachment_records.attachment_turn_get(turn["id"], turn["owner_id"]).get("run_refs", {}))
    refs[document["id"]] = run_id
    attachment_records.attachment_turn_update(turn["id"], turn["owner_id"], {"run_refs": refs})
    runs = dict(document.get("runs", {}))
    runs[run_id] = {"id": run_id, "state": "parsing", "manifest": "", "updated": time.time()}
    attachment_records.document_update(document["id"], document["owner_id"], {"status": "parsing", "runs": runs})
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               DO_NOT_TRACK="1", LANGSMITH_TRACING="false")
    env["PYTHONPATH"] = os.pathsep.join([str(config.SRC), env.get("PYTHONPATH", "")])
    env["NLTK_DATA"] = os.getenv("STATION_DOCUMENT_NLTK_DATA") or os.getenv("NLTK_DATA") or str(
        config.DATA_DIR / "document-resources" / "nltk")
    messages = queue.Queue(maxsize=64)
    with (folder / "worker.stderr").open("wb") as errors:
        process = subprocess.Popen([sys.executable, "-m", "station.documents.worker", str(folder / "request.json")],
            stdout=subprocess.PIPE, stderr=errors, env=env, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def read_events():
            for line in process.stdout:
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                try:
                    messages.put(item, timeout=5)
                except queue.Full:
                    # 关键发布事件不能静默丢掉；异常会在父进程的退出校验中暴露。
                    process.kill()
                    return
        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        deadline = time.monotonic() + cfg["limits"]["timeout_seconds"]
        completed, parsed_ready, failure = False, False, None
        try:
            while process.poll() is None or reader.is_alive() or not messages.empty():
                if cancel_check:cancel_check()
                current_turn = attachment_records.attachment_turn_get(turn["id"], turn["owner_id"])
                if current_turn["state"] == "cancelled":
                    raise ValueError("附件处理已取消")
                if time.monotonic() > deadline:
                    raise ValueError("附件处理超时，请调整资源限制或拆分源文件")
                try:
                    live = psutil.Process(process.pid)
                    rss = live.memory_info().rss + sum(p.memory_info().rss for p in live.children(recursive=True))
                    if rss > cfg["limits"]["worker_memory_mb"] * 1024 * 1024:
                        raise ValueError("附件解析超过内存限制")
                except psutil.NoSuchProcess:
                    pass
                try:
                    event = messages.get(timeout=0.15)
                except queue.Empty:
                    continue
                kind = event.get("type")
                if kind == "parsed_ready":
                    publish(document, run_id, "parsed-manifest.json", "parsed_ready")
                    parsed_ready = True
                    from station.documents.service import mark_ready
                    mark_ready(turn["id"], turn["owner_id"])
                elif kind == "completed":
                    publish(document, run_id, "manifest.json", "completed")
                    completed = True
                elif kind == "failure":
                    failure = event
                yield event
            process.wait()
            if not completed:
                raise ValueError((failure or {}).get("message", "解析进程未生成完整结果"))
        finally:
            if process.poll() is None:
                stop(process)
            reader.join(timeout=5)
            process.stdout.close()
        if not parsed_ready:
            raise ValueError("解析结果未发布")


def build_runner(ctx, turn_id: str):
    turn = attachment_records.attachment_turn_get(turn_id, ctx.user_id)
    if turn is None:
        raise ValueError("附件轮次不存在")
    attachment_records.attachment_turn_update(turn_id, ctx.user_id, {"worker_boot_id": BOOT_ID})
    for index, document_id in enumerate(turn["attachment_ids"], 1):
        if attachment_records.attachment_turn_get(turn_id, ctx.user_id)["state"] == "cancelled":
            raise ValueError("附件处理已取消")
        document = attachment_records.document_get(document_id, ctx.user_id)
        try:
            for event in run_one(document, turn["config"], turn, cancel_check=getattr(ctx,"task_cancel",None)):
                if event.get("type") == "progress":
                    yield {"type": "progress", "percent": int((index - 1) / len(turn["attachment_ids"]) * 100),
                           "message": f"{document['name']} · {event['message']}"}
        except Exception as e:
            # 一份失败不取消其余文件；parsed 若已发布，仍然保留可用正文。
            current = attachment_records.document_get(document_id, ctx.user_id)
            run_id = attachment_records.attachment_turn_get(turn_id, ctx.user_id).get("run_refs", {}).get(document_id)
            runs = dict(current.get("runs", {}))
            if run_id and runs.get(run_id, {}).get("manifest"):
                runs[run_id]["chunk_status"] = "failed"
                runs[run_id]["state"] = "partial"
            else:
                # 失败也留下可下载的质量报告，不能只在临时日志里解释。
                run_id = run_id or uuid.uuid4().hex
                folder = storage.run_root(ctx.user_id, document_id, run_id)
                quality = {"status": "unusable", "issues": [{"code": "processing_failed", "severity": "error",
                           "message": str(e)[:200]}]}
                storage.atomic_json(folder / "quality.json", quality)
                failed = {"schema_version": "1.0", "document_id": document_id, "run_id": run_id,
                          "revision_id": current["sha256"], "owner_id": ctx.user_id,
                          "source": {"name": current["name"], "sha256": current["sha256"]},
                          "processing": {"parse_fingerprint": "", "config": turn["config"]},
                          "parsed": {"snapshot_id": None, "status": "failed", "block_count": 0, "shards": []},
                          "chunk_sets": [], "quality": quality,
                          "resources": [storage.resource(document_id, run_id, folder, "quality.json")]}
                storage.atomic_json(folder / "failed-manifest.json", failed)
                publish(current, run_id, "failed-manifest.json", "failed")
                runs = dict(attachment_records.document_get(document_id, ctx.user_id)["runs"])
            attachment_records.document_update(document_id, ctx.user_id, {"status": "failed", "error": str(e)[:200], "runs": runs})
            failures = dict(attachment_records.attachment_turn_get(turn_id, ctx.user_id).get("failures", {}))
            failures[document_id] = str(e)[:200]
            attachment_records.attachment_turn_update(turn_id, ctx.user_id, {"failures": failures})
            yield {"type": "log", "level": "warning", "text": f"{document['name']} · {str(e)[:200]}"}
        from station.documents.service import mark_ready
        mark_ready(turn_id, ctx.user_id)
    yield {"type": "progress", "percent": 100, "message": "附件处理结束"}


class Executor:
    """满足既有 JobManager 的执行器契约，不注册成需要意图路由的业务技能。"""
    id = "station-documents"
    name = "附件解析"
    keys = []
    heavy = True
    build_runner = staticmethod(build_runner)

    @property
    def task_recovery(self):
        from station.documents import tasks
        return tasks
