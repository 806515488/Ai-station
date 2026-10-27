"""显式发布的只读演示案例：只读技能资源，不读取任何用户的会话或文件柜。

只有案例 JSON 与每份产物的 SHA256 均通过检查，首页才开放入口。
下载参数是已登记的产物标识，不是文件路径；真实执行仍走原聊天与工具链。
"""
from __future__ import annotations

import hashlib
import json
import re
import io
from zipfile import ZipFile, ZIP_DEFLATED
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from station import config

IDENTIFIER = r"^[a-z0-9][a-z0-9_-]{0,63}$"
MEDIA = {".md": "text/markdown", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".png": "image/png", ".jpg": "image/jpeg", ".pdf": "application/pdf",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=IDENTIFIER)
    filename: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,80}$")
    label: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    role: Literal["draft", "result", "input"] = "result"


class CaseMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool"]
    content: str = Field(max_length=20000)
    label: str = Field(default="", max_length=100)
    images: list[str] = Field(default_factory=list, max_length=6)


class CaseTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    duration_ms: int = Field(ge=0)
    messages: list[CaseMessage] = Field(min_length=1, max_length=30)


class Case(BaseModel):
    # 不允许夹带 thread_id、user_id、原始 trace 或模型连接配置。
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(max_length=1000)
    input: str = Field(min_length=1, max_length=12000)
    recorded_at: str = Field(max_length=50)
    material_label: str = Field(default="虚构材料", max_length=100)
    user_label: str = Field(default="示例用户", max_length=80)
    replay_label: str = Field(default="用这份材料重新体验 →", max_length=100)
    turns: list[CaseTurn] = Field(min_length=1, max_length=32)
    files: list[CaseFile] = Field(min_length=1, max_length=24)
    report_id: str = Field(pattern=IDENTIFIER)


def image_inputs(case):
    return [f for f in case.files if f.role == "input" and MEDIA[Path(f.filename).suffix].startswith("image/")]


def load(skill_id: str, case_id: str):
    """限制在技能资源内；缺产物、篡改、非法结构均作为未准备好处理。"""
    if not case_id:
        raise LookupError("案例还在准备中")
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", skill_id) or not re.fullmatch(IDENTIFIER, case_id):
            raise ValueError("无效的案例标识")
        base = config.SKILLS_DIR.resolve()
        root = (base / skill_id / "assets" / "cases" / case_id).resolve()
        if not root.is_relative_to(base):
            raise ValueError("案例路径越界")
        manifest = (root / "case.json").resolve()
        if not manifest.is_relative_to(root):
            raise ValueError("案例记录越界")
        if manifest.stat().st_size > 256000:
            raise ValueError("案例记录过大")
        case = Case.model_validate(json.loads(manifest.read_text(encoding="utf-8")))
        if len({f.id for f in case.files}) != len(case.files):
            raise ValueError("产物标识重复")
        reports = [f for f in case.files if f.id == case.report_id and Path(f.filename).suffix == ".md"]
        if len(reports) != 1:
            raise ValueError("案例缺少正文")
        total_bytes = 0
        for asset in case.files:
            path = (root / asset.filename).resolve()
            if (not path.is_relative_to(root) or path.suffix not in MEDIA
                    or path.stat().st_size > 5_000_000
                    or hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256):
                raise ValueError("案例产物未通过校验")
            total_bytes += path.stat().st_size
        if total_bytes > 25_000_000:
            raise ValueError("案例资源总量过大")
        known_images = {f.id for f in image_inputs(case)}
        if any(set(message.images) - known_images for turn in case.turns for message in turn.messages):
            raise ValueError("过程引用了未登记的图片")
        (root / reports[0].filename).read_text(encoding="utf-8")
        return case, root
    except (OSError, ValueError) as error:
        raise LookupError("案例暂未准备好，请稍后再试") from error


def payload(skill_id: str, case_id: str):
    case, root = load(skill_id, case_id)
    data = case.model_dump(exclude={"files", "report_id"})
    report = next(asset for asset in case.files if asset.id == case.report_id)
    data["report"] = (root / report.filename).read_text(encoding="utf-8")
    data["files"] = [{"id": f.id, "label": f.label, "role": f.role, "filename": f.filename,
                      "url": f"/api/workbench/{skill_id}/case/files/{f.id}",
                      "preview_url": f"/api/workbench/{skill_id}/case/files/{f.id}?preview=1"
                      if MEDIA[Path(f.filename).suffix].startswith("image/") else ""} for f in case.files]
    inputs = image_inputs(case)
    data["materials_url"] = f"/api/workbench/{skill_id}/case/materials" if inputs else ""
    return data


def artifact(skill_id: str, case_id: str, asset_id: str):
    case, root = load(skill_id, case_id)
    asset = next((f for f in case.files if f.id == asset_id), None)
    if asset is None:
        raise LookupError("案例产物不存在")
    path = root / asset.filename
    return path, MEDIA[path.suffix], asset.filename


def materials(skill_id: str, case_id: str):
    """仅打包已审阅并登记的输入图片；不扫描用户上传目录或项目目录。"""
    case, root = load(skill_id, case_id)
    inputs = image_inputs(case)
    if not inputs:
        raise LookupError("该案例没有可下载的示例图片")
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for item in inputs:
            archive.write(root / item.filename, item.filename)
    return buffer.getvalue()
