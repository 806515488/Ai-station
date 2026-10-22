"""任务执行：登记、领取执行权、驱动技能生成器、追踪及资源释放。

保持现有每任务一个后台线程和最外层重活闸。任务数据在 job.py，锁和租约在 runtime。
"""
from __future__ import annotations
import contextlib
import threading
import traceback
from station import config
from station.core import heavy
from station.core.context import Context
from station.persistence.repositories import jobs as job_records
from station.tasks.job import Job
from station.tasks import runtime

class JobManager:
    """任务执行器：submit 提交、后台线程驱动技能、get 查询、resume 接续。

    _active 是“正在跑的”内存表；跑完会移除（历史数据已在 SQLite，随时能 load 回来）。
    """

    def __init__(self):
        self._active: dict[str, runtime.Execution] = {}   # job_id → 本次执行现场
        self._lock = threading.Lock()       # 保护 _active 的锁（提交/移除并发时防竞态）

    def submit(self, skill, args: dict, user_id: str = "", job_id: str = "", task_metadata: dict | None = None) -> Job:
        """接一个任务：记进表、开 daemon 线程跑，立刻把 Job 还给调用方（不阻塞）。

        user_id：谁提的任务（登录用户；隔离历史列表用）。
        """
        if job_id:
            existing = runtime.load_job(job_id)
            if existing:
                if existing.user_id != user_id or existing.skill_id != skill.id or existing.args != args:
                    raise ValueError("任务编号已被使用")
                return existing
        job = Job(skill.id, args)
        if job_id:
            job.id = job_id                 # 宿主先持久化附件轮次引用，再启动 worker，避免抢跑
        job.user_id = user_id or ""
        from station import observability as obs
        origin = obs.current()
        if origin and origin.record["user_id"] == job.user_id:
            job.result = {"_trace_origin": {"trace_id": origin.record["id"],
                                           "thread_id": origin.record["thread_id"]}}
            with obs.span("job_submit", "提交后台任务", job_id=job.id, skill=skill.id):
                pass
        metadata=dict(task_metadata or {})
        caller=runtime.current_origin()
        if caller and caller[0]==job.user_id and caller[1]:metadata['thread_id']=caller[1]
        if origin and origin.record['user_id']==job.user_id:
            metadata.update(thread_id=origin.record['thread_id'],trace_id=origin.record['id'])
        if not job.result and metadata.get('thread_id') and metadata.get('trace_id'):
            job.result={'_trace_origin':{'thread_id':metadata['thread_id'],'trace_id':metadata['trace_id']}}
        if not job_records.task_job_register(job.to_record(),metadata):
            return runtime.load_job(job.id)
        execution = runtime.Execution(job)
        if not runtime.prepare(execution,metadata):
            return job
        with self._lock:
            self._active[job.id] = execution          # 放进“运行中”表
        # 开一个后台线程执行 _run(job, skill)；daemon=True → 主程序退出时线程跟着退，不留孤儿
        threading.Thread(target=self._run, args=(execution, skill), daemon=True).start()
        return job

    def _run(self, execution: runtime.Execution, skill):
        job = execution.job
        from station import observability as obs
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(5):
                try:
                    if not job_records.task_heartbeat(job.id,job.user_id,execution.token,runtime.LEASE_SECONDS):return
                except Exception:return
        monitor=threading.Thread(target=heartbeat,daemon=True)
        monitor.start()
        try:
            origin = (job.result or {}).get("_trace_origin")
            if not origin:
                return self._run_impl(execution, skill)
            trace = obs.begin(job.user_id, origin["thread_id"], kind="job", key="job:"+job.id,
                              origin=origin["trace_id"], attributes={"job_id":job.id,"skill":skill.id})
            with obs.activate(trace):
                if trace:runtime.checkpoint(execution,trace_id=trace.record['id'])
                try:
                    with obs.span("job", "后台任务", job_id=job.id, skill=skill.id) as step:
                        self._run_impl(execution, skill)
                        step.update(artifacts=list(job.artifacts))
                        if job.status == "failed":
                            step.error("JobFailed")
                    obs.finish(trace, {"failed":"error","interrupted":"interrupted","cancelled":"cancelled"}.get(job.status,"success"))
                except BaseException:
                    obs.finish(trace, "interrupted")
                    raise
        finally:
            stop.set()
            try:
                monitor.join()
                job_records.task_release(job.id,job.user_id,execution.token)
            finally:
                # 追踪收尾也完成后再移出 active，避免恢复/测试切库时还有尾部写入。
                with self._lock:self._active.pop(job.id,None)

    def _run_impl(self, execution: runtime.Execution, skill):
        """worker 线程的主方法：喂着 skill.build_runner 生成器跑，直到结束/抛错。"""
        job = execution.job
        # 给这次任务也造一个 Context（技能有独立数据目录 + key 白名单 + 归属用户）
        ctx = Context(skill_id=skill.id,
                      data_dir=config.sub("skills", skill.id),
                      allowed_keys=list(skill.keys or []),
                      auto_approve=config.AUTO_APPROVE,
                      user_id=job.user_id or "",
                      job_id=job.id)
        ctx.task_checkpoint=lambda **values:runtime.checkpoint(execution,**values)
        ctx.task_cancel=lambda:runtime.check(execution)
        ctx.task_recovery=(job_records.task_control_get(job.id,job.user_id) or {}).get('record',{})
        # ★ 这条技能吃不吃内存（manifest 的 heavy 字段，见 skills/manifest.py 的说明）。
        #   **必须用 getattr 而不是 skill.heavy**：测试里的假技能（tests/core/test_heavy.py 的
        #   _Skill/_Slow）是裸类，根本没有这个属性，直接取会 AttributeError；而取不到时
        #   必须按"吃内存"处理 —— 否则那些"同时只跑一件"的用例会静默失去意义。
        heavy_job = getattr(skill, "heavy", True)
        # 闸里显示的名字：用技能自己的名字（用户看得懂），没有才回落到技能 id。
        skill_name = getattr(skill, "name", "") or job.skill_id
        gate=None
        gen=None
        try:
            runtime.check(execution)
            from station import observability as obs
            with obs.span("queue", "等待任务执行名额"):
                if heavy_job:
                    runtime.update(execution, status="queued",message="排队等待执行名额")
                    while True:
                        runtime.check(execution)
                        attempt=heavy.heavy_slot(f"{skill_name} {job.id}",wait=.2)
                        if attempt.__enter__():
                            gate=attempt;break
                        attempt.__exit__(None,None,None)
                else:
                    gate=contextlib.nullcontext();gate.__enter__()
            runtime.check(execution)
            runtime.update(execution, status="running",message="开始处理")
            gen=skill.build_runner(ctx,**job.args)
            if gen is None:raise RuntimeError("build_runner 未返回生成器")
            while True:
                runtime.check(execution)
                try:ev=next(gen)
                except StopIteration:break
                runtime.check(execution)
                if isinstance(ev,dict):runtime.append(execution, ev)
            runtime.update(execution, status="done",message="完成")
        except runtime.TaskCancelled:
            cleanup = getattr(getattr(skill, "task_recovery", None), "cancel", None)
            if callable(cleanup):
                cleanup(job)
            checkpoint=(job_records.task_control_get(job.id,job.user_id) or {}).get('record',{})
            runtime.update(execution, status="cancelled",message="已停止本地等待，远端任务可能仍在执行" if checkpoint.get('remote_id') else "已取消")
        except runtime.LeaseLost:
            job.status="interrupted"  # 已丢失执行权，不能覆盖另一实例保存的状态。
        except Exception as e:
            runtime.update(execution, status="failed",message=f"{type(e).__name__}: {e}")
            runtime.append(execution, {"type":"log","level":"error","text":traceback.format_exc()})
        finally:
            try:
                if gen is not None:
                    close=getattr(gen,"close",None)
                    if close:close()
            finally:
                if gate is not None:gate.__exit__(None,None,None)

    def get(self, job_id: str) -> dict | None:
        """查任务状态。正在跑看内存表，跑完了/重启过就从磁盘 load。"""
        with self._lock:
            execution = self._active.get(job_id)
        job = execution.job if execution else runtime.load_job(job_id)
        if not job:return None
        from station.tasks.runtime import state
        snapshot=runtime.snapshot(execution) if execution else job.snapshot()
        snapshot['status']=state(job,job_records.task_control_get(job.id,job.user_id))
        if snapshot['status']=='cancelling':snapshot['message']='取消请求已发送，正在安全停止'
        return snapshot

    def is_active(self, job_id: str) -> bool:
        """任务是否仍有本进程 worker；重启后库里 running 的旧 job 不算活着。"""
        with self._lock:
            return job_id in self._active

    def resume(self, job_id: str, skill) -> Job | None:
        """只恢复明确可重跑的宿主任务；不自动重放视频生成等外部副作用。"""
        with self._lock:
            if job_id in self._active:
                return self._active[job_id].job
            job = runtime.load_job(job_id)
            if job is None:
                return None
            control=job_records.task_control_get(job.id,job.user_id)
            if not control:
                return None
            from station.observability.recorder import instance_id
            local_instance=instance_id()
            if not local_instance or control['instance_id']!=local_instance:
                return None
            handler = getattr(skill, "task_recovery", None)
            can_resume = getattr(handler, "can_resume", None)
            if callable(can_resume) and not can_resume(control['record']):
                return None
            execution = runtime.Execution(job)
            if not runtime.prepare(execution):
                return None
            runtime.update(execution, status="queued", message="恢复后台处理")
            self._active[job_id] = execution
        threading.Thread(target=self._run, args=(execution, skill), daemon=True).start()
        return job


    def append(self, job_id: str, event: dict) -> None:
        """给正在执行的任务追加已有进度事件；查询快照不承担写入职责。"""
        with self._lock:
            execution = self._active.get(job_id)
        if execution is not None:
            runtime.append(execution, event)

_manager: JobManager | None = None


def get_manager() -> JobManager:
    """进程内唯一任务执行器；查询、工具提交和恢复复用同一实例。"""
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
