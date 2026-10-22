"""档案资源：本人卷页预览与用户回传的终版目录。"""
from __future__ import annotations

from station.skills.storage import repository as skill_repository


from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from station import config
from station.app.auth import require_user

router = APIRouter(tags=["档案资源"])


def _owned_project(project: str, user: dict) -> None:
    """校验这份卷属于当前登录用户；不属于/不存在 → 404（防探测，不用 403）。

    ★ 经技能仓库查归属，读取和游标消费都受 Database 作用域保护。
      全景卡一次要拉上百张缩略图，每张都过这道校验；裸用连接会和聊天流里的
      `thread.save()` 抢同一条 sqlite 连接 → 部分缩略图拿到 401/500，
      在浏览器里就是**一片破图**（这是 09-11 报的那个 bug 的另一半）。
    """
    if skill_repository('archive').archive_state_owner(project) != user["id"]:
        raise HTTPException(404, "档案卷不存在")


@router.post("/api/archive/reconcile")
async def archive_reconcile(file: UploadFile = File(...), project: str = Form(""),
                            request: Request = None):
    """收下"用户改过的终版目录 xlsx" → 落到 data/station/uploads/final/ → 把路径回给前端。

    ★ 为什么不复用 `/api/photos`：① 那里有个 `purge_old` 会**清空上一次的照片目录** ——
      手上正在整理的那卷照片可能就在里面，传个 xlsx 把照片删了是灾难；② 那里没有
      `require_user`，而这份表要按卷做归属校验；③ 生命周期与落盘目录都不同。
    真正干活的是 `archive_core.service.learn.reconcile`（由工具 `reconcile` 在对话里调），
    这里只负责"收文件 + 鉴权 + 告诉前端路径"。
    """
    u = require_user(request)
    _owned_project(project, u)
    name = os.path.basename((file.filename or "").strip())     # ★ 去掉任何目录成分，防穿越
    if not name.lower().endswith(".xlsx"):
        raise HTTPException(400, "请上传 .xlsx 文件（Excel 目录）。")
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的。")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件太大了（超过 20MB）。")
    # ★ 放**独立**目录，别塞在 `uploads/` 底下：`POST /api/photos` 会调 `purge_old`
    #   把 `uploads/` 的每个子目录 rmtree 掉（重传照片是常规流程），塞在里面的话
    #   用户一重传照片，刚传上来的终版目录就被删了、而 agent 手上还攥着那个路径
    #   （review 抓到的真 bug：之后对账会报 FileNotFoundError）。
    d = config.sub("reconcile")
    d.mkdir(parents=True, exist_ok=True)
    dst = d / name
    dst.write_bytes(data)
    return {"path": str(dst), "name": name}


@router.get("/api/archive/page/{seq}")
def archive_page(seq: int, project: str, thumb: int = 0, request: Request = None):
    """返回某页图（page-card 的 img 用）；thumb=1 时返回压缩过的缩略图。

    产物按 (卷, 页, 宽度, 旋转角) 缓存在 data/station/thumbs/：全景卡一展开就是
    上百张，每张都现缩会拖死列表，缓存后只有第一次慢；原图被改过就重做。
    带 rotate 的页（人工确认的横躺照片）会转正后再发 —— 那些照片压根没写 EXIF
    方向，浏览器无从判断该怎么转，只能由服务端按 photos.json 里的 rotate 转。
    """
    u = require_user(request)
    _owned_project(project, u)
    from station.skills.registry import get_registry
    try:
        entry = get_registry().entry_module("archive")
        path, media_type = entry.page_resource(project, seq, bool(thumb))
    except LookupError:
        raise HTTPException(404, "页不存在")
    return FileResponse(path, media_type=media_type)
