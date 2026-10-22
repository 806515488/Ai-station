"""interview-coach 技能入口：FFmpeg 指导 + 音频转写 + 后台执行器。"""
from __future__ import annotations

from station.tasks.runtime import load_job


import interview_repository as interview_records


import json
import time
from pathlib import Path

from station.core.tool import Tool


def task_recovery():
    import interview_tasks
    return interview_tasks


def repository():
    """面试数据访问入口；模块与技能一起加载，不要求宿主认识业务 Python 包。"""
    import interview_repository
    return interview_repository


_SKILL_DIR = Path(__file__).resolve().parents[1]


def _read_prompt(name: str) -> str:
    """读取技能目录下的提示词/说明文件。"""
    return (_SKILL_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


def _owner(ctx) -> str:
    """对话场景取 thread.user_id；job 场景回落 ctx.user_id。"""
    t = getattr(ctx, "thread", None)
    return ((getattr(t, "user_id", "") if t else "")
            or getattr(ctx, "user_id", "") or "")


def _thread_job_id(ctx) -> str:
    t = getattr(ctx, "thread", None)
    return ((getattr(t, "meta", {}) or {}).get("job_id") or "") if t else ""


def _thread_interview_id(ctx) -> str:
    """当前会话选中的面试档案。音频/转写绝不能只靠“最近一次 job”猜归属。"""
    t = getattr(ctx, "thread", None)
    return ((getattr(t, "meta", {}) or {}).get("interview_id") or "") if t else ""


def _remember_interview(ctx, interview_id: str) -> None:
    t = getattr(ctx, "thread", None)
    if t is not None:
        t.meta["interview_id"] = interview_id


def _current_interview(ctx) -> tuple[dict | None, str]:
    """取当前会话档案并做 owner 校验；没有就让用户显式创建/切换，绝不猜。"""
    iid = _thread_interview_id(ctx)
    row = interview_records.interview_get(iid, _owner(ctx)) if iid else None
    if row is None:
        return None, "当前会话还没有选中面试档案。先说“新建一次面试”，或“列出我的面试”。"
    return row, ""


def _remember_job(ctx, job_id: str) -> None:
    t = getattr(ctx, "thread", None)
    if t is not None:
        t.meta["job_id"] = job_id
        t.meta["interview_job_id"] = job_id


def _check_file(ctx, fid: str) -> tuple[str, str]:
    """校验文件存在且属于当前用户；返回 (path, error)。"""
    from station.files import store as fs
    fid = (fid or "").strip()
    if not fid:
        return "", "还没有音频文件。先上传音频，或在对话里说「上传面试音频」。"
    p = fs.path(fid)
    if p is None:
        return "", "这个音频文件已经不在了，请重新上传。"
    owner = (fs.meta(fid) or {}).get("owner") or ""
    if owner and owner != _owner(ctx):
        return "", "这个音频文件不属于当前账号。"
    return str(p), ""


def _check_job(ctx, job_id: str):
    """校验任务属于当前用户；返回 (snapshot, error)。"""
    from station.tasks.executor import get_manager
    jid = (job_id or "").strip() or _thread_job_id(ctx)
    if not jid:
        return None, "这个会话还没有语音识别任务。"
    row = load_job(jid)
    if row is None or getattr(row, "user_id", "") != _owner(ctx):
        return None, "找不到这个语音识别任务。"
    snap = get_manager().get(jid)
    if snap is None:
        return None, "找不到这个语音识别任务。"
    return snap, ""


def ffmpeg_guide(ctx) -> str:
    """给用户一份可直接照着做的 FFmpeg Windows 本地处理说明。"""
    return _read_prompt("ffmpeg-guide")


def create_interview(ctx, title: str = "", company: str = "", role: str = "",
                     round_name: str = "") -> str:
    """新建并选中一份面试档案；日期暂取创建时间，用户可在对话里补充。"""
    title = (title or "").strip() or time.strftime("%m-%d 面试")
    row = interview_records.interview_create(_owner(ctx), title, company=company, role=role,
                              round_name=round_name, occurred_at=time.time())
    _remember_interview(ctx, row["id"])
    return f"已新建并选中「{row['title']}」（档案 {row['id']}）。现在可上传第一段面试音频。"


# ★★ 下面所有工具的返回文字里，id 一律给**完整值**，不许 [:8] 截断。
#   原因：宿主 core/agent.run_tool 只把返回值的 `text` 交给模型，`render` 只发给前端 ——
#   模型看不见卡片里的完整 id。而 select_interview / show_question / set_action_status
#   这些"按 id 操作"的工具要的是完整 id。列表里给 8 位截断 id，模型就永远无法把它交给
#   这些工具，只能一遍遍重列（线上真发生过：模型自己都打出"列表里给我的是截断编号"）。
#   想省 token 也不能省这里 —— 断掉的是"模型能不能干活"这条链。


def list_interviews(ctx) -> str:
    """列出本人最近面试档案，让用户按标题/编号切换，避免多场面试串在一起。"""
    rows = interview_records.interview_list(_owner(ctx))
    if not rows:
        return "还没有面试档案。说“新建一次面试”后再上传音频。"
    cur = _thread_interview_id(ctx)
    lines = ["我的面试档案："]
    for r in rows:
        selected = "（当前）" if r["id"] == cur else ""
        lines.append(f"- {r['id']} · {r['title']} {selected}"
                     f"：录音 {r.get('recording_count', 0)} 段，已转写 {r.get('done_count', 0)} 段")
    return "\n".join(lines)


def select_interview(ctx, interview_id: str) -> str:
    """切换当前会话正在操作的面试档案；后续上传与转写都固定归到它。"""
    iid = (interview_id or "").strip()
    row = interview_records.interview_get(iid, _owner(ctx))
    if row is None:
        return "找不到这份面试档案。先用“列出我的面试”查看可选编号。"
    _remember_interview(ctx, iid)
    return f"已切换到「{row['title']}」。后续上传的音频会追加到这次面试。"


def show_interview(ctx) -> str:
    """显示当前面试与多段录音的状态，便于决定继续上传、转写还是开始复盘。"""
    row, err = _current_interview(ctx)
    if err:
        return err
    recs = interview_records.interview_recordings(row["id"], _owner(ctx))
    try:
        speaker_map = json.loads(row.get("speaker_map") or "{}")
    except Exception:
        speaker_map = {}
    who = "；说话人未确认" if not speaker_map else "；" + "、".join(
        f"{k}={v}" for k, v in speaker_map.items())
    head = f"当前面试：{row['title']}（{row['status']}{who}）"
    if not recs:
        return {"text": head + "\n还没有音频。可以说“上传面试音频”。",
                "render": {"type":"interview-card","title":row["title"],"status":row["status"],"recordings":[]}}
    lines = [head]
    for r in recs:
        # 录音行必须带上**完整 recording_id**：retry_transcribe / delete_recording
        # 都要它，而模型看不到 render 卡片。文件名只是给人看的，不能当参数用。
        lines.append(f"- 第 {r['sequence']} 段《{r['display_name'] or '未命名音频'}》（录音 {r['id']}）：{r['status']}"
                     + (f"（{r['error_message']}）" if r.get("error_message") else ""))
    return {"text": "\n".join(lines), "render": {"type":"interview-card","title":row["title"],
            "status":row["status"],"recordings":[{"id":r["id"],"sequence":r["sequence"],"name":r["display_name"],"status":r["status"]} for r in recs]}}


def set_speakers(ctx, candidate_speaker: str, interviewer_speaker: str) -> str:
    """确认 ASR 的 speaker 编号归属；不确认就不应把 Q&A 草稿当成可靠事实。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    candidate, interviewer = (candidate_speaker or "").strip(), (interviewer_speaker or "").strip()
    if not candidate.startswith("speaker_") or not interviewer.startswith("speaker_") or candidate == interviewer:
        return "请明确填写两个不同的 ASR 编号，例如 candidate_speaker=speaker_0、interviewer_speaker=speaker_1。"
    interview_records.interview_set_speaker_map(interview["id"], _owner(ctx), {
        candidate: "candidate", interviewer: "interviewer"})
    return f"已确认：{candidate} 是你，{interviewer} 是面试官。之后整理 Q&A 会按此归属。"


def ask_audio(ctx) -> dict:
    """发一张音频上传卡片。"""
    row, err = _current_interview(ctx)
    if err:
        return {"text": err}
    return {
        "text": f"请上传要追加到「{row['title']}」的面试音频。",
        "render": {
            "type": "upload-card",
            "purpose": "audio",
            "interview_id": row["id"],
            "hint": "选择本地提取好的面试音频（建议单声道 M4A/WAV）",
        },
    }


def transcribe(ctx, file_id: str = "", force: bool = False) -> dict:
    """提交后台语音识别任务，立即返回一张 job 进度卡。"""
    from station import config
    from station.tasks.executor import get_manager
    from station.skills.registry import get_registry


    interview, err = _current_interview(ctx)
    if err:
        return err
    fid = (file_id or "").strip()
    if not fid:
        return ask_audio(ctx)
    _, err = _check_file(ctx, fid)
    if err:
        return err
    recording = interview_records.interview_recording_by_file(interview["id"], _owner(ctx), fid)
    if recording is None:
        # 兼容升级前已上传的音频：用户明确在当前面试里转写时，才把它补挂到这里。
        from station.files import store as fs
        recording = interview_records.interview_recording_add(interview["id"], _owner(ctx), fid,
                                               (fs.meta(fid) or {}).get("name") or "")
    if recording.get("status") == "transcribing" and recording.get("asr_job_id") and not force:
        return {"text": "这段音频正在转写中。",
                "render": {"type": "job", "job_id": recording["asr_job_id"]}}
    try:
        import asr
        asr.resolve_entry(ctx)
    except Exception as e:                  # noqa：缺配置是常见情况，直接给人话
        return str(e)
    if not config.public_base():
        return config.public_base_hint()

    skill = get_registry().get(ctx.skill_id or "interview-coach")
    if skill is None or skill.build_runner is None:
        return "语音识别执行器没有装载，重启工作站再试。"
    import uuid
    job_id=uuid.uuid4().hex[:16]
    interview_records.interview_recording_start(recording["id"], _owner(ctx), job_id)
    try:
        job = get_manager().submit(skill, {
            "file_id": fid, "interview_id": interview["id"], "recording_id": recording["id"],
        }, user_id=_owner(ctx),job_id=job_id)
    except Exception as exc:
        interview_records.interview_recording_finish(recording["id"],_owner(ctx),error_message=type(exc).__name__,expected_job_id=job_id)
        raise
    _remember_job(ctx, job.id)
    return {
        "text": (f"开始转写（任务 {job.id}）。音频会生成临时访问链接交给百炼，"
                 f"任务结束后自动失效；进度在这条任务卡里。"),
        "render": {"type": "job", "job_id": job.id},
    }


def retry_transcribe(ctx, recording_id: str = "") -> dict | str:
    """用户明确要求时重试一段卡住/失败的录音，新 job 会接管旧 job 的回写权。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    recs = interview_records.interview_recordings(interview["id"], _owner(ctx))
    rec = next((r for r in recs if r["id"] == recording_id), None) if recording_id else (recs[-1] if recs else None)
    if rec is None:
        return "找不到要重试的录音。"
    return transcribe(ctx, rec["file_id"], force=True)


def delete_recording(ctx, recording_id: str) -> str:
    """批准后删除一段录音及其转写；仅在没有其它面试记录引用时删物理文件。"""
    from station.files import store as fs
    files = interview_records.interview_recording_delete((recording_id or "").strip(), _owner(ctx))
    if not files:
        return "找不到这段录音。"
    removed = []
    for fid in files:
        if interview_records.interview_file_references(fid, _owner(ctx)) == 0 and fs.remove(fid, _owner(ctx)):
            removed.append(fid)
    return f"已删除录音记录；清理文件 {len(removed)}/{len(files)} 份。"


def delete_interview(ctx, interview_id: str = "") -> str:
    """批准后删除整场面试；只清理已经没有其它面试引用的音频/转写文件。"""
    from station.files import store as fs
    iid = (interview_id or _thread_interview_id(ctx) or "").strip()
    files = interview_records.interview_delete(iid, _owner(ctx))
    if not files:
        return "找不到这份面试档案。"
    removed = sum(1 for fid in files if interview_records.interview_file_references(fid, _owner(ctx)) == 0 and fs.remove(fid, _owner(ctx)))
    if iid == _thread_interview_id(ctx):
        _remember_interview(ctx, "")
    return f"已删除面试档案及关联记录；清理文件 {removed}/{len(files)} 份。"


def compare_interviews(ctx, interview_ids: list | None = None) -> str:
    """把用户明确选择的多场面试 Q&A 交给教练做横向洞察；不自动读取全部隐私档案。"""
    ids = [str(x) for x in (interview_ids or []) if str(x)]
    rows = interview_records.interview_questions_across(_owner(ctx), ids)
    if not rows:
        return "所选面试没有已保存的 Q&A。先整理并确认题目，再做跨面试比较。"
    lines = ["跨面试 Q&A（请仅基于以下内容总结重复弱点、进步和练习方向）："]
    for q in rows[:80]:
        lines.append(f"- 【{q['interview_title']}】{q['question_text']}\n  回答：{q['answer_text'][:500]}")
    return "\n".join(lines)


def job_status(ctx, job_id: str = "") -> str:
    """查语音识别任务状态。"""
    snap, err = _check_job(ctx, job_id)
    if err:
        return err
    st = snap.get("status") or ""
    if st == "done":
        return f"语音识别完成了（任务 {snap.get('id')}）。可以开始整理问答和复盘。"
    if st == "failed":
        return f"语音识别失败了（任务 {snap.get('id')}）：{snap.get('message') or '原因未提供'}"
    return (f"语音识别还在进行中：{snap.get('progress') or 0}% —— "
            f"{snap.get('message') or ''}")


def transcript(ctx, job_id: str = "", recording_id: str = "") -> str:
    """读取当前面试的一段或全部已完成转写；多段音频按用户确认顺序合并。"""
    from station.files import store as fs
    interview, err = _current_interview(ctx)
    if err:
        return err
    recs = interview_records.interview_recordings(interview["id"], _owner(ctx))
    if recording_id:
        recs = [r for r in recs if r["id"] == recording_id]
    elif job_id:
        recs = [r for r in recs if r.get("asr_job_id") == job_id]
    if not recs:
        return "当前面试找不到这段音频。先查看当前面试概况或上传音频。"
    pending = [r for r in recs if r.get("status") != "done"]
    if pending:
        return "还有音频未完成转写：" + "、".join(
            f"第 {r['sequence']} 段（{r['status']}）" for r in pending)

    lines = [f"面试转写：{interview['title']}"]
    try:
        speaker_map = json.loads(interview.get("speaker_map") or "{}")
    except Exception:
        speaker_map = {}
    for r in recs:
        p = fs.path(r.get("transcript_file_id") or "")
        if p is None:
            return f"第 {r['sequence']} 段的转写结果文件已经不在了。"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:                  # noqa
            return f"第 {r['sequence']} 段转写结果读取失败：{type(e).__name__}: {e}"
        lines.append(f"\n## 第 {r['sequence']} 段（录音 {r['id']}；{data.get('language') or '未知语言'}，"
                     f"{len(data.get('segments') or [])} 段）")
        for s in data.get("segments") or []:
            sp = s.get("speaker") or "unknown"
            role = speaker_map.get(sp) or sp
            lines.append(f"[{s.get('start',0):.1f}-{s.get('end',0):.1f}] {role}: {s.get('text','')}")
    return "\n".join(lines)


def list_questions(ctx) -> str:
    """列当前面试的 Q&A 草稿和已复盘题，让用户选择下一道要讨论的题。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    rows = interview_records.interview_question_list(interview["id"], _owner(ctx))
    if not rows:
        return "当前面试还没有整理出题目。先读取转写稿；确认问答无误后，我会保存题目草稿。"
    lines = [f"「{interview['title']}」的题目："]
    for q in rows:
        lines.append(f"- {q['id']} · 第 {q['sequence']} 题 [{q['status']}]：{q['question_text'][:100]}")
    return {"text": "\n".join(lines), "render": {"type":"interview-questions-card","items":[
        {"id":q["id"],"sequence":q["sequence"],"status":q["status"],"question":q["question_text"]} for q in rows]}}


def edit_question(ctx, question_id: str, question_text: str, answer_text: str, category: str = "", followup_text: str = "") -> str:
    q = interview_records.interview_question_get(question_id, _owner(ctx))
    if not q or q["interview_id"] != (_thread_interview_id(ctx) or ""):
        return "找不到当前面试的这道题。"
    try:
        interview_records.interview_question_update(question_id, _owner(ctx), question_text=question_text, answer_text=answer_text,
                                     category=category, followup_text=followup_text)
    except ValueError as e: return str(e)
    return "已更新题目草稿。"


def merge_questions(ctx, keep_id: str, merge_id: str) -> str:
    try: q = interview_records.interview_question_merge(keep_id, merge_id, _owner(ctx))
    except ValueError as e: return str(e)
    return f"已合并为第 {q['sequence']} 题。"


def split_question(ctx, question_id: str, first_question: str, first_answer: str, second_question: str, second_answer: str) -> str:
    try: pair = interview_records.interview_question_split(question_id, _owner(ctx), first_question=first_question, first_answer=first_answer, second_question=second_question, second_answer=second_answer)
    except ValueError as e: return str(e)
    return "已拆成两道题。" if pair else "拆分失败：第二题与已有题重复或内容为空。"


def save_questions(ctx, items: list) -> str:
    """保存经模型提取、并准备让用户校正的 Q&A 草稿；重复题自动跳过。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    added = interview_records.interview_question_add_many(interview["id"], _owner(ctx), items or [])
    if not added:
        return "没有保存新题目：每项都需要同时包含 question_text 和 answer_text，或与已有草稿重复。"
    lines = [f"已为「{interview['title']}」保存 {len(added)} 道 Q&A 草稿，请用户核对："]
    lines.extend(f"- {q['id']} · {q['question_text'][:100]}" for q in added)
    return "\n".join(lines)


def show_question(ctx, question_id: str) -> str:
    """读当前面试的一题及其复盘版本，供逐题讨论前精确定位，不跨面试取数。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    q = interview_records.interview_question_get((question_id or "").strip(), _owner(ctx))
    if q is None or q["interview_id"] != interview["id"]:
        return "找不到当前面试的这道题。先列出当前面试题目再选择。"
    lines = [f"第 {q['sequence']} 题 [{q['status']}]", f"问题：{q['question_text']}",
             f"原回答：{q['answer_text']}"]
    if q.get("followup_text"):
        lines.append(f"追问：{q['followup_text']}")
    reviews = interview_records.interview_reviews(q["id"], _owner(ctx))
    if reviews:
        latest = reviews[0]
        lines.append(f"最近复盘（第 {latest['version']} 版）：{latest['coach_feedback']}")
    return "\n".join(lines)


def confirm_question(ctx, question_id: str) -> str:
    """用户确认 Q&A 草稿无误后标记 confirmed；后续重新提取不应覆盖它。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    q = interview_records.interview_question_get((question_id or "").strip(), _owner(ctx))
    if q is None or q["interview_id"] != interview["id"]:
        return "找不到当前面试的这道题。"
    interview_records.interview_question_set_status(q["id"], _owner(ctx), "confirmed")
    return f"已确认第 {q['sequence']} 题。之后可以开始逐题复盘。"


def save_review(ctx, question_id: str, self_reflection: str = "", coach_feedback: str = "",
                improved_answer: str = "", drill_prompt: str = "") -> str:
    """把一轮单题复盘追加为新版本；不覆盖旧版本，保留练习过程。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    q = interview_records.interview_question_get((question_id or "").strip(), _owner(ctx))
    if q is None or q["interview_id"] != interview["id"]:
        return "找不到当前面试的这道题，不能保存复盘。"
    review = interview_records.interview_review_add(
        q["id"], _owner(ctx), self_reflection=self_reflection, coach_feedback=coach_feedback,
        improved_answer=improved_answer, drill_prompt=drill_prompt, source="ai")
    if review is None:
        return "保存复盘失败：题目不存在或不属于当前账号。"
    return f"已保存第 {q['sequence']} 题的第 {review['version']} 版复盘。"


def add_action(ctx, title: str, detail: str = "", question_id: str = "") -> str:
    """记录下一次面试前的练习行动项，可选关联当前面试的一题。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    try:
        action = interview_records.interview_action_add(interview["id"], _owner(ctx), title,
                                         detail=detail, question_id=question_id)
    except ValueError as e:
        return str(e)
    return f"已加入行动项 {action['id']}：{action['title']}"


def list_actions(ctx, status: str = "") -> str:
    """查看当前面试的行动项，默认列全部；可按 todo/doing/done/dismissed 过滤。"""
    interview, err = _current_interview(ctx)
    if err:
        return err
    rows = interview_records.interview_actions(interview["id"], _owner(ctx), status=status)
    if not rows:
        return "当前面试还没有行动项。"
    text = "\n".join(["当前面试行动项："] + [f"- {a['id']} · [{a['status']}] {a['title']}" for a in rows])
    return {"text":text,"render":{"type":"interview-actions-card","items":[{"id":a["id"],"title":a["title"],"status":a["status"]} for a in rows]}}


def set_action_status(ctx, action_id: str, status: str) -> str:
    """更新当前账号自己的行动项状态，例如完成一次模拟追问后标为 done。"""
    try:
        action = interview_records.interview_action_set_status((action_id or "").strip(), _owner(ctx), status)
    except ValueError as e:
        return str(e)
    if action is None:
        return "找不到这条行动项。"
    return f"行动项「{action['title']}」已更新为 {action['status']}。"


def build_runner(ctx, file_id: str = "", interview_id: str = "", recording_id: str = ""):
    """后台执行 Qwen Filetrans，并把归一化转写结果写成 job artifact。"""
    from station.files import share
    from station.files import store as fs


    fid = (file_id or "").strip()
    if not fid:
        raise ValueError("没有音频文件")
    if not fs.path(fid) and not (getattr(ctx,'task_recovery',{}) or {}).get('remote_id'):
        raise ValueError("音频文件不存在")
    owner = _owner(ctx)
    recording = interview_records.interview_recording_get(recording_id, owner)
    if recording is None or recording.get("interview_id") != interview_id or recording.get("file_id") != fid:
        raise ValueError("音频与面试档案归属不一致")
    recovery=getattr(ctx,"task_recovery",{}) or {}
    token = None if recovery.get("remote_id") else share.make_ref_link(fid, owner, ttl=max(share.link_ttl(), 14400.0))
    try:
        url = share.ref_url(token) if token else ""
        import asr
        result = None
        for ev in asr.iter_transcribe(
                ctx, url,
                language_hints=["zh", "en"],
                diarization_enabled=True,
                poll_interval=5.0,
                poll_deadline=7200.0):
            if ev.get("type") == "result":
                result = ev["result"]
            else:
                yield ev
        if not isinstance(result, dict):
            raise RuntimeError("语音识别没有返回结构化结果")
        stamp = time.strftime("%m%d-%H%M%S")
        tfid = fs.save_text(
            json.dumps(result, ensure_ascii=False),
            ".json", name=f"面试转写-{stamp}.json",
            owner=owner, skill_id="interview-coach",
            group_key=f"interview-transcript:{ctx.job_id or stamp}",
            group_label="面试转写结果",
            extra={"purpose": "interview_transcript"},
        )
        interview_records.interview_recording_finish(recording_id, owner, transcript_file_id=tfid,
                                      asr_model=str(result.get("model") or ""),
                                      expected_job_id=ctx.job_id or "")
        yield {"type": "artifact", "id": tfid}
        yield {"type": "log",
               "text": f"transcript segments={len(result.get('segments') or [])}"}
        yield {"type": "progress", "percent": 100, "message": "转写完成"}
    except Exception as e:                    # noqa：任务失败也要让档案卡知道哪段失败了
        interview_records.interview_recording_finish(recording_id, owner, error_message=f"{type(e).__name__}: {e}",
                                      expected_job_id=ctx.job_id or "")
        raise
    finally:
        if token:share.revoke([token])


def tools() -> list[Tool]:
    """注册 interview.* 工具；复盘推理仍由现有 LLM/agent 完成。"""
    return [
        Tool(
            name="create_interview",
            label="新建面试档案",
            description=(
                "新建并选中一次面试。用户要上传一场新面试的音频、或明确说这是另一家公司/轮次时调用。"
                "不要把不同面试的音频自动合并。"
            ),
            run=create_interview,
            args=[{"name": "title", "type": "str", "desc": "面试标题，如 A公司·后端开发·一面", "required": False},
                  {"name": "company", "type": "str", "desc": "公司名（可省）", "required": False},
                  {"name": "role", "type": "str", "desc": "岗位（可省）", "required": False},
                  {"name": "round_name", "type": "str", "desc": "轮次，如一面/二面（可省）", "required": False}],
        ),
        Tool(
            name="list_interviews",
            label="列出面试档案",
            description="列出当前账号的面试档案及每份的录音、转写进度；用户要切换或比较面试时调用。",
            run=list_interviews,
            args=[],
        ),
        Tool(
            name="select_interview",
            label="切换当前面试",
            description="按面试档案 id 切换当前会话的操作对象。上传、转写和读取文字稿都会归到当前面试。",
            run=select_interview,
            args=[{"name": "interview_id", "type": "str", "desc": "list_interviews 返回的完整档案 id", "required": True}],
        ),
        Tool(
            name="show_interview",
            label="查看当前面试概况",
            description="显示当前面试的录音段数、转写状态与失败原因。",
            run=show_interview,
            args=[],
        ),
        Tool(
            name="set_speakers",
            label="确认说话人身份",
            description=(
                "把 ASR 的两个 speaker 编号明确标为 candidate 和 interviewer。"
                "整理 Q&A 前、说话人身份未知时必须先向用户确认再调用。"
            ),
            run=set_speakers,
            args=[{"name": "candidate_speaker", "type": "str", "desc": "用户本人对应的 speaker_编号", "required": True},
                  {"name": "interviewer_speaker", "type": "str", "desc": "面试官对应的 speaker_编号", "required": True}],
        ),
        Tool(
            name="ffmpeg_guide",
            label="查看 FFmpeg 使用说明",
            description=(
                "返回 FFmpeg 官方下载入口、Windows release essentials 安装说明，"
                "以及从面试视频提取音频的命令。用户问怎么下载 FFmpeg、视频怎么转音频、"
                "怎么在本地准备面试素材时调用。它只提取音频，不做语音转文字。"
            ),
            run=ffmpeg_guide,
            args=[],
        ),
        Tool(
            name="ask_audio",
            label="发音频上传卡片",
            description=(
                "发一张上传卡片，让用户上传已经从面试视频提取好的音频文件。"
                "用户想转写但还没提供音频时调用。"
            ),
            run=ask_audio,
            args=[],
        ),
        Tool(
            name="transcribe",
            label="转写面试音频",
            description=(
                "拿一份已经上传、且属于当前面试档案的音频 file_id 启动后台语音识别。"
                "用户消息形如「面试音频已上传：<file_id>（文件名，大小）」时，"
                "提取 file_id 调它。识别是后台任务，不会立刻返回全文。"
            ),
            run=transcribe,
            args=[{"name": "file_id", "type": "str",
                   "desc": "音频上传接口返回的文件 id", "required": False}],
        ),
        Tool(name="retry_transcribe", label="重新转写（新任务）", risk="approve",
             description="用户明确要求时，为失败或陈旧的转写任务创建新 job；旧 job 无权覆盖新结果。",
             run=retry_transcribe,
             args=[{"name":"recording_id","type":"str","desc":"录音 id（可省，默认当前面试最近一段）","required":False}]),
        Tool(
            name="status",
            label="查转写进度",
            description="查询语音识别任务状态（用户问「转写到哪了/好了吗」时用）。",
            run=job_status,
            args=[{"name": "job_id", "type": "str",
                   "desc": "任务号（可省，默认本会话最近一次）", "required": False}],
        ),
        Tool(
            name="transcript",
            label="读取转写全文",
            description=(
                "读取当前面试的一段或全部已完成转写，按音频顺序返回带时间戳和说话人编号的文本。"
                "整理问答、复盘回答前先调用它；不能跨面试混读。"
            ),
            run=transcript,
            args=[{"name": "job_id", "type": "str",
                   "desc": "指定某次转写任务（可省）", "required": False},
                  {"name": "recording_id", "type": "str",
                   "desc": "指定当前面试的一段录音（可省，省略则读全部已完成录音）", "required": False}],
        ),
        Tool(
            name="list_questions",
            label="列出当前面试题目",
            description="列出当前面试已保存的 Q&A 草稿/已确认题目；用户要逐题复盘或校正题目时调用。",
            run=list_questions,
            args=[],
        ),
        Tool(
            name="save_questions",
            label="保存问答草稿",
            description=(
                "在读取当前面试转写稿后，把模型已提取出的 Q&A 草稿保存下来。"
                "items 每项必须有 question_text 与 answer_text，可选 category、followup_text、tags。"
                "不要凭空编题；保存后要请用户核对。"
            ),
            run=save_questions,
            args=[{"name": "items", "type": "list", "desc": "Q&A 草稿数组", "required": True}],
        ),
        Tool(
            name="show_question",
            label="查看单题详情",
            description="读取当前面试的一道题、原回答和最近复盘；逐题分析前先调用。",
            run=show_question,
            args=[{"name": "question_id", "type": "str", "desc": "题目 id", "required": True}],
        ),
        Tool(
            name="confirm_question",
            label="确认问答草稿",
            description="用户确认题目与原回答无误后，标记为 confirmed，防止后续草稿混淆。",
            run=confirm_question,
            args=[{"name": "question_id", "type": "str", "desc": "题目 id", "required": True}],
        ),
        Tool(name="edit_question", label="编辑问答草稿", description="按用户核对结果编辑当前面试的一道题和原回答。",
             run=edit_question,
             args=[{"name":"question_id","type":"str","desc":"题目 id","required":True},{"name":"question_text","type":"str","desc":"修正后的问题","required":True},{"name":"answer_text","type":"str","desc":"修正后的原回答","required":True},{"name":"category","type":"str","desc":"分类（可省）","required":False},{"name":"followup_text","type":"str","desc":"追问（可省）","required":False}]),
        Tool(name="merge_questions", label="合并重复题目", description="合并当前面试中重复拆出的两道题；已有复盘版本的题不允许自动合并。",
             run=merge_questions, args=[{"name":"keep_id","type":"str","desc":"保留的题目 id","required":True},{"name":"merge_id","type":"str","desc":"合并进去的题目 id","required":True}]),
        Tool(name="split_question", label="拆分题目", description="把当前面试中混在一起的一道题拆成两道独立问答。",
             run=split_question, args=[{"name":"question_id","type":"str","desc":"原题 id","required":True},{"name":"first_question","type":"str","desc":"第一题问题","required":True},{"name":"first_answer","type":"str","desc":"第一题回答","required":True},{"name":"second_question","type":"str","desc":"第二题问题","required":True},{"name":"second_answer","type":"str","desc":"第二题回答","required":True}]),
        Tool(
            name="save_review",
            label="保存单题复盘",
            description=(
                "完成一题复盘后保存一个新版本：可记录用户自评、教练反馈、改写答案和下一轮追问练习。"
                "先讨论、后保存；不得凭空编造用户经历。"
            ),
            run=save_review,
            args=[{"name": "question_id", "type": "str", "desc": "题目 id", "required": True},
                  {"name": "self_reflection", "type": "str", "desc": "用户自评（可省）", "required": False},
                  {"name": "coach_feedback", "type": "str", "desc": "基于原回答的具体反馈", "required": False},
                  {"name": "improved_answer", "type": "str", "desc": "保留用户真实经历的改写答案", "required": False},
                  {"name": "drill_prompt", "type": "str", "desc": "下一轮追问练习（可省）", "required": False}],
        ),
        Tool(
            name="add_action",
            label="添加练习行动项",
            description="把明确的下一步练习保存为当前面试行动项，可选关联一道题。",
            run=add_action,
            args=[{"name": "title", "type": "str", "desc": "行动项标题", "required": True},
                  {"name": "detail", "type": "str", "desc": "练习说明（可省）", "required": False},
                  {"name": "question_id", "type": "str", "desc": "关联题目 id（可省）", "required": False}],
        ),
        Tool(
            name="list_actions",
            label="查看练习行动项",
            description="列出当前面试的行动项，可按 todo/doing/done/dismissed 过滤。",
            run=list_actions,
            args=[{"name": "status", "type": "str", "desc": "状态过滤（可省）", "required": False}],
        ),
        Tool(
            name="set_action_status",
            label="更新行动项状态",
            description="把一条练习行动项更新为 todo、doing、done 或 dismissed。",
            run=set_action_status,
            args=[{"name": "action_id", "type": "str", "desc": "行动项 id", "required": True},
                  {"name": "status", "type": "str", "desc": "todo/doing/done/dismissed", "required": True}],
        ),
        Tool(name="compare_interviews", label="跨面试洞察",
             description="对用户明确选择的面试档案做横向比较，找重复弱点、进步与练习方向；不传则只比较当前可见数据。",
             run=compare_interviews,
             args=[{"name":"interview_ids","type":"list","desc":"要比较的面试档案 id 列表","required":False}]),
        Tool(name="delete_recording", label="删除面试录音", risk="approve",
             description="删除一段录音及其转写记录，并在没有其它引用时清理文件区。必须用户明确要求删除。",
             run=delete_recording,
             args=[{"name":"recording_id","type":"str","desc":"录音 id","required":True}]),
        Tool(name="delete_interview", label="删除整场面试", risk="approve",
             description="删除整场面试的录音、题目、复盘和行动项；必须用户明确确认删除。",
             run=delete_interview,
             args=[{"name":"interview_id","type":"str","desc":"面试档案 id（可省，默认当前面试）","required":False}]),
    ]
