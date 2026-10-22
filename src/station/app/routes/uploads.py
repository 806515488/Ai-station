"""媒体上传：档案照片、人像参考图和面试音频；保留各自的存储与归属规则。

档案照片沿用原上传目录；人像在独立文件区归一化并去掉 EXIF，避免被档案清理误删。
面试音频绑定本人面试档案，流式校验类型与大小；只登记原件，不在接口里转写。
"""
from __future__ import annotations

from station.skills.storage import repository as skill_repository


import time
import uuid
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from station import config
from station.app.auth import require_user
from station.app.uploads import purge_old, store_upload

router = APIRouter(tags=["媒体上传"])


@router.post("/api/photos")
async def upload_photos(files: list[UploadFile] = File(...),
                        label: str = Form("")):
    """接收多张照片 → 落到 data/station/uploads/<人名>-<uid>/，返回该目录。

    body: multipart/form-data；files（可多个）为照片，label（可选）是"这一卷是谁的"
    （干部姓名或所选文件夹名）。返回 {"dir","count","label"}；dir 作 job 的 photos_dir。
    每次新上传先把旧上传目录清掉（单用户顺序流程，旧的已被上一次 job 消费）。
    """
    items: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read()
        if data:
            items.append((f.filename or "", data))
    if not items:
        raise HTTPException(400, "没有收到照片")   # 一张都没有 → 直接告诉前端
    root = config.sub("uploads")                 # data/station/uploads
    purge_old(root)                              # 只留最近这一批
    d, n = store_upload(root, items, label.strip())
    return {"dir": d, "count": n, "label": label.strip()}


MAX_PORTRAITS = 5            # 视频模型 Flash 档最多接受 5 张参考图


@router.post("/api/portrait")
async def upload_portrait(files: list[UploadFile] = File(...), request: Request = None):
    """收 1–5 张人像照片 → 归一化 → 落**文件区**，返回按序的 fid 列表。

    ★★ 与上面 `POST /api/photos` 的两处关键差别（改之前先看清）：
      ① **要登录**（那个端点没有鉴权，是历史遗留）；
      ② 落 `files/` 而**不是** `uploads/` —— `uploads/` 会被下一次 `POST /api/photos`
         的 `purge_old` **无条件清空**（它 rmtree 掉底下每一个子目录，不看时间）。
         status.md 记过同一个坑：对账目录 `uploads/final/` 就是这么被删掉的。
    ★ 落盘前用 `files.image.normalize_jpeg` 过一遍：缩尺寸 + **脱掉 EXIF（含 GPS）**，
      并保证"提取特征用的图"与"交给视频模型的参考图"是同一份字节。
    ★ `portrait_index` 是**批内次序**，它决定提示词里 `<Picture N>` 的编号 ——
      不能靠 created 时间戳推（同批连着写可能撞同一个值），所以显式记下来。
    """
    u = require_user(request)
    items: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read()
        if data:
            items.append((f.filename or "", data))
    if not items:
        raise HTTPException(400, "没有收到照片")
    if len(items) > MAX_PORTRAITS:
        raise HTTPException(400, f"最多传 {MAX_PORTRAITS} 张（收到 {len(items)} 张）")

    from station.files import image as image_util
    from station.files import store as fs

    batch = uuid.uuid4().hex[:8]
    fids: list[str] = []
    for i, (name, data) in enumerate(items):
        try:
            norm = image_util.normalize_jpeg(data)
        except ValueError as e:
            # 带上"第几张" —— 一次传 5 张时，只说"这张太小了"用户不知道是哪张
            raise HTTPException(400, f"第 {i + 1} 张：{e}")
        fids.append(fs.save_bytes(
            norm, ".jpg", name=name or f"人像{i + 1}.jpg",
            owner=u["id"], skill_id="video",
            group_key=f"portrait:{u['id']}:{batch}",
            group_label=f"人像参考图 · {time.strftime('%m-%d %H:%M')}",
            extra={"portrait_index": i}))
    return {"files": fids, "count": len(fids)}


MAX_INTERVIEW_AUDIO_BYTES = 300 * 1024 * 1024


INTERVIEW_AUDIO_SUFFIXES = {
    ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".oga", ".opus", ".webm",
}


@router.post("/api/interview/audio")
async def upload_interview_audio(file: UploadFile = File(...),
                                 interview_id: str = Form(""),
                                 request: Request = None):
    """收一份面试音频，流式落文件区，返回文件 id。

    只收音频、不收视频：视频应由用户在本地用 FFmpeg 提取成音频后再传，避免巨量文件
    占用服务器带宽和内存。这里**不放 uploads/**，否则会被下一次档案照片上传的
    `purge_old` 清掉。
    """
    u = require_user(request)
    interview_id = (interview_id or "").strip()
    if interview_id and skill_repository('interview-coach').interview_get(interview_id, u["id"]) is None:
        raise HTTPException(400, "面试档案不存在或不属于当前账号，请重新打开上传卡片。")
    raw = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    suffix = Path(raw).suffix.lower()
    if suffix not in INTERVIEW_AUDIO_SUFFIXES:
        raise HTTPException(
            400, "请上传音频文件（m4a / mp3 / wav / aac / flac / ogg / opus / webm）。"
                 "如果是面试视频，请先在本地用 FFmpeg 提取音频。")

    from station.files import store as fs

    tmp_dir = config.sub("interview_audio_tmp")
    tmp = tmp_dir / (uuid.uuid4().hex + suffix)
    total = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_INTERVIEW_AUDIO_BYTES:
                    raise HTTPException(
                        413, f"音频太大了（超过 {MAX_INTERVIEW_AUDIO_BYTES // 1024 // 1024}MB）。"
                             "建议用 FFmpeg 导出单声道 64kbps M4A 后再上传。")
                out.write(chunk)
        if not total:
            raise HTTPException(400, "音频文件是空的")
        name = raw or f"面试音频{suffix}"
        fid = fs.save_path(
            tmp, suffix, name=name,
            owner=u["id"], skill_id="interview-coach",
            group_key=f"interview-audio:{uuid.uuid4().hex[:8]}",
            group_label=f"面试音频 · {time.strftime('%m-%d %H:%M')}",
            extra={"purpose": "interview_audio"},
        )
        recording = (skill_repository('interview-coach').interview_recording_add(interview_id, u["id"], fid, name)
                     if interview_id else None)
        return {"file": fid, "name": name, "size": total,
                "mime": file.content_type or "", "recording_id": (recording or {}).get("id", ""),
                "interview_id": interview_id}
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:                         # noqa：临时文件清理失败不影响上传结果
            pass
