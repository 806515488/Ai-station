"""调用现成 cleaners；未知结构保留换行，清洗记录与提取原文一起保存。"""
from __future__ import annotations


def clean(text: str, suffix: str, metadata: dict) -> tuple[str, list[dict]]:
    try:
        from unstructured.cleaners.core import clean_extra_whitespace, replace_unicode_quotes
    except ImportError as e:
        from station.documents.loaders import DocumentError
        raise DocumentError("unavailable", "基础清洗依赖未安装，请安装 documents 依赖组") from e
    # Markdown、表格、代码和结构未知内容不压平；保守 profile 可以是有记录的 no-op。
    category = metadata.get("category", "")
    prose = suffix in {".txt", ".pdf", ".docx"} or category == "NarrativeText"
    transforms = []
    functions = [replace_unicode_quotes] if prose and category not in {"Table", "CodeSnippet", "Formula"} else []
    if category == "NarrativeText" and "\n" not in text and suffix != ".md":
        functions.append(clean_extra_whitespace)
    for function in functions:
        value = function(text)
        if value != text:
            transforms.append({"function": function.__name__, "profile": "conservative-text-v1",
                               "before_chars": len(text), "after_chars": len(value)})
        text = value
    return text, transforms
