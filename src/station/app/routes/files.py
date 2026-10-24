"""产物文件：本人产物目录、预览下载和有限期外部取件令牌。

普通文件按登录用户校验归属；/api/ref/{token} 以不可猜且会过期的令牌授权，
只查服务端映射并校验文件 ID，不接受路径，不缓存或携带原始文件名。
"""
from __future__ import annotations

from station.persistence.repositories import shares as share_records


import re
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from station.app.auth import require_user

router = APIRouter(tags=["产物文件"])


def _kind_rank(name: str) -> int:
    """给同组内文件排展示顺序：档案 4 件套固定顺序，周报 md 在 docx 前。"""
    if "人事档案目录" in (name or ""): return 0
    if "档案原件" in (name or ""):      return 1
    if "待核对清单" in (name or ""):    return 2
    if "判定报告" in (name or ""):      return 3
    if (name or "").lower().endswith(".md"):    return 4
    if (name or "").lower().endswith(".docx"):  return 5
    return 9


def _legacy_group(name: str, fid: str) -> tuple[str, str]:
    """老文件没有来源元数据时，从文件名猜“属于哪一件事”（卷/周报/任务）。"""
    n = name or ""
    for sep in ("-人事档案目录", "-档案原件", "-待核对清单", "-自动判定报告"):
        if sep in n:
            person = n.split(sep, 1)[0]
            return f"legacy-archive:{person}", f"干部档案 · {person}"
    if n.startswith("周报-"):
        week = n[3:].rsplit(".", 1)[0]
        return f"legacy-weekly:{week}", f"写周报 · {week}"
    if n.startswith("周报."):
        return "legacy-weekly:draft", "写周报"
    if n.startswith("demo-"):
        tag = n[5:].rsplit(".", 1)[0] or "demo"
        return f"legacy-demo:{tag}", f"Demo 流水线 · {tag}"
    return f"file:{fid}", (n or "产物文件")[:30]


def _group_products(files: list) -> list:
    """把文件列表按 group_key 聚合成“产物组”（组内同名只留最新一份）。"""
    groups: dict = {}
    for f in files:
        name = f.get("name") or f.get("id") or ""
        gkey, glabel = _legacy_group(name, f.get("id", ""))
        gkey = f.get("group_key") or gkey               # 新文件优先用落盘来源
        glabel = f.get("group_label") or glabel
        created = float(f.get("created") or 0)
        g = groups.setdefault(gkey, {"key": gkey, "label": glabel,
                                     "created": 0, "_files": {}})
        g["label"] = glabel
        g["created"] = max(g["created"], created)
        old = g["_files"].get(name)
        row = {"id": f["id"], "name": name, "created": created}
        if old is None or created > old["created"]:
            g["_files"][name] = row                      # 重出件：同名取最新那份
    out = []
    for g in groups.values():
        rows = sorted(g.pop("_files").values(),
                      key=lambda x: (_kind_rank(x["name"]), x["name"]))
        out.append({"key": g["key"], "label": g["label"],
                    "created": g["created"], "files": rows})
    out.sort(key=lambda g: g["created"], reverse=True)
    return out


@router.get("/api/files")
def list_files(request: Request):
    """当前登录用户的产物组（我的产物抽屉数据源）。"""
    u = require_user(request)
    from station.files import store as fs       # 函数内 import，保持 server 顶层轻量
    mine = [f for f in fs.iter_files(u["id"])]  # owner 已由 store 过滤（历史文件兼容可见）
    return {"groups": _group_products(mine)}


@router.get("/api/files/{fid}")
def get_file(fid: str, dl: int = 0, request: Request = None):
    """取一个文件：dl=0 浏览器里预览(inline)，dl=1 触发下载。

    FastAPI 会按 URL 的 query ?dl=1 把值传给参数 dl。FileResponse 直接回文件流。
    """
    from station.files import store as fs
    u = require_user(request)
    meta = fs.meta(fid)
    owner = meta.get("owner") or ""
    if owner and owner != u["id"]:
        raise HTTPException(404, "文件不存在")            # 不是你的 → 404（防探测）
    p = fs.path(fid)
    if p is None:
        raise HTTPException(404, "文件不存在")
    media = meta.get("mime") or "application/octet-stream"
    if dl:
        return FileResponse(p, media_type=media,
                            filename=meta.get("name", "download"))  # 带下载名
    return FileResponse(p, media_type=media)                        # 浏览器预览


_REF_FID_RE = re.compile(r"^[0-9a-f]{40}$")


@router.get("/api/ref/{token}")
def get_shared_file(token: str):
    """凭令牌取一份文件（**免登录**，给 Agnes 抓人像参考图用）。"""
    from station.files import store as fs

    fid = share_records.ref_link_resolve(token or "")          # 过期判断在 SQL 里，这里不碰时间
    if not fid or not _REF_FID_RE.match(fid):
        raise HTTPException(404, "链接不存在或已过期")   # 一律 404，防探测
    p = fs.path(fid)
    if p is None:
        raise HTTPException(404, "文件不存在")          # 表里有行但文件没了 → 404 不是 500
    meta = fs.meta(fid)
    resp = FileResponse(p, media_type=meta.get("mime") or "application/octet-stream")
    resp.headers["Cache-Control"] = "no-store"      # 任何中间代理都不该缓存用户的照片
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp
