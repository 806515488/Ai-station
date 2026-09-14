"""图片归一化 —— 把用户上传的图片收拾成"可以直接用"的 JPEG 字节。

为什么要有这一步（一次解决三件事）：

  ① **体积可控**：手机原图动辄 3000–4600px、2–6MB。喂给识图模型又慢又贵，
     交给视频模型当参考图也超出它的舒适区（Agnes 要求每张 <15MB、单边 256–5760px）。
  ② **EXIF 会被整块脱掉** —— 里面可能带**拍摄地点的 GPS 坐标**。用户传的是自己的
     照片，不该连他家在哪一起送出去。这是隐私上必须有的一步，不能指望对端不管。
  ③ 让"**提取特征用的图**"和"**当参考图交给视频模型的图**"是**同一份字节** ——
     如果两处各自处理，可能出现"文字读出来是短发、图却是另一张裁切"的对不上。

为什么放在 station 而不是 archive/imaging.py：这是**宿主级的文件工具**，跟档案业务
无关；video 技能要能被单独搬走，不能反过来依赖 archive 包。

新手视角（Java 朋友版）：≈ 一个 `ImageUtils.normalizeJpeg(byte[]) → byte[]`。
Pillow 的 `Image.open` ≈ ImageIO.read，`thumbnail` ≈ 等比缩放（不用自己算比例）。
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

MAX_SIDE = 1600      # 缩到长边不超过它。够识图模型看清五官，也够当参考图
MIN_SIDE = 256       # 比这还小就直接拒 —— 视频模型的硬要求（见 skills/video 的注释）
QUALITY = 85


def normalize_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    """任意图片字节 → 长边 ≤ max_side 的 JPEG 字节（不带 EXIF）。

    失败抛 `ValueError`，消息**是给人看的**（会被直接显示在界面上，不是堆栈）。
    """
    if not data:
        raise ValueError("这张图是空的")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()                       # 真正解码一次：坏文件在这一步就炸，别拖到后面
    except Exception as e:              # noqa：Pillow 的异常类型很杂，统一翻成人话
        # ★ 最常见的原因是 iPhone 直传的 **HEIC** —— Pillow 默认不带 heif 解码器。
        #   直接抛 500 的话用户只会看到"服务器错误"，根本猜不到是格式问题。
        raise ValueError(
            "这张图读不出来（可能是 iPhone 的 HEIC 格式，或文件已损坏）。"
            "先转成 JPG 再传一次。") from e

    w, h = im.size
    if min(w, h) < MIN_SIDE:
        raise ValueError(f"这张图太小了（{w}×{h}）—— "
                         f"最短边至少要 {MIN_SIDE} 像素，换一张大点的。")

    # ★ 顺序有讲究：先 exif_transpose（按 EXIF 的 Orientation 把图**转正**），
    #   再 convert("RGB")、再缩。反过来的话旋转会把刚算好的尺寸搞乱。
    #   ★ 转正之后我们**只把像素写出去**，`save()` 不带 exif= 参数 →
    #     **EXIF 整块（含 GPS）都不会被复制**。这就是隐私那一步。
    im = ImageOps.exif_transpose(im) or im
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=QUALITY, optimize=True)
    return buf.getvalue()
