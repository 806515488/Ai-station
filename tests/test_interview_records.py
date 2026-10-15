"""面试档案第一期：多场面试、多段音频必须按 owner/interview_id 严格隔离。"""
from __future__ import annotations

from station import db
from station.core.context import Context
from station.core.session import Thread
from station.skills.registry import get_registry


def test_interview_recordings_are_ordered_and_owner_scoped():
    owner = db.create_user("interview-owner", "pw")
    other = db.create_user("interview-other", "pw")
    iv = db.interview_create(owner["id"], "A公司·后端·一面", company="A公司", role="后端", round_name="一面")
    first = db.interview_recording_add(iv["id"], owner["id"], "audio-1", "上半场.m4a")
    second = db.interview_recording_add(iv["id"], owner["id"], "audio-2", "下半场.m4a")

    assert [r["id"] for r in db.interview_recordings(iv["id"], owner["id"])] == [first["id"], second["id"]]
    assert [r["sequence"] for r in db.interview_recordings(iv["id"], owner["id"])] == [1, 2]
    assert db.interview_get(iv["id"], other["id"]) is None
    assert db.interview_recordings(iv["id"], other["id"]) == []


def test_interview_recording_job_lifecycle_updates_only_its_own_interview():
    owner = db.create_user("interview-job-owner", "pw")
    first_iv = db.interview_create(owner["id"], "A公司一面")
    second_iv = db.interview_create(owner["id"], "B公司一面")
    first = db.interview_recording_add(first_iv["id"], owner["id"], "audio-a")
    second = db.interview_recording_add(second_iv["id"], owner["id"], "audio-b")

    db.interview_recording_start(first["id"], owner["id"], "asr-job-a")
    assert db.interview_get(first_iv["id"], owner["id"])["status"] == "transcribing"
    assert db.interview_get(second_iv["id"], owner["id"])["status"] == "draft"

    done = db.interview_recording_finish(first["id"], owner["id"], transcript_file_id="transcript-a",
                                          asr_model="qwen-audio-filetrans")
    assert done["status"] == "done" and done["transcript_file_id"] == "transcript-a"
    assert db.interview_get(first_iv["id"], owner["id"])["status"] == "ready"
    assert db.interview_recording_by_job("asr-job-a", owner["id"])["id"] == first["id"]
    assert db.interview_recording_get(second["id"], owner["id"])["status"] == "uploaded"


def test_interview_tools_bind_upload_card_to_selected_interview(tmp_path):
    owner = db.create_user("interview-tool-owner", "pw")
    skill = get_registry().get("interview-coach")
    thread = Thread(skill_id="interview-coach")
    thread.user_id = owner["id"]
    ctx = Context(skill_id="interview-coach", data_dir=tmp_path, user_id=owner["id"], thread=thread)
    tools = {tool.leaf(): tool for tool in skill.tools}

    text = tools["create_interview"].run(ctx, title="A公司·技术一面")
    assert "已新建并选中" in text
    iid = thread.meta["interview_id"]
    card = tools["ask_audio"].run(ctx)
    assert card["render"]["purpose"] == "audio"
    assert card["render"]["interview_id"] == iid
    shown = tools["show_interview"].run(ctx)   # 返回 {text, render} 卡片，不是裸字符串
    assert "A公司·技术一面" in shown["text"]
    assert shown["render"]["type"] == "interview-card"
    assert shown["render"]["title"] == "A公司·技术一面"


def test_interview_questions_reviews_and_actions_keep_history_and_scope():
    owner = db.create_user("interview-review-owner", "pw")
    first_iv = db.interview_create(owner["id"], "A公司一面")
    second_iv = db.interview_create(owner["id"], "B公司一面")
    added = db.interview_question_add_many(first_iv["id"], owner["id"], [
        {"question_text": "请介绍一个最困难的项目问题", "answer_text": "我先说明背景，再讲排障过程。",
         "category": "项目经历", "tags": ["项目", "排障"]},
    ])
    # 同题同答重复提取时不再插入；人工确认/复盘不会被草稿工具覆盖。
    assert len(added) == 1
    assert db.interview_question_add_many(first_iv["id"], owner["id"], [
        {"question_text": "请介绍一个最困难的项目问题", "answer_text": "我先说明背景，再讲排障过程。"},
    ]) == []
    q = added[0]
    assert db.interview_question_set_status(q["id"], owner["id"], "confirmed")["status"] == "confirmed"

    first = db.interview_review_add(q["id"], owner["id"], coach_feedback="缺少量化结果", improved_answer="补充影响范围和结果")
    second = db.interview_review_add(q["id"], owner["id"], coach_feedback="结构更完整", improved_answer="加入取舍说明")
    assert (first["version"], second["version"]) == (1, 2)
    assert [r["version"] for r in db.interview_reviews(q["id"], owner["id"])] == [2, 1]

    action = db.interview_action_add(first_iv["id"], owner["id"], "用 STAR 重答这道项目题", question_id=q["id"])
    assert db.interview_action_set_status(action["id"], owner["id"], "done")["status"] == "done"
    assert len(db.interview_actions(first_iv["id"], owner["id"])) == 1
    assert db.interview_actions(second_iv["id"], owner["id"]) == []


def test_tool_text_must_expose_full_ids_so_the_model_can_act(tmp_path):
    """★ 回归护栏：工具返回的 `text` 里必须是**完整 id**，不许截断成 8 位。

    宿主 `core/agent.run_tool` 只把 `text` 交给模型（`render` 只发前端），所以列表里给
    截断 id，模型就永远没法把它交给 `select_interview` / `show_question` /
    `set_action_status` 这些"按 id 操作"的工具 —— 线上实测的后果是：模型一遍遍重列，
    每次调用都撞「找不到」，整轮空转（它自己都打出"列表里给我的是截断编号"）。
    """
    owner = db.create_user("interview-id-owner", "pw")
    iv = db.interview_create(owner["id"], "A公司一面")
    rec = db.interview_recording_add(iv["id"], owner["id"], "audio-x", "上半场.m4a")
    q = db.interview_question_add_many(iv["id"], owner["id"], [
        {"question_text": "介绍一个最困难的项目问题", "answer_text": "先背景再排障"}])[0]

    skill = get_registry().get("interview-coach")
    thread = Thread(skill_id="interview-coach")
    thread.user_id = owner["id"]
    ctx = Context(skill_id="interview-coach", data_dir=tmp_path, user_id=owner["id"], thread=thread)
    tools = {tool.leaf(): tool for tool in skill.tools}

    # 1) 列档案给完整 id，且这个 id 真能用来切换
    listed = tools["list_interviews"].run(ctx)
    assert iv["id"] in listed
    tools["select_interview"].run(ctx, interview_id=iv["id"])
    assert thread.meta["interview_id"] == iv["id"]

    # 2) 概况里的录音要给完整 recording_id（删除/重试要它）
    assert rec["id"] in tools["show_interview"].run(ctx)["text"]

    # 3) 列题目 / 存题目给完整题目 id（逐题复盘要它）
    assert q["id"] in tools["list_questions"].run(ctx)["text"]
    saved = tools["save_questions"].run(ctx, items=[
        {"question_text": "再讲一次那个项目", "answer_text": "补充影响范围与结果"}])
    newest = [x for x in db.interview_question_list(iv["id"], owner["id"])
              if x["question_text"] == "再讲一次那个项目"][0]
    assert newest["id"] in saved

    # 4) 加行动项 / 列行动项给完整 action_id（改状态要它）
    added = tools["add_action"].run(ctx, title="用 STAR 重答这道项目题")
    action = db.interview_actions(iv["id"], owner["id"])[0]
    assert action["id"] in added
    assert action["id"] in tools["list_actions"].run(ctx)["text"]
