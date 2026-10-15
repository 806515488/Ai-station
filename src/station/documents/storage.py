"""文档数据卷与发布：使用用户/文档 ID 建目录，用户文件名只用于显示。

每次运行有独立 staging 目录；分片和清单校验完成后，service 才更新数据库的已发布
引用。读取只按 manifest 中登记的资源 ID 查找，不提供任意服务器路径读取。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Iterator

from station import config


def root(owner: str, document_id: str) -> Path:
    # 既有 users.id 是短 ID；文档 ID 是完整 UUID。两者分别验证，仍不允许路径分隔符。
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", owner) or not re.fullmatch(r"[a-f0-9]{32}", document_id):
        raise ValueError("无效文档身份")
    return config.sub("documents", owner, document_id)


def run_root(owner: str, document_id: str, run_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("无效处理版本")
    return root(owner, document_id) / "runs" / run_id


def atomic_json(path: Path, value: dict) -> None:
    """同目录写临时文件再替换，崩溃不能把已发布清单截成半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resource(document_id: str, run_id: str, folder: Path, relative: str) -> dict:
    path = folder / relative
    return {"id": uuid.uuid5(uuid.NAMESPACE_URL, document_id + ":" + run_id + ":" + relative).hex,
            "path": relative, "name": relative, "byte_size": path.stat().st_size,
            "sha256": sha256(path)}


class JsonlWriter:
    """每行一条完整记录，按字节分片；绝不在一条 JSON 中间换文件。"""
    def __init__(self, folder: Path, category: str, max_bytes: int):
        self.folder, self.category = folder, category
        self.max_bytes = max_bytes
        self.shards: list[dict] = []
        self.stream = None
        self.current: dict = {}

    def write(self, record: dict, record_id: str) -> None:
        from station.documents.quality import validate
        validate("parsed" if self.category == "parsed" else "chunk", record)
        line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(line) > self.max_bytes:
            raise ValueError("单条记录超过分片上限，请增加分片限制或补充结构分组适配")
        if self.stream is None or self.current["byte_size"] + len(line) > self.max_bytes:
            self.close()
            relative = f"{self.category}/{len(self.shards) + 1}.jsonl"
            path = self.folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = path.open("wb")
            self.current = {"index": len(self.shards) + 1, "path": relative,
                            "record_count": 0, "byte_size": 0,
                            "first_id": record_id, "last_id": record_id}
        self.stream.write(line)
        self.current["record_count"] += 1
        self.current["byte_size"] += len(line)
        self.current["last_id"] = record_id

    def close(self) -> None:
        if self.stream is not None:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.current["sha256"] = sha256(self.folder / self.current["path"])
            self.shards.append(self.current)
            self.stream = None


def records(folder: Path, shards: list[dict], *, verify: bool = True) -> Iterator[dict]:
    """严格按清单顺序读；校验文件，避免恢复时拼进别的运行或半写结果。"""
    for shard in shards:
        relative = Path(shard["path"])
        path = (folder / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(folder.resolve()):
            raise ValueError("清单路径越界")
        if verify and (path.stat().st_size != shard["byte_size"] or sha256(path) != shard["sha256"]):
            raise ValueError("文档结果校验失败，请重新处理")
        count = 0
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    raise ValueError("文档分片末行未完成")
                count += 1
                yield json.loads(line)
        if count != shard["record_count"]:
            raise ValueError("文档记录数量不一致")


def manifest(document: dict, run_id: str | None = None) -> tuple[Path, dict]:
    run_id = run_id or document.get("latest_run")
    if not run_id or run_id not in document.get("runs", {}):
        raise ValueError("文件还没有可用的解析结果")
    folder = run_root(document["owner_id"], document["id"], run_id)
    value = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if value["document_id"] != document["id"] or value["run_id"] != run_id:
        raise ValueError("文档清单身份不一致")
    return folder, value
