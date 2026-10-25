"""任务展示：只读数据库业务状态，消息确认与工作状态分别呈现。"""
from station.files import store as files
from station.persistence.repositories import jobs as records

ACTIVE = {"queued", "running", "cancelling"}
ATTENTION = {"failed", "partial", "interrupted", "unknown"}


def view(job, owner, cache=None):
    meta = job.meta
    status = job.status
    artifacts = []
    title = {"video": "视频生成", "archive": "档案识别", "interview-coach": "音频转写"}.get(job.skill_id, job.skill_id)
    if job.phase == "export":
        title = "档案导出"
    if job.skill_id == "video":
        title += " · " + str(job.args.get("prompt", ""))[:40]
    elif job.skill_id == "archive":
        title += " · " + str(job.args.get("person", ""))
    elif job.skill_id == "interview-coach":
        title += " · " + str((files.meta(job.args.get("file_id", "")) or {}).get("name", ""))[:60]
    for fid in job.artifacts:
        value = files.meta(fid)
        if value and value.get("owner") == owner:
            artifacts.append({"name": value.get("name") or fid, "url": "/api/files/" + fid})
    actions, hint = [], ""
    successor = meta.get("successor_id", "")
    if successor and not records.task_existing_jobs(owner, [successor]):
        successor = ""
    if job.work_status in ("pending", "running") and not job.cancel_requested:
        actions.append({"id": "cancel", "label": "停止本地等待（远端可能继续）" if meta.get("remote_id") else "取消处理"})
    delivery_retry = (job.message_status in ("failed", "unknown") and not job.execution_id and job.step_attempts == 0
                      and job.work_status in ("pending", "needs_review") and job.phase != "legacy")
    if job.work_status in ("failed", "needs_review", "cancelled") and not successor and not delivery_retry:
        if job.skill_id == "archive":
            actions.append({"id": "retry_cached", "label": "重新处理（复用缓存）"})
        if meta.get("remote_id") and not meta.get("remote_terminal"):
            actions.append({"id": "resume_remote", "label": "继续查询原任务并取回结果"})
    if delivery_retry:
        actions.append({"id": "retry_publish", "label": "重新投递当前步骤"})
    if job.work_status == "needs_review":
        hint = "执行结果需要核验，不会自动重复提交外部任务。"
    if meta.get("submission_state") == "submitting" and not meta.get("remote_id"):
        hint = "尚未确认厂家是否受理，请核对原任务，系统不会自动再次提交。"
    elif job.work_status in ("failed", "needs_review", "cancelled") and meta.get("remote_id"):
        hint = "原远端任务编号已保留，继续操作只查询和取回结果，不重新生成。"
    if successor:
        hint = "已有后续任务，原结果保留。"
    return {"id": job.id, "type": job.skill_id, "title": title[:180], "status": status, "processing_state": status,
            "action_version": job.message_id or str(job.updated),
            "message_status": job.message_status, "work_status": job.work_status,
            "message": job.message[:600], "progress": job.progress, "created": job.created, "updated": job.updated,
            "thread_id": meta.get("thread_id", ""), "trace_id": meta.get("trace_id", ""),
            "parent_job_id": meta.get("parent_job_id", ""), "successor_id": successor,
            "phase": meta.get("phase") or job.phase, "hint": hint, "artifacts": artifacts, "actions": actions}
