"""视频任务已有恢复规则：使用原参数及检查点，只查询原远端任务。"""


def validate(job, owner):
    """远端 ID 和终态的可操作判断沿用任务展示层现有白名单。"""


def prepare(job, owner, next_id, metadata):
    return dict(job.args)


def can_resume(checkpoint):
    return bool(checkpoint.get("remote_id"))
