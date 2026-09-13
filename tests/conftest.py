"""测试夹具（conftest）—— 给所有测试“搭台子”的公共设置。

新手视角（Java 朋友版）：pytest 会在跑测试前自动找 conftest.py 并执行这里定义的
fixture（≈ 测试的 @BeforeEach 初始化）。这里干两件事：
  1) 把仓库 src/ 放进 sys.path —— 不然测试里 `import station` 找不到包（跟运行服务同理）。
  2) 提供一个【autouse 自动生效】的 fixture：把“宿主的数据目录”临时改到 pytest 的
     tmp 目录，并关掉危险操作自动放行 —— 保证测试不往你真项目 data/ 写垃圾、也不
     依赖你机器上的环境变量。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# __file__ = .../tests/conftest.py → parents[1] 就是仓库根；src 在仓库根下
_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:       # 防重复：同一个目录别塞两次
    sys.path.insert(0, str(_SRC))   # 把 src 加进“模块搜索路径”


@pytest.fixture(autouse=True)       # autouse：每个测试自动注入，不用在测试里声明
def _tmp_data(monkeypatch, tmp_path):
    """把宿主的数据目录/SQLite 指到临时目录；AUTO_APPROVE 固定为 False。

    monkeypatch.setattr(对象, "属性名", 新值)：临时改掉 config 模块里的全局，
    测试结束后 pytest 自动还原 —— 不影响别的测试/真机运行。
    """
    from station import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")  # 数据全进临时目录
    monkeypatch.setattr(config, "AUTO_APPROVE", False)          # 别依赖环境变量
    # SQLite 也指到临时目录（DB_PATH 是模块级常量、连接是单例 → 都要重置）
    import station.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "station.db")
    monkeypatch.setattr(db, "_conn", None)      # 强制下个 conn() 在新路径重开连接
    yield                                       # 测试体执行…
    monkeypatch.setattr(db, "_conn", None)      # 还原后强制重开（下个测试连新库）
    return tmp_path     # 把临时目录还给测试用（需要的话）
