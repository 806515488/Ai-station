"""项目正式 Celery Worker：读取任务编号，调用真实技能，并可靠保存业务阶段。

Web 不执行此文件的任务函数。相同镜像用 celery -A station.tasks.celery_app:app worker 启动。
"""
from contextlib import ExitStack
import time
import uuid

from station import config, observability as obs
from station.core.context import Context
from station.persistence import database
from station.persistence.repositories import jobs as records
from station.persistence.repositories.task_execution import exclusive
from station.tasks import runtime, settings
from station.tasks.celery_app import app
from station.tasks.contracts import ExecutionBusy, ExecutionLost, NeedsReview, RetryStep, StepResult
from station.tasks.job import Job


def execute_job(job_id, message_id):
    """可直接隔离验证的真实执行入口；消息过期或任务结束后不重复执行业务。"""
    from station.skills.registry import get_registry
    with ExitStack() as stack:
        if not stack.enter_context(exclusive(job_id)):
            raise ExecutionBusy()
        source = records.job_load(job_id)
        if (not source or source["message_id"] != message_id or source["work_status"] not in ("pending", "running")
                or source["queue"] not in (settings.queue(True), settings.queue(False))):
            return
        resource = source["meta"].get("resource")
        if resource and not stack.enter_context(exclusive("resource:" + resource)):
            raise ExecutionBusy()
        row = records.begin_execution(job_id, message_id, uuid.uuid4().hex)
        if row is None:
            return
        job = Job.from_record(row)
        execution = runtime.Execution(job)
        skill = get_registry().get(job.skill_id)
        if not skill or not (getattr(skill, "run_step", None) or getattr(skill, "build_runner", None)):
            runtime.update(execution, work_status="failed", message="技能执行入口不可用")
            return
        ctx = Context(skill_id=skill.id, data_dir=config.sub("skills", skill.id),
                      allowed_keys=list(skill.keys or []), user_id=job.user_id, job_id=job.id,
                      auto_approve=config.AUTO_APPROVE, task_recovery=job.meta)
        ctx.task_checkpoint = lambda **values: runtime.checkpoint(execution, **values)
        ctx.task_cancel = lambda: runtime.check(execution)
        ctx.task_emit = lambda event: runtime.append(execution, event)
        origin = job.meta.get("trace_id", "")
        trace = obs.begin(job.user_id, job.meta.get("thread_id", ""), kind="job",
                          key="job:" + job.id + ":" + job.execution_id, origin=origin,
                          attributes={"job_id": job.id, "skill": job.skill_id, "phase": job.phase}) if origin else None
        if trace:
            stack.enter_context(obs.activate(trace))
        generator = None
        state = "success"
        try:
            if job.step_attempts > 3:
                raise NeedsReview("当前步骤多次中断，请核对执行记录后再处理")
            runtime.check(execution)
            with obs.span("job", "执行后台任务步骤", job_id=job.id, skill=skill.id, phase=job.phase) as step:
                generator = (skill.run_step(ctx, job.phase, **job.args) if getattr(skill, "run_step", None)
                             else skill.build_runner(ctx, **job.args))
                if generator is None:
                    raise RuntimeError("技能执行入口没有返回事件生成器")
                while True:
                    runtime.check(execution)
                    try:
                        event = next(generator)
                    except StopIteration as finished:
                        result = finished.value or StepResult()
                        break
                    runtime.check(execution)
                    if isinstance(event, dict):
                        runtime.append(execution, event)
                if not isinstance(result, StepResult):
                    raise RuntimeError("技能返回了无效的阶段结果")
                job.meta["retry_count"] = 0
                runtime.advance(execution, result)
                step.update(artifacts=list(job.artifacts))
        except runtime.TaskCancelled:
            state = "interrupted"
            runtime.update(execution, work_status="cancelled", message="已停止本地处理；已受理的远端任务可能继续")
        except NeedsReview as error:
            state = "interrupted"
            runtime.update(execution, work_status="needs_review", message=str(error)[:600])
        except RetryStep:
            state = "error"
            count = int(job.meta.get("retry_count", 0)) + 1
            job.meta["retry_count"] = count
            if count >= 6:
                runtime.update(execution, work_status="failed", message="当前只读步骤多次失败，可核对原任务后继续取回")
            else:
                runtime.advance(execution, StepResult(job.phase, min(60, 2 ** count), "查询或取回暂时失败，稍后重试原任务"))
        except ExecutionLost:
            state = "interrupted"
            raise
        except ValueError as error:
            state = "error"
            uncertain = job.meta.get("submission_state") == "submitting" and not job.meta.get("remote_id")
            runtime.update(execution, work_status="needs_review" if uncertain else "failed",
                           message="远端提交结果未能保存，请核对厂家任务" if uncertain else str(error)[:600])
        except Exception as error:
            state = "error"
            uncertain = job.meta.get("submission_state") == "submitting" and not job.meta.get("remote_id")
            runtime.update(execution, work_status="needs_review" if uncertain else "failed",
                           message="远端提交结果不明，请核对后处理" if uncertain else "任务执行失败：" + type(error).__name__)
        except BaseException:
            state = "interrupted"
            raise
        finally:
            try:
                if generator is not None:
                    close = getattr(generator, "close", None)
                    if close:
                        close()
            finally:
                if trace:
                    obs.finish(trace, state)


@app.task(bind=True, name="station.tasks.execute", ignore_result=True)
def execute(self, job_id, message_id):
    """数据库暂时不可用或锁仍被旧执行持有时，延迟重试，不忙循环重投。"""
    try:
        execute_job(job_id, message_id)
    except (ExecutionBusy, ExecutionLost) as error:
        raise self.retry(exc=RuntimeError(type(error).__name__), countdown=5, max_retries=120)
    except Exception as error:
        # 开始读取任务前数据库就不可达等异常，不能伪造执行成功。
        raise self.retry(exc=RuntimeError(type(error).__name__), countdown=10, max_retries=12)
