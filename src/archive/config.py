"""archive.config —— 配置/密钥加载（极简 .env 读取，零依赖）。

新手视角（Java 朋友版）：archive 自己的“配置文件加载器”。它读 src/.env（相当于
把密钥放进环境变量）。说明：与 station/config.py 职责重复但各自独立——两边都能读
同一份 src/.env 的 key，保持两个包解耦（archive 不 import station，反之亦然）。
（旧版曾在这里做“多通道 base_url 覆盖”的解析，主链路已不用、属历史残留，已删。）
"""
from __future__ import annotations

import os
import sys


def app_dir() -> str:
    """应用根目录：源码运行 = src 目录（.env 就放这）。

    保留兼容：若将来打包成单文件 exe，再在 frozen 分支返回 exe 同目录。
    """
    if getattr(sys, "frozen", False):          # PyInstaller 打包运行时
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str | None = None) -> dict:
    """读取 .env 并注入 os.environ（不覆盖已存在的同名变量）。返回读到了哪些键值。

    查找顺序：指定路径 > 默认 src/.env > 当前工作目录 .env。
    """
    if path is None:
        cand = os.path.join(app_dir(), ".env")
        path = cand if os.path.isfile(cand) else os.path.join(os.getcwd(), ".env")
    loaded = {}
    if not os.path.isfile(path):
        return loaded
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
                loaded[k] = v
    return loaded
