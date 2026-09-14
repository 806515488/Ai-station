"""人像上传端点 + 图片归一化（离线；数据目录由 conftest 指到 tmp）。

为什么单独一组：这条通路有两个**静默**的失败方式，单测不钉就没人会发现 ——
  ① 照片被存进 `uploads/` 的话，下一次有人传档案照片就会被 `purge_old` **无条件清空**
     （status.md 记过同一个坑：对账目录就是这么丢的）；
  ② 归一化漏了脱 EXIF，用户的自拍会**连拍摄地点的 GPS 一起**留在服务器上、
     并且顺着参考图发给对端。
两个都不报错、不崩，只是"东西没了"或"隐私漏了"。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from station import config
from station.files import image as image_util
from station.files import store as fs


def _jpeg(size=(1200, 900), color="red", exif=None) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if exif is None:
        im.save(buf, "JPEG")
    else:
        im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _client():
    from fastapi.testclient import TestClient
    from station.app.server import app
    c = TestClient(app)
    return c


def _login(c, name="站长"):
    c.post("/api/auth/register", json={"name": name, "password": "pw"})
    return c


# ── 归一化本身 ───────────────────────────────────────────────────────

def test_normalize_shrinks_big_image():
    """长边超过 1600 的会被等比缩下来（手机原图动辄 3000+）。"""
    out = image_util.normalize_jpeg(_jpeg((3000, 2000)))
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= image_util.MAX_SIDE, f"没缩：{im.size}"
    assert im.format == "JPEG"


def test_normalize_strips_exif_entirely():
    """★ 输出**整块不带 EXIF** —— 这是 GPS 不外流的保证。

    输入先确认真的带 EXIF，否则这条断言是空的（测了个本来就没有的东西）。
    """
    exif = Image.Exif()
    exif[0x010F] = "TestPhone"          # Make
    exif[0x0110] = "TestModel"          # Model
    src = _jpeg((800, 600), exif=exif)
    assert dict(Image.open(io.BytesIO(src)).getexif()), "构造的输入没带上 EXIF，测试无效"

    out = image_util.normalize_jpeg(src)
    assert not dict(Image.open(io.BytesIO(out)).getexif()), "EXIF 没被脱掉（GPS 会跟着出去）"


def test_normalize_rejects_tiny_image():
    """比最短边要求还小的直接拒 —— 交给视频模型也会被它 400，早点给人话。"""
    with pytest.raises(ValueError) as e:
        image_util.normalize_jpeg(_jpeg((100, 100)))
    assert "太小" in str(e.value)


def test_normalize_rejects_broken_bytes():
    """坏字节给**人话**（顺带把 iPhone 的 HEIC 那条线索说出来），不是堆栈。"""
    with pytest.raises(ValueError) as e:
        image_util.normalize_jpeg(b"this is not an image")
    assert "HEIC" in str(e.value)


# ── 端点 ─────────────────────────────────────────────────────────────

def test_portrait_upload_requires_login():
    """没登录 401（这个端点跟 /api/photos 不一样，那个是历史遗留的无鉴权端点）。"""
    c = _client()
    r = c.post("/api/portrait", files={"files": ("a.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 401


def test_portrait_upload_returns_fids_in_order():
    """上传成功返回 fid 列表，顺序 = 提交顺序，且落盘后能取到。"""
    c = _login(_client())
    r = c.post("/api/portrait", files=[
        ("files", ("1.jpg", _jpeg((700, 700), "red"), "image/jpeg")),
        ("files", ("2.jpg", _jpeg((700, 700), "blue"), "image/jpeg")),
    ])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 2 and len(d["files"]) == 2
    for fid in d["files"]:
        assert fs.path(fid) is not None
        assert (fs.meta(fid) or {}).get("suffix") == ".jpg"
    # 两张不同的图必须是不同的 fid（内容寻址）
    assert d["files"][0] != d["files"][1]


def test_portrait_upload_caps_at_five():
    """超过 5 张直接 400（前端也卡，但**别信前端**）。"""
    c = _login(_client())
    files = [("files", (f"{i}.jpg", _jpeg((700, 700), (i * 20, 0, 0)), "image/jpeg"))
             for i in range(6)]
    r = c.post("/api/portrait", files=files)
    assert r.status_code == 400
    assert "5" in r.json()["detail"]


def test_portrait_upload_rejects_small_and_broken_without_saving():
    """坏图/太小的图：400 + 人话，而且**一个字都不落盘**。"""
    c = _login(_client())
    before = len(list(config.sub("files").glob("*.json")))
    for name, data in (("tiny.jpg", _jpeg((80, 80))), ("bad.jpg", b"nope")):
        r = c.post("/api/portrait", files={"files": (name, data, "image/jpeg")})
        assert r.status_code == 400, f"{name} 应该被拒"
        assert "第 1 张" in r.json()["detail"], "要指出是第几张（一次可能传好几张）"
    assert len(list(config.sub("files").glob("*.json"))) == before, "被拒的图不该落盘"


def test_portrait_upload_marks_batch_and_index():
    """★ 落盘的元数据里要带 `portrait_index` 与同一批的 group_key。

    顺序靠它（不能靠 created 时间戳：同一批连着写可能撞同一个值），
    而"当前那一批"靠 group_key 区分。
    """
    c = _login(_client())
    r = c.post("/api/portrait", files=[
        ("files", ("a.jpg", _jpeg((700, 700), "red"), "image/jpeg")),
        ("files", ("b.jpg", _jpeg((700, 700), "blue"), "image/jpeg")),
    ])
    metas = [fs.meta(fid) for fid in r.json()["files"]]
    assert [m.get("portrait_index") for m in metas] == [0, 1]
    assert metas[0]["group_key"] == metas[1]["group_key"], "同一批要共用 group_key"
    assert metas[0]["group_key"].startswith("portrait:")
    assert metas[0]["skill_id"] == "video"


# ── 与 purge_old 的关系（这条是这组测试存在的首要理由）──────────────

def test_portrait_dir_is_not_under_uploads():
    """结构断言：文件区不在 `uploads/` 底下 —— purge_old 够不着它。"""
    files = str(config.sub("files"))
    uploads = str(config.sub("uploads"))
    assert not files.startswith(uploads), "人像落进了会被 purge_old 清掉的树里"


def test_archive_photo_upload_does_not_wipe_portraits():
    """行为断言：传完人像，再来一次档案照片上传，人像必须还在。

    （`POST /api/photos` 每次都会 `purge_old` 掉 `uploads/` 下**每一个**子目录，
    不看新旧。人像要是落在那儿，用户传下一卷档案时就静默没了。）
    """
    c = _login(_client())
    r = c.post("/api/portrait", files={"files": ("me.jpg", _jpeg((700, 700)), "image/jpeg")})
    fid = r.json()["files"][0]

    c.post("/api/photos", files={"files": ("p.jpg", _jpeg((300, 300)), "image/jpeg")},
           data={"label": "测试"})

    assert fs.path(fid) is not None, "传档案照片把人像清掉了"
    assert (fs.meta(fid) or {}).get("owner")
