"""使用本地 JSON Schema 验证导出契约；schema 只引用包内定义，不解析网络资源。"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=3)
def validator(kind: str):
    if kind not in {"manifest", "parsed", "chunk"}:
        raise ValueError("未知文档契约")
    from jsonschema import Draft202012Validator
    schema = json.loads(files("station.documents").joinpath("schemas", kind + ".json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(kind: str, value: dict) -> None:
    errors = list(validator(kind).iter_errors(value))
    if errors:
        # 错误只报告字段位置，不把用户原文拼进日志。
        field = ".".join(map(str, errors[0].path)) or "root"
        raise ValueError(f"文档 {kind} 契约校验失败：{field}")
