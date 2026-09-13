"""uploads —— 客户端照片上传落盘（纯函数，离线可测）。

非技术用户在自己电脑选"这位干部整卷翻拍照片的文件夹/多张照片"，浏览器把它
POST 到宿主；宿主把每张照片落成一个临时目录 data/station/uploads/<人名>-<uid>/，
返回该目录绝对路径给前端，前端把它作为 job 的 photos_dir 传给 archive 去跑。

目录为什么带人名：目录 = "这一卷是谁的"。人名优先取"干部姓名"，没填则自动取
所选文件夹的名字；再加一小段 uid 防同名人/重复整理撞名。archive 没填姓名时默认
拿 photos_dir 的目录名当干部名，所以目录可读 = 产物文件名（李明-人事档案目录.xlsx）
也可读，不会出现 "732c9e…-人事档案目录.xlsx" 这种。

设计取舍：
  - 只存文件、不动 archive：目录结构恰好等于 archive storage.create_project 要的
    "一个装满图片的文件夹"，引擎照旧复制入库，改动面最小。
  - 保留原始文件名：archive 建项目默认按拍摄时间(EXIF)排、没 EXIF 按文件名自然序
    排 → 卷顺序可复现。
  - 一次只留最近一批：新上传先把旧目录清掉（单用户顺序流程，旧的已被消费）。
"""

from __future__ import annotations

import os
import re
import uuid


def sanitize_name(name: str) -> str:
    """把浏览器传来的文件名洗成"安全文件名"：只留最后一段 + 白名单字符。

    防路径穿越（../x、C:\\x）与奇怪字符：取 basename，再只留中文/字母/数字/_-.，
    其余替换成 _。archive 只认图片后缀，非图会被建项目跳过，无碍。
    """
    base = os.path.basename((name or "").replace("\\", "/"))
    safe = re.sub(r"[^\w一-鿿.\- ]", "_", base).strip() or "photo"
    return safe[:120]


def _unique_path(dest: str, name: str) -> str:
    """同一目录下避免重名（文件夹里偶有不同子目录同名照片）：加 (1)/(2)…"""
    p = os.path.join(dest, name)
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(p):
        p = os.path.join(dest, f"{stem}({i}){ext}")
        i += 1
    return p


def store_upload(root: str, items: list[tuple[str, bytes]],
                 label: str = "") -> tuple[str, int]:
    """把 (文件名, 字节) 列表落到 root/<人名>-<uid>/，返回 (目录绝对路径, 张数)。

    label 是"这一卷是谁的"（干部姓名/所选文件夹名），可读 + 去撞名。
    """
    os.makedirs(root, exist_ok=True)
    who = sanitize_name(label or "照片")            # 空标签给通用名"照片"
    dest = os.path.join(root, f"{who}-{uuid.uuid4().hex[:8]}")
    os.makedirs(dest)
    n = 0
    for name, data in items:
        if not data:
            continue
        p = _unique_path(dest, sanitize_name(name))
        with open(p, "wb") as f:
            f.write(data)
        n += 1
    return dest, n


def purge_old(root: str) -> None:
    """清空 root 下的旧上传目录（每次新上传前调，只留正在用的这一批）。"""
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            import shutil
            shutil.rmtree(p, ignore_errors=True)
