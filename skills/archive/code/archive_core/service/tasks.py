"""档案任务已有恢复规则：原项目必须在本机，接续识别复用缓存。"""
from pathlib import Path


def validate(job, owner):
    if not Path(job.args.get("project", "")).is_file():
        raise ValueError("当前实例没有档案项目，请返回保存原件的实例操作")


def prepare(job, owner, next_id, metadata):
    return {**job.args, "refresh": False}
