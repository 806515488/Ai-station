"""JobManager —— 单用户版异步任务：进程内 worker 线程 + 落盘状态。

状态机：queued → running → done | failed | cancelled；失联/旧记录按租约解释为 interrupted/unknown
  （确认门/awaiting_confirm 已随对话式改造删除 09-07：识别跑完即 done、
  现场落 DB，之后核对/出件由对话里的工具完成——见 skills/archive。）

事件：log 里追加 {"type": progress|artifact|log, ...}，供 SSE 与前端。
产物：skill 生成器 yield {"type":"artifact","id":...} 时登记进 job.artifacts。

契约（pipeline 入口 build_runner(ctx, **args)）：
  生成器逐个 yield 事件 dict（progress/artifact/log）；迭代完 = done；抛异常 = failed。

新手视角（Java 朋友版）：这里就是你的“线程池 + 任务表”。
  - 为什么异步？档案/生视频要跑几十秒到几分钟；不能让 HTTP 请求一直干等（会超时）。
    Java 常用 ExecutorService 丢线程池；这里更朴素——每个任务直接 new 一个 daemon 线程。
  - Job 记录 ≈ 数据库里的一行任务：状态/进度/消息/产物，且每步都写 DB → 崩了重启能找回。
  - 线程安全：worker 线程在写 job，浏览器在读 job —— 用 threading.Lock 保护（≈ synchronized）。
"""
from __future__ import annotations

import contextlib           # nullcontext()：什么都不做的 with ——"不领牌子"那一支用它
import threading            # 线程 + Lock（≈ Java 的 Thread / synchronized）
import time
import traceback            # 拿完整异常堆栈（排查失败原因用）
import uuid

from station import config
from station.core import heavy      # 重活闸：全站同时只跑一件吃内存的活（见该模块说明）
from station.core.context import Context


def _now() -> float:
    """当前 Unix 时间戳（秒），给 created/updated 排序、判断新旧用。"""
    return time.time()


class Job:
    """一个异步任务的数据记录（≈一张任务表的一行 + 一把锁）。"""

    def __init__(self, skill_id: str, args: dict):
        self.id = uuid.uuid4().hex[:16]   # 随机 16 位 id，当文件/接口的“取件码”
        self.skill_id = skill_id          # 跑哪个技能
        self.args = args                  # 传给 build_runner 的参数（dict≈Map）
        self.status = "queued"            # 状态机：queued→running→done/failed
        self.progress = 0                 # 0-100 进度
        self.message = ""                 # 给人看的一句话进度
        self.created = self.updated = _now()
        self.log: list[dict] = []         # 事件流水（progress/artifact/log…）
        self.artifacts: list[str] = []    # 产出的文件 id 列表
        self.result: dict | None = None   # 预留：技能想挂额外结果时用（确认门已删）
        self.user_id = ""                 # 归属用户（server 提交时填；历史列表按它隔离）
        self._lock = threading.Lock()     # 互斥锁：所有读写都进 with self._lock: 保护

    # ── 线程安全更新 + 落盘 ──────────────────────────────────────
    def update(self, **kw):
        """批量更新若干字段（如 status/message），随后落盘。

        **kw ≈ Java 的 Map 参数：调用 update(status="done", message="完成")
        会收到 kw={"status":"done","message":"完成"}。
        """
        with self._lock:                  # 进锁（with ≈ try/finally 自动 unlock）
            for k, v in kw.items():       # 遍历“字段名→新值”
                setattr(self, k, v)       # setattr≈反射：self.status = v 这种动态赋值
            self.updated = _now()
        self._persist()                   # 锁外落盘（写文件不占锁，减少互斥时间）

    def append(self, ev: dict):
        """追加一条事件并顺带更新派生字段（progress/message/artifacts）。"""
        with self._lock:
            self.log.append(ev)
            if ev.get("type") == "progress":      # 进度事件 → 刷新 progress+message
                self.progress = ev.get("percent", self.progress)
                self.message = ev.get("message", self.message)
            elif ev.get("type") == "artifact":    # 产物事件 → 记录文件 id
                self.artifacts.append(ev.get("id"))
            self.updated = _now()
        self._persist()

    def _persist(self):
        """把整个 Job 写进 SQLite（jobs 表，见 station/db.py）——崩了重启也能找回。"""
        from station import db
        if getattr(self, '_lease_token', ''):
            from station.tasks.runtime import LeaseLost, LEASE_SECONDS
            if not db.task_heartbeat(self.id,self.user_id,self._lease_token,LEASE_SECONDS):
                raise LeaseLost()
        db.job_save(self)                 # UPSERT：有则更新无则插入（db 侧自己有写锁）

    def snapshot(self, with_log: bool = True) -> dict:
        """返回一个“拷贝”dict 给接口用（带锁读取，避免读到半写状态）。"""
        with self._lock:
            return {"id": self.id, "skill_id": self.skill_id,
                    "status": self.status, "progress": self.progress,
                    "message": self.message, "created": self.created,
                    "updated": self.updated,
                    "log": list(self.log) if with_log else [],   # 拷贝新 list，防外部改到内部
                    "artifacts": list(self.artifacts),
                    "result": self.result}

    @classmethod
    def load(cls, job_id: str) -> "Job | None":
        """从 SQLite 读回一个 Job（重启后恢复用）。找不到返回 None。

        @classmethod ≈ Java 静态工厂：cls 就是 Job 类自己。
        """
        from station import db
        return db.job_load(job_id)


class JobManager:
    """任务调度器：接活(submit)、跑活(worker 线程)、查活(get)、推活(stream)。

    _active 是“正在跑的”内存表；跑完会移除（历史数据已在 SQLite，随时能 load 回来）。
    """

    def __init__(self):
        self._active: dict[str, Job] = {}   # 内存表：job_id → Job（≈ Map<String,Job>）
        self._lock = threading.Lock()       # 保护 _active 的锁（提交/移除并发时防竞态）

    def submit(self, skill, args: dict, user_id: str = "", job_id: str = "", task_metadata: dict | None = None) -> Job:
        """接一个任务：记进表、开 daemon 线程跑，立刻把 Job 还给调用方（不阻塞）。

        user_id：谁提的任务（登录用户；隔离历史列表用）。
        """
        from station import db
        from station.tasks import runtime
        if job_id:
            existing = Job.load(job_id)
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
        if not db.task_job_register(job,metadata):
            return Job.load(job.id)
        if not runtime.prepare(job,metadata):
            return job
        with self._lock:
            self._active[job.id] = job          # 放进“运行中”表
        # 开一个后台线程执行 _run(job, skill)；daemon=True → 主程序退出时线程跟着退，不留孤儿
        threading.Thread(target=self._run, args=(job, skill), daemon=True).start()
        return job

    def _run(self, job: Job, skill):
        from station import db,observability as obs
        from station.tasks import runtime
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(5):
                try:
                    if not db.task_heartbeat(job.id,job.user_id,job._lease_token,runtime.LEASE_SECONDS):return
                except Exception:return
        monitor=threading.Thread(target=heartbeat,daemon=True)
        monitor.start()
        try:
            origin = (job.result or {}).get("_trace_origin")
            if not origin:
                return self._run_impl(job, skill)
            trace = obs.begin(job.user_id, origin["thread_id"], kind="job", key="job:"+job.id,
                              origin=origin["trace_id"], attributes={"job_id":job.id,"skill":skill.id})
            with obs.activate(trace):
                if trace:runtime.checkpoint(job,trace_id=trace.record['id'])
                try:
                    with obs.span("job", "后台任务", job_id=job.id, skill=skill.id) as step:
                        self._run_impl(job, skill)
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
                db.task_release(job.id,job.user_id,job._lease_token)
            finally:
                # 追踪收尾也完成后再移出 active，避免恢复/测试切库时还有尾部写入。
                with self._lock:self._active.pop(job.id,None)

    def _run_impl(self, job: Job, skill):
        """worker 线程的主方法：喂着 skill.build_runner 生成器跑，直到结束/抛错。"""
        # 给这次任务也造一个 Context（技能有独立数据目录 + key 白名单 + 归属用户）
        ctx = Context(skill_id=skill.id,
                      data_dir=config.sub("skills", skill.id),
                      allowed_keys=list(skill.keys or []),
                      auto_approve=config.AUTO_APPROVE,
                      user_id=job.user_id or "",
                      job_id=job.id)
        from station import db
        from station.tasks import runtime
        ctx.task_checkpoint=lambda **values:runtime.checkpoint(job,**values)
        ctx.task_cancel=lambda:runtime.check(job)
        ctx.task_recovery=(db.task_control_get(job.id,job.user_id) or {}).get('record',{})
        # ★ 这条技能吃不吃内存（manifest 的 heavy 字段，见 skills/manifest.py 的说明）。
        #   **必须用 getattr 而不是 skill.heavy**：测试里的假技能（tests/test_heavy.py 的
        #   _Skill/_Slow）是裸类，根本没有这个属性，直接取会 AttributeError；而取不到时
        #   必须按"吃内存"处理 —— 否则那些"同时只跑一件"的用例会静默失去意义。
        heavy_job = getattr(skill, "heavy", True)
        # 闸里显示的名字：用技能自己的名字（用户看得懂），没有才回落到技能 id。
        skill_name = getattr(skill, "name", "") or job.skill_id
        gate=None
        gen=None
        try:
            runtime.check(job)
            from station import observability as obs
            with obs.span("queue", "等待任务执行名额"):
                if heavy_job:
                    job.update(status="queued",message="排队等待执行名额")
                    while True:
                        runtime.check(job)
                        attempt=heavy.heavy_slot(f"{skill_name} {job.id}",wait=.2)
                        if attempt.__enter__():
                            gate=attempt;break
                        attempt.__exit__(None,None,None)
                else:
                    gate=contextlib.nullcontext();gate.__enter__()
            runtime.check(job)
            job.update(status="running",message="开始处理")
            gen=skill.build_runner(ctx,**job.args)
            if gen is None:raise RuntimeError("build_runner 未返回生成器")
            while True:
                runtime.check(job)
                try:ev=next(gen)
                except StopIteration:break
                runtime.check(job)
                if isinstance(ev,dict):job.append(ev)
            job.update(status="done",message="完成")
        except runtime.TaskCancelled:
            if job.skill_id=='station-documents':
                turn=db.attachment_turn_get(job.args.get('turn_id',''),job.user_id)
                if turn:
                    db.attachment_turn_update(turn['id'],job.user_id,{'state':'cancelled'},expected=('waiting_parse',))
                    for did,rid in turn.get('run_refs',{}).items():
                        doc=db.document_get(did,job.user_id)
                        if not doc:continue
                        runs=dict(doc.get('runs',{}));run=dict(runs.get(rid,{}))
                        if run.get('state') not in ('parsing','parsed_ready'):continue
                        run['state']='partial' if run.get('manifest') else 'cancelled';runs[rid]=run
                        values={'runs':runs}
                        if doc.get('latest_run')==rid:values['status']=run['state']
                        db.document_update(did,job.user_id,values)
            checkpoint=(db.task_control_get(job.id,job.user_id) or {}).get('record',{})
            job.update(status="cancelled",message="已停止本地等待，远端任务可能仍在执行" if checkpoint.get('remote_id') else "已取消")
        except runtime.LeaseLost:
            job.status="interrupted"  # 已丢失执行权，不能覆盖另一实例保存的状态。
        except Exception as e:
            job.update(status="failed",message=f"{type(e).__name__}: {e}")
            job.append({"type":"log","level":"error","text":traceback.format_exc()})
        finally:
            try:
                if gen is not None:
                    close=getattr(gen,"close",None)
                    if close:close()
            finally:
                if gate is not None:gate.__exit__(None,None,None)

    def get(self, job_id: str) -> dict | None:
        """查任务状态。正在跑看内存表，跑完了/重启过就从磁盘 load。"""
        job = self._active.get(job_id)
        if job is None:
            job = Job.load(job_id)              # 磁盘兜底
        if not job:return None
        from station import db
        from station.tasks.runtime import state
        snapshot=job.snapshot()
        snapshot['status']=state(job,db.task_control_get(job.id,job.user_id))
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
                return self._active[job_id]
            job = Job.load(job_id)
            if job is None:
                return None
            from station.tasks import runtime
            from station import db
            control=db.task_control_get(job.id,job.user_id)
            if not control:
                return None
            from station.observability.recorder import instance_id
            local_instance=instance_id()
            if not local_instance or control['instance_id']!=local_instance:
                return None
            if job.skill_id in ('video','interview-coach') and not control['record'].get('remote_id'):
                return None
            if not runtime.prepare(job):
                return None
            job.update(status="queued", message="恢复后台处理")
            self._active[job_id] = job
        threading.Thread(target=self._run, args=(job, skill), daemon=True).start()
        return job

    # stream()（SSE 推流）已删（09-11）：它的唯一调用方是 GET /api/jobs/{id}/events，
    # 而前端从头到尾都是**轮询** self.get(job_id)，没有任何 EventSource 连过那条流。


# 单例：进程里只允许一个 JobManager。模块顶层变量 ≈ Java static 字段。
_manager: JobManager | None = None


def get_manager() -> JobManager:
    """取全局唯一的 manager；没有就建一个（懒加载 ≈ Java 单例的 double-check）。"""
    global _manager             # 要“改模块级变量”必须声明 global（原理见会话里 global 那段）
    if _manager is None:
        _manager = JobManager()
    return _manager
