"""附件配置：环境变量控制资源上限，固定快照保证重试使用同一处理策略。"""
from __future__ import annotations

import hashlib
import json
import os


def limits() -> dict:
    """限制是解析资源边界，超限明确报错；不偷偷截断文档。"""
    return {
        "max_bytes": int(os.getenv("STATION_DOCUMENT_MAX_BYTES", str(64 * 1024 * 1024))),
        "max_files": int(os.getenv("STATION_DOCUMENT_MAX_FILES", "10")),
        "max_unit_chars": int(os.getenv("STATION_DOCUMENT_MAX_UNIT_CHARS", "8000000")),
        "max_units": int(os.getenv("STATION_DOCUMENT_MAX_UNITS", "10000")),
        "shard_bytes": int(os.getenv("STATION_DOCUMENT_SHARD_BYTES", str(16 * 1024 * 1024))),
        "timeout_seconds": int(os.getenv("STATION_DOCUMENT_TIMEOUT", "300")),
        "worker_memory_mb": int(os.getenv("STATION_DOCUMENT_WORKER_MEMORY_MB", "768")),
    }


def snapshot() -> dict:
    """配置随附件轮次持久化；后续改默认值不会改变已经排队的处理。"""
    resource_limits = limits()
    if any(value <= 0 for value in resource_limits.values()) or resource_limits["shard_bytes"] < 32768:
        raise ValueError("附件资源限制必须为正数，分片上限至少为 32 KiB")
    return {"version": 1, "cleaning": "conservative-text-v1", "splitting": "recursive-v1",
            "chunk_size": 1000, "chunk_overlap": 150,
            "separators": ["\n\n", "\n", "。", "！", "？", "；", " ", ""],
            "limits": resource_limits}


def fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fingerprints(source_hash: str, value: dict, versions: dict) -> tuple[str, str]:
    """物理分片/资源上限不改变内容身份；只改切块参数也不改变 parsed 身份。"""
    parse = fingerprint({"source": source_hash, "cleaning": value["cleaning"],
                         "version": value["version"], "versions": {k: v for k, v in versions.items()
                         if k != "langchain-text-splitters"}})
    chunks = fingerprint({"parsed": parse, "strategy": value["splitting"],
                          "size": value["chunk_size"], "overlap": value["chunk_overlap"],
                          "separators": value["separators"],
                          "splitter_version": versions.get("langchain-text-splitters")})
    return parse, chunks
