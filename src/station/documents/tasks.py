"""附件任务已有恢复与取消清理：保留原件、旧版本和冻结的轮次引用。"""
import time
import uuid

from station.persistence.repositories import attachments as records


def source_turn(job, owner):
    return records.attachment_turn_get(job.args.get("turn_id", ""), owner)


def validate(job, owner):
    from station.documents import storage
    source = source_turn(job, owner)
    for did in source["attachment_ids"]:
        document = records.document_get(did, owner)
        if not document or not (storage.root(owner, did) / ("source" + document["suffix"])).is_file():
            raise ValueError("当前实例没有原件，请在保存原件的实例操作")


def prepare(job, owner, next_id, metadata):
    from station.documents import settings, loaders
    source = source_turn(job, owner)
    config = settings.snapshot()
    config["components"] = loaders.versions()
    record = {"id": uuid.uuid4().hex, "owner_id": owner, "thread_id": metadata["thread_id"],
              "client_key": "task:" + next_id, "message": source["message"],
              "attachment_ids": source["attachment_ids"], "config": config, "state": "waiting_parse",
              "created": time.time(), "job_id": next_id, "run_refs": {}, "failures": {},
              "active_skill": source.get("active_skill", ""), "manual_reply": True}
    turn, _ = records.attachment_turn_create(record)
    return {"turn_id": turn["id"]}


def cancel(job):
    turn = source_turn(job, job.user_id)
    if not turn:
        return
    records.attachment_turn_update(turn["id"], job.user_id, {"state": "cancelled"}, expected=("waiting_parse",))
    for did, rid in turn.get("run_refs", {}).items():
        document = records.document_get(did, job.user_id)
        if not document:
            continue
        runs = dict(document.get("runs", {}))
        run = dict(runs.get(rid, {}))
        if run.get("state") not in ("parsing", "parsed_ready"):
            continue
        run["state"] = "partial" if run.get("manifest") else "cancelled"
        runs[rid] = run
        patch = {"runs": runs}
        if document.get("latest_run") == rid:
            patch["status"] = run["state"]
        records.document_update(did, job.user_id, patch)
