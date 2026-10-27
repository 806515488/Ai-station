"""工作台用例：读取技能的展示声明，准备普通聊天消息。

这里不创建会话、不提交任务、不调用模型。技能自己的 manifest 维护引导内容，
首页只能展示已经加载且有对话工具的能力；真实执行继续由 conversation/chat 承担。
"""
from __future__ import annotations

import logging
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from station.skills.registry import get_registry
from station import workbench_cases

DRAFT_LIMIT = 12000
log = logging.getLogger(__name__)
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Definition(BaseModel):
    """技能可选的工作台声明；只接受展示文字和普通消息，不接受路径或可执行代码。"""
    model_config = ConfigDict(extra="forbid")

    order: int = Field(default=100, ge=0, le=10000)
    icon: Literal["archive", "interview", "weekly", "video", "task"] = "task"
    name: ShortText
    category: ShortText
    description: str = Field(min_length=1, max_length=500)
    output: str = Field(min_length=1, max_length=300)
    steps: list[ShortText] = Field(min_length=1, max_length=8)
    input_kind: Literal["conversation", "text"]
    need: str = Field(default="", max_length=1000)
    label: str = Field(default="", max_length=100)
    placeholder: str = Field(default="", max_length=1000)
    action: ShortText
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    sample: str = Field(default="", max_length=DRAFT_LIMIT)
    sample_label: ShortText = "示例材料"
    sample_notice: str = Field(default="", max_length=300)
    pending: str = Field(default="", max_length=500)
    case_id: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9_-]{0,63})?$")

    @model_validator(mode="after")
    def check_input(self):
        if self.input_kind == "text" and not self.label.strip():
            raise ValueError("文本入口必须声明输入标签")
        if self.input_kind == "conversation" and not self.need.strip():
            raise ValueError("上传入口必须说明需要什么材料")
        return self


def _definition(skill):
    """损坏或未装载的单个技能不拖垮整个首页，也不暴露其内部代码入口。"""
    if (not skill or not isinstance(skill.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", skill.id)
            or not getattr(skill, "tools", None) or not getattr(skill, "workbench", None)):
        return None
    try:
        return Definition.model_validate(skill.workbench)
    except ValidationError:
        log.warning("技能工作台声明无效：%s", skill.id)
        return None


def catalog():
    """只发布显式声明的目录，不从用户历史会话、模型密钥或产物中拼演示案例。"""
    entries = []
    for skill in get_registry().list():
        definition = _definition(skill)
        if definition:
            entries.append((definition.order, skill.id, definition))
    tasks = []
    for _, skill_id, definition in sorted(entries, key=lambda row: (row[0], row[1])):
        # 发消息的前缀只在服务端使用，前端负责编辑正文；没有两套消息拼装规则。
        public = definition.model_dump(exclude={"order", "message", "case_id"})
        available = False
        inputs = 0
        material_label = ""
        if definition.case_id:
            try:
                case, _ = workbench_cases.load(skill_id, definition.case_id)
                inputs = len(workbench_cases.image_inputs(case))
                material_label = case.material_label
                available = True
            except LookupError:
                pass
        tasks.append({"id": skill_id, **public,
                      "case": {"available": available, "label": "查看完整案例" if available else "案例准备中",
                               "materials_url": f"/api/workbench/{skill_id}/case/materials" if available and inputs else "",
                               "input_count": inputs,
                               "reason": f"{material_label} · 实际执行记录" if available else "完整案例需准备示例资料并实际生成结果后再开放"}})
    return {"tasks": tasks, "draft_limit": DRAFT_LIMIT}


def prepare(skill_id: str, draft: str = ""):
    """准备消息是无副作用操作；浏览器仍须将返回的 message 发到原 /api/chat。"""
    definition = _definition(get_registry().get(skill_id))
    if definition is None:
        raise LookupError("该任务入口不存在或尚未加载，请刷新工作台")
    if not isinstance(draft, str) or len(draft) > DRAFT_LIMIT:
        raise ValueError("输入内容过长，请精简后再开始")
    content = draft.strip()
    if definition.input_kind == "text":
        if not content:
            raise ValueError("请先填写内容，也可以使用示例")
        message = definition.message.strip() + "\n" + content
    else:
        if content:
            raise ValueError("该入口请按对话引导上传材料，不接受文本材料")
        message = definition.message.strip()
    return {"message": message}


def case_definition(skill_id):
    definition = _definition(get_registry().get(skill_id))
    if definition is None:
        raise LookupError("该任务入口不存在或尚未加载")
    return definition.case_id
