"""file store —— 内容寻址存储 + 原始文件名登记 + mime 预览映射。

规则：data/station/files/<sha1><suffix> 存数据，同名 <sha1>.json 存 {name, suffix}。
同内容去重（同 sha1 命中即返回原 id）。服务端按 mime 决定预览/下载。

新手视角：这像“带取件码的文件柜”。
  - 存文件：save_bytes 按内容算一个唯一编号(sha1)当文件名，同内容再存也不会重复占位。
  - 取文件：GET /api/files/<id> → 浏览器能预览(图/pdf/md)或下载。
  - 它是“产物区”：pipeline/agent 生成的周报、4件套都落这里；“我的产物”抽屉
    从这里取清单，并借助 owner/group 元数据按“一次产出”分组展示。
"""
from __future__ import annotations

import hashlib     # 算 sha1：同一段内容永远算出同一个编号 → 天然去重
import json
import time
from pathlib import Path
from typing import Iterator

from station import config

# 文件后缀 → 浏览器用的 MIME 类型（决定它是“在页内预览”还是“触发下载”）
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".md": "text/markdown", ".txt": "text/plain",
    ".json": "application/json", ".html": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _dir() -> Path:
    """存放目录（不存在会自动建）。每次调用现取，保证配置改了也生效。"""
    return config.sub("files")


def save_bytes(data: bytes, suffix: str = ".txt", name: str = "",
               *, owner: str = "", skill_id: str = "",
               group_key: str = "", group_label: str = "",
               extra: dict | None = None) -> str:
    """写一个文件，返回它的 id（内容编号）。

    data   文件内容（bytes）；suffix 后缀（决定 mime）；name 人看的文件名（下载时用）。
    同内容再次保存 → sha1 相同 → 不重复写，直接返回同一个 id。
    owner/skill_id/group_key/group_label 是可选的来源元数据（谁生成/哪个技能/
    哪批产物），前端按它们分组；“我的产物”只会显示 owner 是自己的文件。
    extra  额外的自定义元数据（原样并进去）。

    ★ extra 是为「**同一批文件里的次序**」加的（09-14，人像参考图）：一次上传几张照片时，
      它们在同一批里谁排第一是有意义的（就是提示词里 `<Picture 1>` 的编号），
      而 `created` 是**写入时刻的时间戳** —— 连着写几张可能撞到同一个值，
      靠它排序在关键场景会**静默指错**。
      ⚠️ 但要注意元数据是**按内容覆盖写**的：同一份字节被两处保存时，后写的 extra
      会盖掉先写的（和 owner 一样）。所以 extra 只放"这份内容是什么"，别放"谁在用"。
    """
    # sha1 是对“内容”算指纹：一模一样的字节必得同样的 id（去重的关键）
    fid = hashlib.sha1(data).hexdigest()
    mp = _dir() / (fid + ".json")
    meta = {}
    if mp.is_file():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:                                # noqa：坏元数据当新文件重写
            meta = {}
    meta.setdefault("created", time.time())
    meta["name"] = name or meta.get("name") or ("file" + (suffix or ""))
    meta["suffix"] = suffix
    meta["mime"] = MIME.get(suffix, "application/octet-stream")
    if owner:       meta["owner"] = owner
    if skill_id:    meta["skill_id"] = skill_id
    if group_key:   meta["group_key"] = group_key
    if group_label: meta["group_label"] = group_label
    if extra:       meta.update(extra)
    (_dir() / (fid + suffix)).write_bytes(data)
    mp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return fid


def save_text(text: str, suffix: str = ".txt", name: str = "", **kw) -> str:
    """便捷版：直接存一段文本（自动转成 utf-8 字节）。"""
    return save_bytes(text.encode("utf-8"), suffix, name, **kw)


def path(fid: str) -> Path | None:
    """按 id 找真实文件路径；不存在返回 None（而不是抛异常）。"""
    if not fid:
        return None
    mp = _dir() / (fid + ".json")                        # 先读元数据才知道后缀
    if not mp.is_file():
        return None
    meta = json.loads(mp.read_text(encoding="utf-8"))
    p = _dir() / (fid + meta.get("suffix", ""))
    return p if p.is_file() else None


def meta(fid: str) -> dict:
    """按 id 读元数据（名字/mime）。没有就返回空 dict。"""
    mp = _dir() / (fid + ".json")
    if mp.is_file():
        return json.loads(mp.read_text(encoding="utf-8"))
    return {}


def iter_files(owner: str = "") -> Iterator[dict]:
    """列出文件区文件（含 id/来源/时间）；owner 给了就只返回该用户可见的。"""
    for mp in sorted(_dir().glob("*.json")):             # 扫所有“元数据文件”
        d = json.loads(mp.read_text(encoding="utf-8"))
        d["id"] = mp.stem                                # 元数据文件名 = 文件 id
        if "created" not in d:
            d["created"] = mp.stat().st_mtime            # 旧文件兜底用 mtime
        if owner and d.get("owner") and d["owner"] != owner:
            continue
        yield d
