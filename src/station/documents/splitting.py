"""现成递归字符切块器；字符偏移核对后才可用于来源追溯。"""
from __future__ import annotations


def splitter(settings: dict):
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    return RecursiveCharacterTextSplitter(chunk_size=settings["chunk_size"],
        chunk_overlap=settings["chunk_overlap"], length_function=len, keep_separator=True,
        add_start_index=True, separators=settings["separators"], is_separator_regex=False)


def split(text: str, settings: dict):
    from langchain_core.documents import Document
    for chunk in splitter(settings).split_documents([Document(page_content=text)]):
        start = chunk.metadata.get("start_index", -1)
        if start < 0 or text[start:start + len(chunk.page_content)] != chunk.page_content:
            raise ValueError("切块来源偏移校验失败")
        yield start, chunk.page_content
