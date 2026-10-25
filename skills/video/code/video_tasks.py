"""视频任务阶段：创建只执行一次；查询和取回可重复，不在 Worker 中长时间睡眠。"""
import time
from station.tasks.contracts import NeedsReview, RetryStep, StepResult
from station.tasks.runtime import entry_snapshot, resolve_remote


def validate(job, owner):
    pass


def prepare(job, owner, next_id, metadata):
    metadata.pop("deadline", None)
    return dict(job.args)


def can_resume(checkpoint):
    return bool(checkpoint.get("remote_id"))


def _revoke(ctx):
    from station.files import share
    share.revoke(ctx.task_recovery.get("ref_tokens", []))


def run_step(ctx, phase, prompt="", seconds="5", aspect_ratio="16:9", photos=None):
    import video_tools as video
    import agnes
    from station.files import share, store
    from station.models import config as modelcfg

    meta = ctx.task_recovery
    text = (prompt or "").strip()
    sec, aspect = video._norm_seconds(seconds), video._norm_aspect(aspect_ratio)
    if not text:
        raise ValueError("没有视频描述")
    if phase == "submit":
        if meta.get("remote_id"):
            return StepResult("poll")
        if meta.get("submission_state") == "submitting":
            raise NeedsReview("视频提交结果尚未确认，请核对平台记录，不会自动再次生成")
        entries = modelcfg.resolve(ctx.user_id, "video")
        if not entries:
            raise ValueError("没有配置视频模型")
        tokens, images = [], []
        for fid in (photos or [])[:video.MAX_PHOTOS]:
            token = share.make_ref_link(fid, ctx.user_id)
            tokens.append(token)
            images.append(share.ref_url(token))
        ctx.checkpoint(ref_tokens=tokens)
        for entry in entries:
            ctx.check_cancel()
            ctx.checkpoint(submission_state="submitting", phase="submitting", **entry_snapshot(entry))
            try:
                task = agnes.create_task(entry["base_url"], entry["api_key"], entry["model"],
                                         text, sec, aspect, images=images or None)
            except agnes.AgnesError as error:
                if error.status in (400, 401, 403, 404, 429, 503):
                    ctx.checkpoint(submission_state="rejected")
                    continue
                raise NeedsReview("厂家是否受理本次视频尚未确认，请核对平台记录") from error
            except Exception as error:
                raise NeedsReview("视频提交连接中断，无法确认厂家是否受理，请先核对") from error
            remote = str(task.get("video_id") or task.get("id") or "").strip()
            if not remote:
                raise NeedsReview("厂家未返回视频任务编号，请先核对，系统不会重复创建")
            ctx.checkpoint(remote_id=remote, remote_state="submitted", submission_state="accepted",
                           deadline=time.time() + video.POLL_DEADLINE, **entry_snapshot(entry))
            yield {"type": "progress", "message": "任务已提交，厂家已受理，等待生成"}
            return StepResult("poll", video.POLL_SECONDS, "等待厂家生成视频")
        _revoke(ctx)
        raise ValueError("所有配置的视频来源均明确拒绝提交，请检查配置或额度")

    if not meta.get("remote_id"):
        raise NeedsReview("缺少原视频任务编号，不能恢复或重新生成")
    entry = resolve_remote(ctx.user_id, meta, "video")
    if phase not in ("poll", "collect"):
        raise ValueError("未知的视频任务阶段")
    if phase == "collect":
        for saved in store.iter_files(owner=ctx.user_id):
            if saved.get("owner") == ctx.user_id and saved.get("group_key") == "video:" + meta["remote_id"] and store.path(saved["id"]):
                yield {"type": "artifact", "id": saved["id"]}
                _revoke(ctx)
                return StepResult(message="已取回保存的视频")
    try:
        ctx.check_cancel()
        state = agnes.query_task(entry["base_url"], entry["api_key"], entry["model"], meta["remote_id"])
    except agnes.AgnesError as error:
        if error.status == 429:
            yield {"type": "log", "text": "厂家查询限流，稍后继续查询原任务"}
            return StepResult("poll", max(video.POLL_SECONDS, error.retry_after or 0), "查询限流，稍后重试原任务")
        raise RetryStep("原视频任务查询暂时失败") from error
    except Exception as error:
        raise RetryStep("原视频任务查询暂时失败") from error
    status = str(state.get("status") or "").lower()
    if status in ("failed", "error", "cancelled"):
        ctx.checkpoint(remote_terminal=True, remote_state="failed")
        _revoke(ctx)
        reason = str(state.get("error") or state.get("message") or "厂家未返回失败原因")[:500]
        if entry.get("api_key"):
            reason = reason.replace(entry["api_key"], "<redacted>")
        raise ValueError("厂家视频任务失败：" + reason)
    if status not in ("completed", "succeeded", "done"):
        if not meta.get("deadline"):
            ctx.checkpoint(deadline=time.time() + video.POLL_DEADLINE)
        if time.time() > float(meta["deadline"]):
            raise ValueError("本次查询等待已结束，原远端任务可稍后继续取回")
        if state.get("progress") is not None:
            yield {"type": "progress", "percent": min(95, int(state["progress"])), "message": "厂家正在生成视频"}
        return StepResult("poll", video.POLL_SECONDS, "厂家正在生成视频")
    ctx.checkpoint(remote_state="completed")
    if phase == "poll":
        return StepResult("collect", message="视频已生成，准备取回")
    url = agnes.result_url(state)
    if not url:
        raise ValueError("任务已完成但没有下载地址，响应字段：" + str(sorted(state)))
    try:
        data = agnes.download(url)
    except Exception as error:
        raise RetryStep("视频下载暂时失败") from error
    ctx.check_cancel()
    fid = store.save_bytes(data, ".mp4", name="AI视频-" + meta["remote_id"][:24] + ".mp4",
                           owner=ctx.user_id, skill_id="video", group_key="video:" + meta["remote_id"],
                           group_label="AI 视频 · " + text[:18])
    yield {"type": "artifact", "id": fid}
    _revoke(ctx)
    return StepResult(message="视频已保存")
