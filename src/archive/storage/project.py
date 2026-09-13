"""storage.project —— 建项目：把一批照片复制入库、编号、算 md5/phash 并落 project/photos.json。

语义：photos.json 的 seq(1..N) 即卷内顺序，识别引擎(mark/切份)与导出都以此为基。

新手视角（Java 朋友版）：这是“档案输入层”。它把“用户给的一堆翻拍图”变成程序能用的
标准数据集：
  1) 复制照片进项目目录 photos/（别再动用户原图）
  2) 给每页编号 seq=1..N（按排序策略，默认拍摄时间）
  3) 算 md5（内容指纹）和 phash（感知哈希，近似图比对用）
  4) 写 project.json（项目信息）+ photos.json（每页一条记录，ocr 字段先空着等识别填）
之后识别引擎读的，就是这个 photos.json 里的 records。
（旧“角标裁切/四角证据图”随角标识别废弃，建项目已不再裁角。）
"""
from __future__ import annotations

import json
import os
import shutil

from archive.imaging import md5_of_file, load_upright, phash
from archive.storage.photos import list_photos


def create_project(name: str, photos_dir: str, base_dir: str,
                   order: str = "拍摄时间") -> str:
    """创建项目文件夹并把照片复制入库，返回 project.json 路径。

    物理顺序 = 排序策略（默认按拍摄时间EXIF）后的顺序（seq 从 1 开始）。
    """
    proj = os.path.join(base_dir, name)          # 项目根目录 = base/<项目名>
    photos_dir_abs = os.path.abspath(photos_dir)
    proj_abs = os.path.abspath(proj)
    # 防呆：用户不能把“项目目录自己/项目内部”再当输入（会递归复制）——快速失败
    if photos_dir_abs.startswith(proj_abs + os.sep):
        raise ValueError(
            f"所选文件夹「{photos_dir}」在本项目目录内部。\n"
            "请选择存放原始照片的文件夹（项目文件夹是系统自动生成的，"
            "不要选它）。")
    photos_in = os.path.join(proj, "photos")     # 入库照片放这
    os.makedirs(photos_in, exist_ok=True)

    listed = list_photos(photos_dir, order=order)   # 拿排好序的照片清单
    files, warnings = listed["files"], listed["warnings"]
    if not files:                                   # 一张图都没有 → 明确报错
        raise FileNotFoundError(f"{photos_dir} 中没有找到图片")

    records = []
    for seq, src in enumerate(files, start=1):      # enumerate(从1开始) ≈ for i+索引
        stem = f"{seq:04d}"                         # 001.jpg 这种定宽名字，保证字典序=数字序
        dst = os.path.join(photos_in, stem + os.path.splitext(src)[1].lower())
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)                  # 复制入库（同路径则跳过）

        img = load_upright(dst)                     # 按 EXIF 方向摆正成直立 RGB
        # 一条记录（photos.json 里的一行）—— 识别引擎的“输入单元”
        records.append({
            "file": os.path.basename(dst),
            "original_name": os.path.basename(src),
            "path": dst,               # 绝对路径，识别引擎要读原图
            "seq": seq,                # ★ 卷内顺序（一切排序基准）
            "md5": md5_of_file(dst),   # 内容指纹（查重复上传用）
            "phash": phash(img),       # 感知哈希（像素级重复判定用）
            "rotate": 0,               # 人工确认的整页旋转角（0/90/180/270，见 imaging.apply_rotate）
            "ocr": {},                 # 识别结果占位，由建档(mark)填充
            "flags": [],               # 质检/异常标记
        })

    project = {                        # project.json：项目级信息
        "name": name,
        "created_by": "importer",
        "photo_count": len(records),
        "order_strategy": order,
        "import_warnings": warnings,
        "records_file": "photos.json", # 记录存在哪个文件（load_project 靠它找）
    }
    # 两个 json 落盘（ensure_ascii=False：中文原样存；indent=2：好读）
    with open(os.path.join(proj, "project.json"), "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False, indent=2)
    with open(os.path.join(proj, "photos.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return os.path.join(proj, "project.json")


def load_project(proj_json: str) -> tuple[dict, list]:
    """读 project.json 与其指向的 photos.json → (project, records)。

    records 就是喂给识别引擎的那份数据（含 ocr 字段，可能已识别过一部分）。
    """
    with open(proj_json, encoding="utf-8") as f:
        project = json.load(f)
    root = os.path.dirname(proj_json)             # project.json 所在目录
    with open(os.path.join(root, project["records_file"]), encoding="utf-8") as f:
        records = json.load(f)                    # records_file 默认 photos.json
    return project, records


def save_records(proj_json: str, records: list) -> None:
    """把识别后的 records（含每页 ocr.mark）写回 photos.json。

    建档/识别在引擎内存里更新了每页 ocr，如果不落盘，页面/重开/导出再次读
    photos.json 时就只剩空 ocr —— 表现为“识别过却看不了页卡/像没识别过”。
    """
    with open(proj_json, encoding="utf-8") as f:
        project = json.load(f)
    root = os.path.dirname(proj_json)
    records_path = os.path.join(root, project["records_file"])
    with open(records_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
