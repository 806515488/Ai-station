"""LangChain 本地 Loader 注册表；依赖缺失明确报错，不用自制解析器假装成功。"""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

# 在任何 Unstructured import 之前关闭遥测；本地文件接口不调用云端 partition。
os.environ.setdefault("DO_NOT_TRACK", "1")
SUPPORTED = {".txt", ".md", ".csv", ".tsv", ".pdf", ".docx", ".xlsx", ".html", ".htm"}


class DocumentError(ValueError):
    """可展示给用户的确定错误；code 区分依赖、格式和 OCR 能力缺口。"""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def versions() -> dict:
    out = {}
    for name in ("langchain-community", "langchain-text-splitters", "unstructured",
                 "langchain-unstructured", "docx2txt", "pypdf"):
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def verify_signature(path: Path, suffix: str) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(4096)
    if suffix == ".pdf" and b"%PDF-" not in prefix[:1024]:
        raise DocumentError("invalid_format", "文件内容与 PDF 扩展名不符")
    if suffix in {".docx", ".xlsx"} and not prefix.startswith(b"PK"):
        raise DocumentError("invalid_format", "Office 文件内容与扩展名不符")
    if suffix in {".txt", ".md", ".csv", ".tsv", ".html", ".htm"} and b"\0" in prefix:
        raise DocumentError("encoding", "文本包含零字节；请先转换为 UTF-8 编码")


def create(path: Path, suffix: str):
    """构造器只拿服务端登记的本地 Path，客户端无法让 Loader 下载任意 URL。"""
    if suffix not in SUPPORTED:
        raise DocumentError("unsupported", "暂不支持此文件类型")
    verify_signature(path, suffix)
    try:
        from langchain_community.document_loaders import CSVLoader, Docx2txtLoader, PyPDFLoader, TextLoader
        if suffix in {".txt", ".md"}:
            return TextLoader(str(path), encoding="utf-8-sig"), "TextLoader"
        if suffix in {".csv", ".tsv"}:
            return CSVLoader(str(path), encoding="utf-8-sig", csv_args={"delimiter": "\t" if suffix == ".tsv" else ","}), "CSVLoader"
        if suffix == ".pdf":
            from pypdf import PdfReader
            if PdfReader(path).is_encrypted:
                raise DocumentError("needs_password", "加密 PDF 暂不能读取，请上传解密副本")
            return PyPDFLoader(str(path), mode="page"), "PyPDFLoader"
        if suffix == ".docx":
            # Docx2txtLoader 自身懒导入，先检查依赖才能给出准确的 unavailable。
            import docx2txt  # noqa: F401
            return Docx2txtLoader(str(path)), "Docx2txtLoader"
        from langchain_unstructured import UnstructuredLoader
        return UnstructuredLoader(file_path=str(path), partition_via_api=False), "UnstructuredLoader"
    except ImportError as e:
        raise DocumentError("unavailable", "解析依赖未安装，请安装项目的 documents 依赖组") from e
