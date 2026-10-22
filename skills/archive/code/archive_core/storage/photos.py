"""照片列表/排序（拍摄时间、文件名自然序、修改时间）。页面校正已废弃（语义模式不用）。

新手视角（Java 朋友版）：这个文件很“工具性”——回答一个问题：
“给我一个装满图片的文件夹，怎么把它们排成 1、2、3…的顺序？”
  默认按拍摄时间(EXIF)排；没 EXIF 的垫后；也能改按文件名/修改时间排。
“seq(卷内顺序)”是后面一切(切份/导出/原件 PDF)的基础，所以排序规则在这集中定义。
"""
from __future__ import annotations

import os

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}   # 认哪些后缀算图片


def _natural_key(s: str):
    """文件名“自然排序”键：让 photo2 排在 photo10 前面（字典序会把 10 排在 2 前，不对）。

    做法：把文件名拆成“文本/数字”交替的片段，数字转成 int 再比较，
    于是 photo2 → ['photo',2]、photo10 → ['photo',10]，2 < 10 顺序就对。
    """
    import re
    name = os.path.basename(s).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]
    # 正则 (\d+) 带括号会把“被切下来的数字”也放进结果 → 所以能区分文本和数字


def _exif_time(path: str):
    """读图片的 EXIF 拍摄时间 → ISO 串；没拍到返回 None。

    EXIF = 相机/手机写进照片文件的元数据；0x9003=DateTimeOriginal(拍摄时间)。
    """
    try:
        from PIL import Image
        img = Image.open(path)          # 打开图片（只读元数据，不整张解码）
        ex = img.getexif()
        dt = ex.get(0x9003) or ex.get(0x0132)   # DateTimeOriginal / DateTime
        img.close()
        if dt:
            return str(dt).replace(":", "-")    # "2026:09:03" → "2026-09-03"(可排序)
    except Exception:                   # noqa  # 坏图/没 EXIF 都不崩
        pass
    return None


def list_photos(photos_dir: str, order: str = "拍摄时间") -> dict:
    """列出目录下所有图片文件，并按策略排好序。

    返回 {"files": [绝对路径…], "warnings": [提示…]}。
    order 取值：拍摄时间(默认) / 修改时间 / 文件名。
    """
    files = []
    for name in sorted(os.listdir(photos_dir)):     # 先按文件名粗排一遍（保证确定性）
        # 只收图片后缀；os.path.join 把目录和文件名拼成完整路径
        if os.path.splitext(name)[1].lower() in IMG_EXT:
            files.append(os.path.join(photos_dir, name))
    warnings = []
    if order == "拍摄时间":
        # 关键：key 函数返回“这个文件该用来排顺序的值”；没有拍摄时间的给个超大串(9x20)垫底
        files.sort(key=lambda p: (_exif_time(p) or "9" * 20, _natural_key(p)))
    elif order == "修改时间":
        files.sort(key=lambda p: os.path.getmtime(p))   # 按文件最后修改时间
    else:                                             # 文件名（自然序）
        files.sort(key=_natural_key)
    return {"files": files, "warnings": warnings}
