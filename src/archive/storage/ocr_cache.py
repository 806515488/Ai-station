"""storage.ocr_cache —— 视觉建档（OCR）结果的持久化缓存。

解决什么问题：建档（mark）是整条识别链里最贵的一步——每页要调一次视觉大模型
（126 张 ≈ 90 秒 + 真 API 费）。以前结果只存在内存/一次性项目里，重跑、改口径
重新定类、隔天再来，全都要**再花一遍钱**。本模块把"每页读出了什么"按内容指纹
存成文件，之后任何时候、任何项目，只要这张图没变、建档提示词没变、通道没变，
就直接命中缓存——**OCR 一辈子只花钱一次**。

缓存键（三个因素，缺一不可）：
  1) 图片内容 md5（photos.json 建项目时已算好）——图变了（重拍/裁剪）→ 键变
  2) 通道名（qwen/glm…）——换一家 OCR → 键变，两家的结果**天然共存**，
     这正好支撑"换一家对比"：不覆盖原结果，只是多存了一份
  3) 建档提示词 sha256——提示词/口径升级 → 旧缓存自动失效（但保留在盘上，可对比）

新手视角（Java 朋友版）：这就是一个"按内容寻址的 JSON 缓存表"（≈ Redis 的
string 结构，只是落磁盘）。<仓库>/data/ocr_cache/<键>.json 一个键一个文件，
读 = json.load，写 = json.dump。data/ 已在 .gitignore 里，不入库。

与其它模块的关系（谁调谁）：
  - engine/graph.py 的 node_mark：建档前先 lookup，未命中才真调模型，调完 store
  - engine/graph.py 的 reocr_page：强制不走缓存重读（换通道对比用）
  - 测试里 monkeypatch CACHE_DIR 到临时目录，离线可测
"""
from __future__ import annotations

import hashlib     # 算 sha256：给"建档提示词"算版本指纹
import json
import os
import time

_PKG = os.path.dirname(os.path.abspath(__file__))               # .../src/archive/storage
_REPO = os.path.abspath(os.path.join(_PKG, "..", "..", ".."))   # 仓库根（data/ 在这）
CACHE_DIR = os.path.join(_REPO, "data", "ocr_cache")            # 缓存目录（测试可改写此常量）


def _path(key: str) -> str:
    """缓存键 → 磁盘文件路径：<CACHE_DIR>/<键>.json。"""
    return os.path.join(CACHE_DIR, key + ".json")


def make_key(img_md5: str, channel: str, prompt: str) -> str:
    """拼缓存键：图片内容 + 通道 + 提示词指纹。

    prompt 直接传文本（不传 sha），由这里统一算 sha256——调用方少操心。
    取前 12 位就够（sha256 碰撞概率天文数字级），键短一点文件名好看。
    """
    psha = hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:12]
    return f"{img_md5}-{channel}-{psha}"


def lookup(img_md5: str, channel: str, prompt: str) -> dict | None:
    """查缓存：命中直接返回"内容卡"（_parse_mark 的输出结构），没命中 None。

    注意返回的是卡本身（不是带元数据的外壳）——调用方拿到就能塞 record.ocr。
    """
    p = _path(make_key(img_md5, channel, prompt))
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("card") or None            # 解包：只还内容卡，元数据留在盘上
    except Exception:                            # noqa：文件损坏当未命中，重 OCR 就是
        return None


def store(img_md5: str, channel: str, prompt: str, card: dict,
          model: str = "") -> None:
    """写缓存：把一页的内容卡连同"当时用的模型"一起存盘。

    card 是 engine.graph._parse_mark 的输出（{mark,title,date,texts,cls}）。
    model 记下来是为了"换一家对比"界面能标出"这份是 qwen 读的/那份是 glm 读的"。
    存失败不抛错（缓存坏了不该影响识别主流程，顶多下次重新 OCR）。
    """
    d = {"card": card, "channel": channel, "model": model,
         "created": int(time.time())}
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_path(make_key(img_md5, channel, prompt)), "w",
                  encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)      # 紧凑存（不需要人读，省空间）
    except OSError:
        pass                                         # 磁盘满/只读等 → 放弃缓存


def clear() -> int:
    """清空全部缓存（开发/测试用）。返回删掉的条数。"""
    n = 0
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                    n += 1
                except OSError:
                    pass
    return n
