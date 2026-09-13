"""参数校验(validate_args)/照片上传落盘(uploads)/系统提示词装载 的离线单测。

validate_args 与 uploads 都是纯函数（只依赖 stdlib，不起 FastAPI/不烧 key），
可以直接 import 断言。

对话式改造（09-07）注：manifest 参数表单（args）已随旧 pipeline 前端删除——
agent 缺输入就在对话里问。validate_args 本身保留：它是通用纯函数（可能被
未来技能复用），用内联字段继续锁住它的行为语义。
"""

from station.app.uploads import purge_old, sanitize_name, store_upload
from station.skills.manifest import validate_args
from station.skills.registry import get_registry


# 内联简化字段（只为了测 validate_args 的行为语义，与具体技能无关）。
F_ARC = [
    {"name": "photos_dir", "label": "翻拍照片", "type": "upload", "required": True},
    {"name": "person", "label": "干部姓名", "type": "text"},
]


def test_validate_no_fields_passthrough():
    # 技能没声明 args → 不校验，任何参数放行（向后兼容）
    assert validate_args([], {"whatever": 1}) is None


def test_validate_unknown_rejected():
    # 未知参数 → 拦截并提示可用参数
    msg = validate_args(F_ARC, {"steps": 5, "photos_dir": "x"})
    assert msg is not None
    assert "steps" in msg and "可用参数" in msg


def test_validate_missing_required():
    # photos_dir 是必填 → 空提交被拦，提示用中文 label
    msg = validate_args(F_ARC, {})
    assert msg is not None
    assert "缺少必填参数" in msg and "翻拍照片" in msg


def test_validate_legal_shapes():
    # 只给照片目录 / 目录+姓名 都合法
    assert validate_args(F_ARC, {"photos_dir": "/x"}) is None
    assert validate_args(F_ARC, {"photos_dir": "/x", "person": "张三"}) is None


def test_validate_one_of_group_still_supported():
    # "至少给一个"组是 validate_args 的通用能力
    fields = [{"name": "a"}, {"name": "b"}]
    g = [["a", "b"]]
    assert validate_args(fields, {}, g) is not None          # 全缺 → 拦
    assert validate_args(fields, {"a": 1}, g) is None        # 单给其一 → 放行
    assert validate_args(fields, {"b": 1}, g) is None


def test_validate_int_and_choice():
    fields = [{"name": "steps", "type": "int", "min": 1},
              {"name": "mode", "type": "choice", "options": ["a", "b"]}]
    assert validate_args(fields, {"steps": 0}) is not None       # int 越界
    assert validate_args(fields, {"mode": "c"}) is not None      # choice 乱填
    assert validate_args(fields, {"steps": 2, "mode": "a"}) is None  # 合法


def test_registry_agent_skills_have_tools_and_system():
    # 注册表装载：agent 技能拿到工具；archive 技能还带系统提示词（system.md）
    reg = get_registry()
    arc = reg.get("archive")
    assert arc is not None and arc.type == "agent"
    leaves = {t.leaf() for t in arc.tools}
    assert {"ask_photos", "scan_photos", "recognize", "show_overview",
            "set_category", "export"} <= leaves          # 工具面齐
    assert arc.system and "干部档案整理" in arc.system    # 系统提示词被读进
    wr = reg.get("weekly-report")
    assert wr is not None and wr.tools and not wr.system  # 没写 system.md → 空串


def test_sanitize_name_drops_path_and_bad_chars():
    assert sanitize_name("C:\\fakepath\\001 张.jpg") == "001 张.jpg"   # 只留最后一段
    assert sanitize_name("../evil???.png") == "evil___.png"            # 去路径 + 坏字符
    assert sanitize_name("a<b>c?.png") == "a_b_c_.png"
    assert sanitize_name("") == "photo"                                # 空 → 兜底名


def test_store_upload_and_purge(tmp_path):
    root = str(tmp_path / "uploads")
    d, n = store_upload(root, [("a.jpg", b"aaa"), ("b.png", b"bbb")])
    assert n == 2
    assert sorted(__import__("os").listdir(d)) == ["a.jpg", "b.png"]
    # 清理：整个目录树清空
    purge_old(root)
    assert __import__("os").listdir(root) == []


def test_store_upload_dir_named_by_label(tmp_path):
    # 目录名 = 人名 + 短 uid：数据有归属（archive 没填姓名时用目录名当干部名）
    import os
    d, n = store_upload(str(tmp_path), [("a.jpg", b"1")], "李明")
    assert n == 1
    assert os.path.basename(d).startswith("李明-")
    # 空标签给通用名"照片"，不裸 uid
    d2, _ = store_upload(str(tmp_path), [("a.jpg", b"1")], "")
    assert os.path.basename(d2).startswith("照片-")


def test_store_upload_handles_same_name(tmp_path):
    # 同一批里重名 → 第二个加后缀，不覆盖
    d, n = store_upload(str(tmp_path), [("p.jpg", b"1"), ("p.jpg", b"2")])
    assert n == 2
    names = sorted(__import__("os").listdir(d))
    assert len(names) == 2 and "p(1).jpg" in names
