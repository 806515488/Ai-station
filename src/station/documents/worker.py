"""独立解析进程：只读本地原件，调用开源组件并写未发布的运行目录。

父进程负责数据库/鉴权/发布，子进程不打开数据库，也不调用模型。stdout 只传短小
JSON 事件；原文留在数据卷，不塞进任务日志。解析崩溃可由父进程回收并重跑。
"""
from __future__ import annotations

import itertools
import json
import sys
import uuid
from pathlib import Path

from station.documents import cleaning, loaders, settings, splitting, storage


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def safe_metadata(metadata: dict) -> dict:
    """源路径换成文档引用，其他真实元数据保持 JSON 可读，不公开主机路径。"""
    return json.loads(json.dumps({k: v for k, v in metadata.items()
                                 if k not in {"source", "file_directory", "filename", "file_path"}},
                                ensure_ascii=False, default=str, allow_nan=False))


def units_from_parsed(folder: Path, manifest: dict):
    """同用户缓存复用原提取文本；新文档仍生成自己的身份和来源。"""
    from langchain_core.documents import Document
    for row in storage.records(folder, manifest["parsed"]["shards"]):
        if row["unit_part_index"] != 1:
            continue
        raw = row["raw_text"]
        if raw is None:
            relative = row["raw_text_ref"]
            entry = next(x for x in manifest["resources"] if x["path"] == relative)
            path = (folder / relative).resolve()
            if not path.is_relative_to(folder.resolve()) or storage.sha256(path) != entry["sha256"]:
                raise ValueError("原文缓存校验失败")
            raw = path.read_text(encoding="utf-8")
        yield Document(page_content=raw, metadata=row["loader_metadata"])


def process(payload: dict) -> None:
    document, cfg = payload["document"], payload["config"]
    folder, source = Path(payload["folder"]), Path(payload["source"])
    folder.mkdir(parents=True, exist_ok=True)
    run_id, doc_id = payload["run_id"], document["id"]
    parse_fp, fingerprint = settings.fingerprints(document["sha256"], cfg, loaders.versions())
    parsed_id = uuid.uuid5(uuid.NAMESPACE_URL, doc_id + ":parsed:" + parse_fp).hex
    chunk_set_id = uuid.uuid5(uuid.NAMESPACE_URL, parsed_id + ":chunks:" + fingerprint).hex
    issues, resources = [], []
    writer = storage.JsonlWriter(folder, "parsed", cfg["limits"]["shard_bytes"])
    count, nonempty, raw_units = 0, 0, 0
    cache = payload.get("cache")
    loader_name = ""
    if cache:
        cached_folder = Path(cache["folder"])
        cached_manifest = json.loads((cached_folder / cache["manifest"]).read_text(encoding="utf-8"))
        units = units_from_parsed(cached_folder, cached_manifest)
        loader_name = cached_manifest["processing"]["loader"]
    else:
        loader, loader_name = loaders.create(source, document["suffix"])
        units = loader.lazy_load()
    pdf_reader = None
    try:
        for unit_index, unit in enumerate(units, 1):
            raw_units = unit_index
            if unit_index > cfg["limits"]["max_units"]:
                raise loaders.DocumentError("resource_limit", "处理单元数量超过上限")
            raw = unit.page_content
            if len(raw) > cfg["limits"]["max_unit_chars"]:
                raise loaders.DocumentError("resource_limit", "单个文档单元超过解析资源限制")
            metadata = safe_metadata(unit.metadata)
            text, transforms = cleaning.clean(raw, document["suffix"], metadata)
            locations = [{"kind": "document", "unit_index": unit_index}]
            if document["suffix"] == ".pdf" and "page" in metadata:
                locations = [{"kind": "pdf", "page_index": int(metadata["page"]) + 1,
                              "page_label": metadata.get("page_label")}]
            elif document["suffix"] in {".csv", ".tsv"}:
                locations = [{"kind": "csv", "loader_row_index": metadata.get("row")}]
            elif metadata.get("page_name"):
                locations = [{"kind": "excel", "sheet_name": metadata["page_name"], "cell_range": None}]
            if text.strip():
                nonempty += 1
            elif document["suffix"] == ".pdf":
                # 真空白页可接受；有绘制内容却没提取到文字，必须标记能力缺口。
                if pdf_reader is None:
                    from pypdf import PdfReader
                    pdf_reader = PdfReader(source)
                page = pdf_reader.pages[int(metadata.get("page", unit_index - 1))]
                contents = page.get_contents()
                if contents is not None and contents.get_data().strip():
                    issues.append({"code": "needs_ocr", "severity": "warning",
                                   "message": "此页有绘制内容但未提取到文字，需要 OCR 或专项解析",
                                   "source_locations": locations})
            unit_id = uuid.uuid5(uuid.NAMESPACE_URL, doc_id + ":unit:" + str(unit_index)).hex
            raw_ref = None
            # 逻辑子记录大小固定，不受物理分片配置影响，保证改变分片不改变来源 ID。
            part_chars = 4096
            if len(raw) > part_chars or len(text) > part_chars or len(raw.encode("utf-8")) > 12000:
                raw_ref = f"raw/{unit_index}.txt"
                path = folder / raw_ref
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8", newline="") as stream:
                    stream.write(raw)
                resources.append(storage.resource(doc_id, run_id, folder, raw_ref))
            # 仅用于存储的子记录保持共同 unit_id；RAG 切块先重组逻辑单元。
            for offset in range(0, max(1, len(text)), part_chars):
                block_id = uuid.uuid5(uuid.NAMESPACE_URL, parsed_id + ":" + unit_id + ":" + str(offset)).hex
                count += 1
                row = {"schema_version": "1.0", "document_id": doc_id,
                       "revision_id": document["sha256"], "parsed_snapshot_id": parsed_id,
                       "block_id": block_id, "unit_id": unit_id, "unit_part_index": offset // part_chars + 1,
                       "unit_text_start": offset, "order": count,
                       "type": "table" if metadata.get("category") == "Table" else "text",
                       "raw_text": raw if raw_ref is None else None, "raw_text_ref": raw_ref,
                       "text": text[offset:offset + part_chars], "section_path": [],
                       "loader_metadata": metadata, "source_locations": locations,
                       "extraction": {"method": "native_text", "backend": loader_name, "confidence": None},
                       "capabilities": {"extraction_scope": "table_view" if metadata.get("text_as_html") else "text",
                                        "supports_layout": False},
                       "transforms": transforms,
                       "quality": {"status": "needs_review" if not text.strip() else "accepted"},
                       "retrieval": {"exclude": not text.strip(), "reason": "empty_text" if not text.strip() else None}}
                writer.write(row, block_id)
            emit(type="progress", message=f"已解析 {unit_index} 个单元", percent=min(65, 10 + unit_index))
    finally:
        writer.close()
    if not nonempty:
        issues.append({"code": "no_text", "severity": "error", "message": "没有可用正文，不能作为已完成提取入库"})
    quality_status = "unusable" if not nonempty else ("partial" if issues else "accepted")
    quality = {"schema_version": "1.0", "status": quality_status, "extraction_scope": "text",
               "processed_units": raw_units, "raw_blocks": count, "issues": issues}
    storage.atomic_json(folder / "quality-parsed.json", quality)
    resources.extend(storage.resource(doc_id, run_id, folder, x["path"]) for x in writer.shards)
    resources.append(storage.resource(doc_id, run_id, folder, "quality-parsed.json"))
    manifest = {"schema_version": "1.0", "document_id": doc_id, "revision_id": document["sha256"],
                "run_id": run_id, "owner_id": document["owner_id"],
                "source": {"name": document["name"], "sha256": document["sha256"],
                           "byte_size": document["size"], "uploaded_at": document["uploaded_at"]},
                "processing": {"loader": loader_name, "config": cfg, "fingerprint": fingerprint,
                               "parse_fingerprint": parse_fp,
                               "versions": loaders.versions(), "cache_hit": bool(cache)},
                "parsed": {"snapshot_id": parsed_id, "status": "ready" if nonempty else "unusable",
                           "block_count": count, "shards": writer.shards},
                "chunk_sets": [], "quality": quality, "resources": resources}
    # parsed 清单写一次后不再修改；对话可用，chunks 仍可继续生成。
    storage.atomic_json(folder / "parsed-manifest.json", manifest)
    emit(type="parsed_ready", parsed_snapshot_id=parsed_id, quality_status=quality_status)
    chunks = storage.JsonlWriter(folder, "chunks/recursive-v1", cfg["limits"]["shard_bytes"])
    chunk_index, previous, pending = 0, None, None
    try:
        for _unit_id, rows in itertools.groupby(storage.records(folder, writer.shards), key=lambda x: x["unit_id"]):
            group = list(rows)
            text = "".join(x["text"] for x in group)
            if not text.strip():
                continue
            for start, body in splitting.split(text, cfg):
                chunk_index += 1
                spans = []
                end = start + len(body)
                for block in group:
                    begin = block["unit_text_start"]
                    left, right = max(start, begin), min(end, begin + len(block["text"]))
                    if right > left:
                        spans.append({"block_id": block["block_id"], "start": left - begin,
                                      "end": right - begin, "role": "body"})
                chunk_id = uuid.uuid5(uuid.NAMESPACE_URL, chunk_set_id + ":" + _unit_id + ":" + str(start)).hex
                row = {"schema_version": "1.0", "chunk_id": chunk_id, "document_id": doc_id,
                       "revision_id": document["sha256"], "parsed_snapshot_id": parsed_id,
                       "chunk_set_id": chunk_set_id, "chunk_index": chunk_index,
                       "kind": "table" if group[0]["type"] == "table" else "text", "text": body,
                       "source_spans": spans, "metadata": {"source_name": document["name"],
                       "owner_id": document["owner_id"], "source_locations": group[0]["source_locations"],
                       "extraction_scope": group[0]["capabilities"]["extraction_scope"]},
                       "strategy": {"id": "recursive-v1", "version": 1,
                                    "length_unit": "unicode_codepoint", "config_fingerprint": fingerprint},
                       "prev_id": previous, "next_id": None,
                       "quality": {"status": "needs_review" if row_is_table(group[0]) else quality_status}}
                if pending:
                    pending["next_id"] = chunk_id
                    chunks.write(pending, pending["chunk_id"])
                pending, previous = row, chunk_id
        if pending:
            chunks.write(pending, pending["chunk_id"])
    finally:
        chunks.close()
    resources.extend(storage.resource(doc_id, run_id, folder, x["path"]) for x in chunks.shards)
    if any(x["type"] == "table" for x in storage.records(folder, writer.shards)):
        quality["issues"].append({"code": "table_view", "severity": "warning",
                                 "message": "基础表格视图切块未验证结构保真，请核对表头和单位"})
        if quality["status"] == "accepted":
            quality["status"] = "needs_review"
    quality["chunks"] = chunk_index
    storage.atomic_json(folder / "quality.json", quality)
    resources.append(storage.resource(doc_id, run_id, folder, "quality.json"))
    manifest["chunk_sets"] = [{"chunk_set_id": chunk_set_id, "parsed_snapshot_id": parsed_id,
                              "strategy_id": "recursive-v1", "status": "ready" if nonempty else "failed",
                              "chunk_count": chunk_index, "shards": chunks.shards}]
    manifest["quality"] = quality
    storage.atomic_json(folder / "manifest.json", manifest)
    # 完整重读校验，之后父进程才切数据库中的发布指针。
    for shards in (writer.shards, chunks.shards):
        for _ in storage.records(folder, shards):
            pass
    emit(type="completed", blocks=count, chunks=chunk_index, quality_status=quality["status"])


def row_is_table(row: dict) -> bool:
    return row["type"] == "table"


def main():
    # 文件处理进程明确禁止联网：缺少 NLP/后端资源时失败，不能临时下载或调用 API。
    import socket
    import threading
    original_connect, original_pair = socket.socket.connect, socket.socketpair
    internal = threading.local()
    def offline(*_args, **_kwargs):
        raise loaders.DocumentError("offline_resource_missing", "离线处理缺少本地资源，请在联网准备阶段补齐")
    def local_pair(*args, **kwargs):
        # Windows asyncio 用回环 socketpair 做内部唤醒，它不是访问网络服务。
        internal.pair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            internal.pair = False
    def guarded_connect(sock, address):
        if getattr(internal, "pair", False) and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return offline()
    socket.socketpair = local_pair
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    socket.getaddrinfo = offline
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        frozen = payload["config"].get("components")
        if frozen is not None and frozen != loaders.versions():
            raise loaders.DocumentError("component_version_changed", "组件版本已变化，请重新处理附件")
        process(payload)
    except Exception as e:
        # 不把解析异常中可能夹带的原文/路径写进日志。
        code = getattr(e, "code", "unavailable" if isinstance(e, ImportError) else "parse_failed")
        message = str(e) if isinstance(e, loaders.DocumentError) else "文件处理失败，请核对格式、依赖和资源限制"
        import traceback
        frame = traceback.extract_tb(e.__traceback__)[-1]
        storage.atomic_json(Path(payload["folder"]) / "failure.json",
                            {"code": code, "error_type": type(e).__name__, "module": Path(frame.filename).name,
                             "line": frame.lineno, "function": frame.name, "message": message})
        emit(type="failure", code=code, message=message, error_type=type(e).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
