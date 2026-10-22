"""转写任务已有恢复规则：关联接续任务，使用原远端转写检查点。"""
import interview_repository


def validate(job, owner):
    """远端 ID 和终态的可操作判断沿用任务展示层现有白名单。"""


def prepare(job, owner, next_id, metadata):
    args = dict(job.args)
    interview_repository.interview_recording_start(args["recording_id"], owner, next_id)
    return args


def can_resume(checkpoint):
    return bool(checkpoint.get("remote_id"))
