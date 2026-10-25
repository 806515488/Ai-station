"""录音转写的单阶段执行：保存厂家编号后只查询、下载，不重复提交。"""
import json
import time
import interview_repository
from station.tasks.contracts import NeedsReview, RetryStep, StepResult
from station.tasks.runtime import entry_snapshot, resolve_remote


def validate(job, owner):
    pass


def prepare(job, owner, next_id, metadata):
    metadata.pop("deadline", None)
    interview_repository.interview_recording_start(job.args["recording_id"], owner, next_id)
    return dict(job.args)


def can_resume(checkpoint):
    return bool(checkpoint.get("remote_id"))


def run_step(ctx, phase, file_id="", interview_id="", recording_id=""):
    import asr
    import dashscope_filetrans as protocol
    from station.files import share, store
    meta = ctx.task_recovery
    recording = interview_repository.interview_recording_get(recording_id, ctx.user_id)
    if not recording or recording["interview_id"] != interview_id or recording["file_id"] != file_id:
        raise ValueError("音频与面试档案归属不一致")
    if recording.get("asr_job_id") != ctx.job_id:
        raise NeedsReview("录音已关联其他转写任务，停止旧执行")
    if phase == "submit":
        if meta.get("remote_id"):
            return StepResult("poll")
        if meta.get("submission_state") == "submitting":
            raise NeedsReview("转写提交结果不明，请核对平台记录，不会自动重复提交")
        entry = asr.resolve_entry(ctx)
        model = entry.get("model", "")
        if not model.lower().startswith("qwen-audio-") or "filetrans" not in model.lower():
            raise ValueError("当前只支持 Qwen-Audio Filetrans 异步转写")
        if not store.path(file_id):
            raise ValueError("音频文件不存在")
        token = share.make_ref_link(file_id, ctx.user_id, ttl=max(share.link_ttl(), 14400))
        ctx.checkpoint(ref_tokens=[token], **entry_snapshot(entry))
        ctx.check_cancel()
        ctx.checkpoint(submission_state="submitting", phase="submitting")
        try:
            remote = protocol.submit_task(entry["base_url"], entry["api_key"], model, share.ref_url(token),
                                          language_hints=["zh", "en"], diarization_enabled=True)
        except protocol.DashScopeAsrError as error:
            if error.status in (400, 401, 403, 404, 429):
                ctx.checkpoint(submission_state="rejected")
                share.revoke([token])
                raise ValueError("转写平台明确拒绝请求，请检查模型、额度或凭据") from error
            raise NeedsReview("转写平台是否受理尚未确认，请核对原请求") from error
        except Exception as error:
            raise NeedsReview("转写提交连接中断，请核对厂家记录") from error
        ctx.checkpoint(remote_id=remote, remote_state="submitted", submission_state="accepted",
                       deadline=time.time() + 7200)
        yield {"type": "progress", "message": "厂家已受理转写"}
        return StepResult("poll", 5, "等待厂家转写")

    if not meta.get("remote_id"):
        raise NeedsReview("缺少原转写任务编号，不能自动再次提交")
    entry = resolve_remote(ctx.user_id, meta, "speech")
    if phase == "collect" and recording.get("transcript_file_id"):
        saved = store.meta(recording["transcript_file_id"])
        if saved and saved.get("owner") == ctx.user_id and store.path(recording["transcript_file_id"]):
            yield {"type": "artifact", "id": recording["transcript_file_id"]}
            share.revoke(meta.get("ref_tokens", []))
            return StepResult(message="已取回保存的转写稿")
    try:
        ctx.check_cancel()
        state = protocol.query_task(entry["base_url"], entry["api_key"], meta["remote_id"])
    except Exception as error:
        raise RetryStep("查询原转写任务失败") from error
    status = protocol.task_status(state)
    if status in protocol.FAILED_STATES:
        ctx.checkpoint(remote_terminal=True, remote_state="failed")
        share.revoke(meta.get("ref_tokens", []))
        raise ValueError("厂家转写任务失败")
    if status not in protocol.SUCCESS_STATES:
        if not meta.get("deadline"):
            ctx.checkpoint(deadline=time.time() + 7200)
        if time.time() > float(meta["deadline"]):
            raise ValueError("本次转写等待已结束，可稍后继续查询原任务")
        return StepResult("poll", 5, "厂家正在转写音频")
    ctx.checkpoint(remote_state="completed")
    if phase == "poll":
        return StepResult("collect", message="转写完成，准备取回文字稿")
    if phase != "collect":
        raise ValueError("未知的转写任务阶段")
    try:
        raw = protocol.download_result(protocol.result_url(state), timeout=60)
    except Exception as error:
        raise RetryStep("下载原转写结果失败") from error
    result = protocol.normalize_result(raw, model=entry["model"], task_id=meta["remote_id"])
    ctx.check_cancel()
    fid = store.save_text(json.dumps(result, ensure_ascii=False), ".json",
                          name="面试转写-" + meta["remote_id"][:24] + ".json",
                          owner=ctx.user_id, skill_id="interview-coach",
                          group_key="interview-transcript:" + ctx.job_id, group_label="面试转写结果",
                          extra={"purpose": "interview_transcript"})
    interview_repository.interview_recording_finish(recording_id, ctx.user_id, transcript_file_id=fid,
                                                    asr_model=str(result.get("model") or ""), expected_job_id=ctx.job_id)
    yield {"type": "artifact", "id": fid}
    share.revoke(meta.get("ref_tokens", []))
    return StepResult(message="面试转写已保存")
