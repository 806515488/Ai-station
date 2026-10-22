"""interview 数据仓库：只处理数据记录与 SQL，连接/锁/提交由统一 Database 管理。"""
from __future__ import annotations

import json
import uuid
from station.persistence import database


def interview_create(user_id: str, title: str, *, company: str = "", role: str = "",
                     round_name: str = "", occurred_at: float | None = None) -> dict:
    """新建一份面试档案，返回可直接给工具层使用的行字典。"""
    now, iid = database.now(), uuid.uuid4().hex
    clean_title = (title or "").strip() or "未命名面试"
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO interviews(id,user_id,title,company,role,round,occurred_at,"
            "status,speaker_map,summary,meta,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, user_id or "", clean_title, (company or "").strip(), (role or "").strip(),
             (round_name or "").strip(), occurred_at, "draft", "{}", "", "{}", now, now))
    return interview_get(iid, user_id) or {}


def interview_get(interview_id: str, user_id: str) -> dict | None:
    """按 id 读取自己的面试档案；不属于该用户与不存在统一返回 None。"""
    if not interview_id or not user_id:
        return None
    with database.session() as connection:
        row = connection.execute(
            "SELECT * FROM interviews WHERE id=? AND user_id=?", (interview_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_list(user_id: str, limit: int = 30) -> list[dict]:
    """列自己的面试档案，附带音频总数与已转写数，供选择/概览卡使用。"""
    if not user_id:
        return []
    with database.session() as connection:
        rows = connection.execute(
            "SELECT i.*, "
            "(SELECT COUNT(*) FROM interview_recordings r WHERE r.interview_id=i.id) AS recording_count, "
            "(SELECT COUNT(*) FROM interview_recordings r WHERE r.interview_id=i.id AND r.status='done') AS done_count "
            "FROM interviews i WHERE i.user_id=? ORDER BY i.updated DESC LIMIT ?",
            (user_id, max(1, min(int(limit or 30), 100)))).fetchall()
        return [dict(row) for row in rows]


def interview_recording_add(interview_id: str, user_id: str, file_id: str,
                            display_name: str = "") -> dict:
    """把一段已上传音频追加到某次面试，sequence 由数据库内现有最大值推导。"""
    interview = interview_get(interview_id, user_id)
    if interview is None:
        raise ValueError("面试档案不存在或不属于当前账号")
    now, rid = database.now(), uuid.uuid4().hex
    with database.session(write=True) as connection:
        old = connection.execute(
            "SELECT * FROM interview_recordings WHERE interview_id=? AND file_id=?",
            (interview_id, file_id)).fetchone()
        if old is not None:              # 上传卡重试不能造出重复录音
            return dict(old)
        seq = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next_seq FROM interview_recordings WHERE interview_id=?",
            (interview_id,)).fetchone()["next_seq"]
        connection.execute(
            "INSERT INTO interview_recordings(id,interview_id,file_id,display_name,sequence,"
            "asr_job_id,transcript_file_id,asr_model,status,error_message,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, interview_id, file_id, display_name or "", int(seq), "", "", "", "uploaded", "", now, now))
        connection.execute("UPDATE interviews SET updated=? WHERE id=?", (now, interview_id))
        return dict(connection.execute("SELECT * FROM interview_recordings WHERE id=?", (rid,)).fetchone())


def interview_recording_get(recording_id: str, user_id: str) -> dict | None:
    """读取一段录音，同时经所属面试的 user_id 做鉴权。"""
    if not recording_id or not user_id:
        return None
    with database.session() as connection:
        row = connection.execute(
            "SELECT r.* FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE r.id=? AND i.user_id=?", (recording_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_recording_by_file(interview_id: str, user_id: str, file_id: str) -> dict | None:
    """从上传回传的 file_id 找到当前面试下对应录音。"""
    if not interview_id or not user_id or not file_id:
        return None
    with database.session() as connection:
        row = connection.execute(
            "SELECT r.* FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE r.interview_id=? AND r.file_id=? AND i.user_id=?",
            (interview_id, file_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_recording_by_job(job_id: str, user_id: str) -> dict | None:
    """按 ASR job 找录音；任务状态页/完成回写都用这条，不依赖会话最近任务。"""
    if not job_id or not user_id:
        return None
    with database.session() as connection:
        row = connection.execute(
            "SELECT r.* FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE r.asr_job_id=? AND i.user_id=?", (job_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_recordings(interview_id: str, user_id: str) -> list[dict]:
    """按用户确认过的 sequence 返回当前面试的全部录音。"""
    if interview_get(interview_id, user_id) is None:
        return []
    with database.session() as connection:
        rows = connection.execute(
            "SELECT * FROM interview_recordings WHERE interview_id=? ORDER BY sequence ASC",
            (interview_id,)).fetchall()
        return [dict(row) for row in rows]


def _refresh_interview_status(interview_id: str) -> None:
    """从录音状态推导档案状态；写作用域复用调用方事务，避免中途提交。"""
    with database.session(write=True) as connection:
        rows = connection.execute(
            "SELECT status FROM interview_recordings WHERE interview_id=?", (interview_id,)).fetchall()
        states = {str(row["status"] or "") for row in rows}
        status = "transcribing" if "transcribing" in states else ("ready" if "done" in states else "draft")
        connection.execute("UPDATE interviews SET status=?,updated=? WHERE id=?", (status, database.now(), interview_id))


def interview_recording_start(recording_id: str, user_id: str, job_id: str) -> dict | None:
    """把录音绑定到刚提交的 ASR job；同一录音不会被悄悄覆盖到另一条 job。"""
    rec = interview_recording_get(recording_id, user_id)
    if rec is None:
        return None
    with database.session(write=True) as connection:
        connection.execute(
            "UPDATE interview_recordings SET asr_job_id=?,status='transcribing',error_message='',updated=? WHERE id=?",
            (job_id, database.now(), recording_id))
        _refresh_interview_status(rec["interview_id"])
    return interview_recording_get(recording_id, user_id)


def interview_recording_finish(recording_id: str, user_id: str, *, transcript_file_id: str = "",
                               asr_model: str = "", error_message: str = "",
                               expected_job_id: str = "") -> dict | None:
    """转写终态回写。失败只保存简短错误，完整异常仍在 job.log，避免表里塞长堆栈。"""
    rec = interview_recording_get(recording_id, user_id)
    if rec is None:
        return None
    ok = bool(transcript_file_id)
    with database.session(write=True) as connection:
        sql = (
            "UPDATE interview_recordings SET transcript_file_id=?,asr_model=?,status=?,error_message=?,updated=? "
            "WHERE id=?")
        args = [transcript_file_id or "", asr_model or rec.get("asr_model") or "",
                "done" if ok else "failed", (error_message or "")[:500], database.now(), recording_id]
        if expected_job_id:
            sql += " AND asr_job_id=?"
            args.append(expected_job_id)
        cur = connection.execute(sql, args)
        if int(cur.rowcount or 0) == 0:
            return interview_recording_get(recording_id, user_id)  # 已被显式重试的新 job 接管
        _refresh_interview_status(rec["interview_id"])
    return interview_recording_get(recording_id, user_id)


def interview_set_speaker_map(interview_id: str, user_id: str, speaker_map: dict) -> dict | None:
    """保存用户确认的 speaker_0/1 身份映射；只收小 JSON，避免把转写全文塞入 PG。"""
    if interview_get(interview_id, user_id) is None:
        return None
    clean = {str(k)[:80]: str(v)[:40] for k, v in (speaker_map or {}).items()
             if str(k).strip() and str(v).strip()}
    with database.session(write=True) as connection:
        connection.execute("UPDATE interviews SET speaker_map=?,updated=? WHERE id=?",
                       (json.dumps(clean, ensure_ascii=False), database.now(), interview_id))
    return interview_get(interview_id, user_id)


def interview_question_spans(question_id: str, user_id: str) -> list[dict]:
    """列某题的可追溯音频证据范围。"""
    question = interview_question_get(question_id, user_id)
    if question is None:
        return []
    with database.session() as connection:
        rows = connection.execute(
            "SELECT * FROM interview_question_spans WHERE question_id=? ORDER BY recording_id,start_ms",
            (question_id,)).fetchall()
        return [dict(row) for row in rows]


def interview_question_list(interview_id: str, user_id: str) -> list[dict]:
    """列当前用户某场面试的题目草稿/已确认题；不把其它面试混进来。"""
    if interview_get(interview_id, user_id) is None:
        return []
    with database.session() as connection:
        rows = connection.execute(
            "SELECT * FROM interview_questions WHERE interview_id=? ORDER BY sequence ASC",
            (interview_id,)).fetchall()
        return [dict(row) for row in rows]


def interview_question_get(question_id: str, user_id: str) -> dict | None:
    """按题目 id 读取，并经 interview.user_id 鉴权。"""
    if not question_id or not user_id:
        return None
    with database.session() as connection:
        row = connection.execute(
            "SELECT q.* FROM interview_questions q JOIN interviews i ON i.id=q.interview_id "
            "WHERE q.id=? AND i.user_id=?", (question_id, user_id)).fetchone()
        return dict(row) if row else None


def interview_question_add_many(interview_id: str, user_id: str, items: list[dict]) -> list[dict]:
    """追加模型提取出的 Q&A 草稿；同题同答已存在时跳过，绝不覆盖人工确认内容。"""
    if interview_get(interview_id, user_id) is None:
        raise ValueError("面试档案不存在或不属于当前账号")
    clean = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question_text") or item.get("question") or "").strip()
        answer = str(item.get("answer_text") or item.get("answer") or "").strip()
        if not question or not answer:
            continue
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        clean.append({
            "question_text": question[:8000], "answer_text": answer[:16000],
            "followup_text": str(item.get("followup_text") or item.get("followup") or "").strip()[:8000],
            "category": str(item.get("category") or "").strip()[:100],
            "tags": json.dumps([str(x)[:80] for x in tags[:20]], ensure_ascii=False),
            "spans": item.get("spans") if isinstance(item.get("spans"), list) else [],
        })
    if not clean:
        return []
    now, added = database.now(), []
    with database.session(write=True) as connection:
        rows = connection.execute(
            "SELECT question_text,answer_text FROM interview_questions WHERE interview_id=?",
            (interview_id,)).fetchall()
        exists = {(str(row["question_text"]).strip(), str(row["answer_text"]).strip()) for row in rows}
        seq = connection.execute(
            "SELECT COALESCE(MAX(sequence),0) AS max_seq FROM interview_questions WHERE interview_id=?",
            (interview_id,)).fetchone()["max_seq"]
        for item in clean:
            sig = (item["question_text"], item["answer_text"])
            if sig in exists:
                continue
            seq, qid = int(seq) + 1, uuid.uuid4().hex
            connection.execute(
                "INSERT INTO interview_questions(id,interview_id,sequence,category,question_text,"
                "answer_text,followup_text,status,tags,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (qid, interview_id, seq, item["category"], item["question_text"], item["answer_text"],
                 item["followup_text"], "draft", item["tags"], now, now))
            added.append(dict(connection.execute("SELECT * FROM interview_questions WHERE id=?", (qid,)).fetchone()))
            # spans 是可选证据，不是模型必须猜出的字段。若给了，必须确属当前面试的录音。
            for span in item.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                rid = str(span.get("recording_id") or "")
                rec = interview_recording_get(rid, user_id)
                if rec is None or rec["interview_id"] != interview_id:
                    continue
                try:
                    start, end = int(span.get("start_ms")), int(span.get("end_ms"))
                except (TypeError, ValueError):
                    continue
                if start < 0 or end < start:
                    continue
                connection.execute(
                    "INSERT INTO interview_question_spans(question_id,recording_id,start_ms,end_ms,speaker_role,created) "
                    "VALUES(?,?,?,?,?,?)",
                    (qid, rid, start, end, str(span.get("speaker_role") or "")[:40], now))
            exists.add(sig)
        if added:
            connection.execute("UPDATE interviews SET updated=? WHERE id=?", (now, interview_id))
    return added


def interview_question_set_status(question_id: str, user_id: str, status: str) -> dict | None:
    """题目状态仅允许 draft/confirmed/reviewed/skipped，防模型写入任意状态字。"""
    if status not in {"draft", "confirmed", "reviewed", "skipped"}:
        raise ValueError("题目状态不合法")
    question = interview_question_get(question_id, user_id)
    if question is None:
        return None
    with database.session(write=True) as connection:
        connection.execute("UPDATE interview_questions SET status=?,updated=? WHERE id=?",
                       (status, database.now(), question_id))
    return interview_question_get(question_id, user_id)


def interview_question_update(question_id: str, user_id: str, *, question_text: str, answer_text: str,
                              category: str = "", followup_text: str = "") -> dict | None:
    q = interview_question_get(question_id, user_id)
    if q is None:
        return None
    if not (question_text or "").strip() or not (answer_text or "").strip():
        raise ValueError("问题和原回答都不能为空")
    with database.session(write=True) as connection:
        connection.execute("UPDATE interview_questions SET question_text=?,answer_text=?,category=?,followup_text=?,updated=? WHERE id=?",
                       ((question_text or "").strip()[:8000], (answer_text or "").strip()[:16000],
                        (category or "").strip()[:100], (followup_text or "").strip()[:8000], database.now(), question_id))
    return interview_question_get(question_id, user_id)


def interview_question_merge(keep_id: str, merge_id: str, user_id: str) -> dict | None:
    keep, other = interview_question_get(keep_id, user_id), interview_question_get(merge_id, user_id)
    if not keep or not other or keep["interview_id"] != other["interview_id"] or keep_id == merge_id:
        raise ValueError("只能合并同一面试中的两道不同题目")
    if interview_reviews(other["id"], user_id):
        raise ValueError("被合并题已有复盘版本，请先人工编辑，避免丢失练习历史")
    with database.session(write=True) as connection:
        answer = keep["answer_text"] + "\n\n" + other["answer_text"]
        follow = "\n".join(x for x in (keep.get("followup_text"), other.get("followup_text")) if x)
        connection.execute("UPDATE interview_questions SET answer_text=?,followup_text=?,updated=? WHERE id=?",
                       (answer[:16000], follow[:8000], database.now(), keep_id))
        connection.execute("UPDATE interview_question_spans SET question_id=? WHERE question_id=?", (keep_id, merge_id))
        connection.execute("UPDATE interview_actions SET question_id=? WHERE question_id=?", (keep_id, merge_id))
        connection.execute("DELETE FROM interview_questions WHERE id=?", (merge_id,))
    return interview_question_get(keep_id, user_id)


def interview_question_split(question_id: str, user_id: str, *, first_question: str, first_answer: str,
                             second_question: str, second_answer: str) -> tuple[dict, dict] | None:
    q = interview_question_get(question_id, user_id)
    if q is None:
        return None
    first = interview_question_update(q["id"], user_id, question_text=first_question, answer_text=first_answer,
                                      category=q.get("category") or "", followup_text=q.get("followup_text") or "")
    added = interview_question_add_many(q["interview_id"], user_id, [{
        "question_text": second_question, "answer_text": second_answer,
        "category": q.get("category") or "", "tags": json.loads(q.get("tags") or "[]"),
    }])
    return (first, added[0]) if added else None


def interview_review_add(question_id: str, user_id: str, *, self_reflection: str = "",
                         coach_feedback: str = "", improved_answer: str = "",
                         drill_prompt: str = "", source: str = "ai") -> dict | None:
    """为一题追加不可覆盖的复盘版本，方便比较“第一次改法”和之后的练习进步。"""
    question = interview_question_get(question_id, user_id)
    if question is None:
        return None
    now, rid = database.now(), uuid.uuid4().hex
    with database.session(write=True) as connection:
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM interview_reviews WHERE question_id=?",
            (question_id,)).fetchone()["next_version"]
        connection.execute(
            "INSERT INTO interview_reviews(id,question_id,version,self_reflection,coach_feedback,"
            "improved_answer,drill_prompt,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rid, question_id, int(version), (self_reflection or "")[:8000],
             (coach_feedback or "")[:16000], (improved_answer or "")[:16000],
             (drill_prompt or "")[:8000], (source or "ai")[:30], now, now))
        connection.execute("UPDATE interview_questions SET status='reviewed',updated=? WHERE id=?", (now, question_id))
        return dict(connection.execute("SELECT * FROM interview_reviews WHERE id=?", (rid,)).fetchone())


def interview_reviews(question_id: str, user_id: str) -> list[dict]:
    """读取一题的复盘版本历史；题目归属校验失败时返回空，避免越权查看。"""
    if interview_question_get(question_id, user_id) is None:
        return []
    with database.session() as connection:
        rows = connection.execute(
            "SELECT * FROM interview_reviews WHERE question_id=? ORDER BY version DESC", (question_id,)).fetchall()
        return [dict(row) for row in rows]


def interview_action_add(interview_id: str, user_id: str, title: str, *, detail: str = "",
                         question_id: str = "", due_at: float | None = None) -> dict | None:
    """新增下一次面试前的行动项；若绑定题目，必须同属这次面试。"""
    if interview_get(interview_id, user_id) is None:
        return None
    if question_id:
        question = interview_question_get(question_id, user_id)
        if question is None or question["interview_id"] != interview_id:
            raise ValueError("行动项绑定的题目不属于当前面试")
    now, aid = database.now(), uuid.uuid4().hex
    with database.session(write=True) as connection:
        connection.execute(
            "INSERT INTO interview_actions(id,interview_id,question_id,title,detail,status,due_at,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, interview_id, question_id or None, (title or "").strip()[:300],
             (detail or "")[:8000], "todo", due_at, now, now))
        return dict(connection.execute("SELECT * FROM interview_actions WHERE id=?", (aid,)).fetchone())


def interview_actions(interview_id: str, user_id: str, status: str = "") -> list[dict]:
    """列某场面试的行动项；可按 todo/doing/done/dismissed 过滤。"""
    if interview_get(interview_id, user_id) is None:
        return []
    sql, args = "SELECT * FROM interview_actions WHERE interview_id=?", [interview_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY CASE status WHEN 'todo' THEN 0 WHEN 'doing' THEN 1 ELSE 2 END, updated DESC"
    with database.session() as connection:
        return [dict(row) for row in connection.execute(sql, args).fetchall()]


def interview_action_set_status(action_id: str, user_id: str, status: str) -> dict | None:
    """完成/搁置行动项；状态白名单防止模型写入任意值。"""
    if status not in {"todo", "doing", "done", "dismissed"}:
        raise ValueError("行动项状态不合法")
    with database.session(write=True) as connection:
        row = connection.execute(
            "SELECT a.* FROM interview_actions a JOIN interviews i ON i.id=a.interview_id "
            "WHERE a.id=? AND i.user_id=?", (action_id, user_id)).fetchone()
        if row is None:
            return None
        connection.execute("UPDATE interview_actions SET status=?,updated=? WHERE id=?",
                       (status, database.now(), action_id))
        return dict(connection.execute("SELECT * FROM interview_actions WHERE id=?", (action_id,)).fetchone())


def interview_file_references(file_id: str, user_id: str) -> int:
    """统计某面试文件仍被多少录音行引用，物理删除前的最后一道保护。"""
    with database.session() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM interview_recordings r JOIN interviews i ON i.id=r.interview_id "
            "WHERE i.user_id=? AND (r.file_id=? OR r.transcript_file_id=?)",
            (user_id, file_id, file_id)).fetchone()
        return int(row["n"] if row else 0)


def interview_recording_delete(recording_id: str, user_id: str) -> list[str]:
    """删除一段录音的业务记录，返回候选文件 id；物理文件由技能层在引用归零后删。"""
    rec = interview_recording_get(recording_id, user_id)
    if rec is None:
        return []
    files = [x for x in (rec.get("file_id"), rec.get("transcript_file_id")) if x]
    with database.session(write=True) as connection:
        connection.execute("DELETE FROM interview_question_spans WHERE recording_id=?", (recording_id,))
        connection.execute("DELETE FROM interview_recordings WHERE id=?", (recording_id,))
        _refresh_interview_status(rec["interview_id"])
    return files


def interview_delete(interview_id: str, user_id: str) -> list[str]:
    """删除整场面试及其关系记录，返回可安全检查后清理的文件 id。"""
    if interview_get(interview_id, user_id) is None:
        return []
    recs = interview_recordings(interview_id, user_id)
    files = [x for r in recs for x in (r.get("file_id"), r.get("transcript_file_id")) if x]
    with database.session(write=True) as connection:
        qrows = connection.execute("SELECT id FROM interview_questions WHERE interview_id=?", (interview_id,)).fetchall()
        qids = [r["id"] for r in qrows]
        for qid in qids:
            connection.execute("DELETE FROM interview_reviews WHERE question_id=?", (qid,))
            connection.execute("DELETE FROM interview_question_spans WHERE question_id=?", (qid,))
        connection.execute("DELETE FROM interview_actions WHERE interview_id=?", (interview_id,))
        connection.execute("DELETE FROM interview_questions WHERE interview_id=?", (interview_id,))
        connection.execute("DELETE FROM interview_recordings WHERE interview_id=?", (interview_id,))
        connection.execute("DELETE FROM interviews WHERE id=? AND user_id=?", (interview_id, user_id))
    return files


def interview_questions_across(user_id: str, interview_ids: list[str] | None = None) -> list[dict]:
    """跨面试洞察的数据源；只读用户显式选择的档案，默认最多最近 20 场。"""
    ids = [x for x in (interview_ids or []) if x]
    sql = ("SELECT q.*,i.title AS interview_title,i.company,i.role,i.round,i.occurred_at "
           "FROM interview_questions q JOIN interviews i ON i.id=q.interview_id WHERE i.user_id=?")
    args = [user_id]
    if ids:
        sql += " AND i.id IN (%s)" % ",".join("?" * len(ids)); args.extend(ids)
    sql += " ORDER BY i.occurred_at DESC,q.sequence ASC"
    with database.session() as connection:
        return [dict(r) for r in connection.execute(sql, args).fetchall()]
