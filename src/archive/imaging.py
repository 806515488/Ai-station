"""archive.imaging —— 图像/哈希工具（与具体功能无关，供 storage/export 复用）。

- 哈希：md5（内容指纹，判完全重复） / phash（感知哈希，判“看起来一样”）
- 图像：EXIF 矫正直立（用 PIL 的 ImageOps，不手写方向表）

新手视角（Java 朋友版）：这是 archive 的“图像工具类”（≈ImageUtil / HashUtil）。
只留当前语义识别链真会用到的方法：建档/导出要“读图、摆正”；判重复要“算指纹”。
（旧“角标裁切”那套 corner 工具随角标识别一起废弃，已删。）
"""
from __future__ import annotations

import hashlib      # 摘要算法：md5/sha1（内容→固定长度指纹）
import io           # 内存里的“文件对象”（BytesIO）——不落盘就能把图存成字节

from PIL import Image, ImageOps    # Python 图像库；Image=图片对象


def md5_of_file(path: str) -> str:
    """按块读文件的 MD5，用于“完全相同的重复页/重复上传”判定。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        # iter(无参函数, 哨兵值)：反复调用 f.read(1MB) 直到返回空 → 分块读大文件不占内存
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)                        # 把这块喂进哈希
    return h.hexdigest()                           # 十六进制字符串


def phash(img: Image.Image, hash_size: int = 16) -> int:
    """简易感知哈希（DCT-free 的均值哈希改进版，足够判同次拍摄）。

    与 md5 的区别：md5 要“字节完全一样”，phash 是“看起来一样就算同”
    （同一次扫描的同一页，哪怕格式/亮度略不同，phash 也会很接近）。
    """
    g = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)  # 转灰度+缩到 NxN
    px = list(g.getdata())            # 取所有像素值
    avg = sum(px) / len(px)           # 算平均亮度
    bits = 0
    for i, v in enumerate(px):        # 每个像素：比平均亮 → 对应位记 1
        if v > avg:
            bits |= 1 << i            # 位运算：把第 i 位设成 1
    return bits                       # 一个 int，就是这张图的“感知指纹”


def load_upright(path: str) -> Image.Image:
    """按 EXIF 方向载入直立图像（返回 RGB 模式的 PIL 图片对象）。

    ImageOps.exif_transpose 会读照片元数据里的方向并自动转正 —— 处理手机竖拍的关键。
    convert("RGB")：统一成 3 通道，避免后面因模式差异报错。
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def apply_rotate(img: Image.Image, deg) -> Image.Image:
    """按整度数旋转（90/180/270；正值=逆时针，与 PIL 一致）；0 或空原样返回。

    为什么要这个：实测某卷 126 张里 113 张是"像素横躺、EXIF 又没写方向"（翻拍设备
    没写方向信息），程序无从判断该不该转 —— 只能由人确认后记在 photos.json 每页的
    rotate 字段里，展示与导出都按它转。
    """
    d = int(deg or 0) % 360
    return img if d == 0 else img.rotate(d, expand=True)   # expand：画布跟着宽高互换


def rotated_jpeg(path: str, deg, quality: int = 92) -> bytes:
    """整页转正后输出 JPEG 字节（大图展示用；含 EXIF 摆正）。"""
    im = apply_rotate(load_upright(path), deg)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def thumb_bytes(path: str, width: int = 320, rotate: int = 0) -> bytes:
    """把一张翻拍照片压成小缩略图，返回 JPEG 字节（卡片列表用）。

    为什么要 draft()：翻拍图动辄 4000px 宽，完整解码再缩放要好几百毫秒；draft()
    能让 JPEG 解码器只解 1/2、1/4、1/8 层（DCT 跳采样），缩略图场景快好几倍、
    内存也小。它对 PNG 等格式自动无效，不影响正确性。

    摆正分两步、信息源不同，别合并：先按 EXIF 转正（手机竖拍不能横着显示），
    再按人工确认的 rotate 转 —— 后者是"照片压根没写方向"时的唯一依据。
    """
    im = Image.open(path)
    im.draft("RGB", (width * 2, width * 2))    # 必须在真正 load 之前调用才有效
    im = ImageOps.exif_transpose(im)           # 这一步才触发解码（按 draft 的尺寸）
    im = im.convert("RGB")
    im = apply_rotate(im, rotate)
    im.thumbnail((width, width * 4), Image.LANCZOS)   # 等比缩到宽度 width（长条图不截断）
    buf = io.BytesIO()                         # 内存缓冲区（像打开一个临时文件）
    im.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()
