"""引擎 CLI（需 conda env langgragh；在仓库根执行）：
  PYTHONPATH=src python -m archive.engine.run <项目目录> [--limit N]

读一个已建档或未建档项目（project.json + 原图），跑 LangGraph → 打印 materials/issues。

新手视角（Java 朋友版）：
  - 这是一个 main() 命令行入口，等价于 Java 里 `public static void main` 那种可执行类。
    不依赖 Web，最适合“先搞清楚引擎到底跑出什么” —— 想断点调试识别链，就跑这个。
  - 用法：先给一个含 project.json+photos.json 的项目目录（建项目见 storage/project.py）。
"""
from __future__ import annotations

import os
import sys
from collections import Counter

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_SRC = os.path.join(_REPO, "src")
for _p in (_SRC, _REPO):                 # 老规矩：把 src 和仓库根加进模块搜索路径
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    # 简易“命令行参数解析”：把不以 -- 开头的当位置参数；专门认 --limit N
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lim = 0
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            try:
                lim = int(sys.argv[sys.argv.index(a) + 1])   # --limit 后面那个值
            except (ValueError, IndexError):                # noqa  # 解析失败就当没传
                pass
    if not args:
        print("用法: python -m archive.engine.run <项目目录> [--limit N]")
        return
    proj_dir = os.path.abspath(args[0])        # 拿用户给的项目目录（绝对路径）
    # 函数内 import：等用到再加载，让“打 --help/参数不对”时不用拉起重型依赖
    from archive.storage.project import load_project
    from archive.engine.graph import build_graph

    _, records = load_project(os.path.join(proj_dir, "project.json"))  # 读记录（一页一条）
    if lim:
        records = records[:lim]                # --limit：只跑前 N 张（联调用，省 key）
    print(f"载入 {len(records)} 页…")
    # 直接 invoke 整条 LangGraph 链（不传 llm 会由节点内部 make_model）
    res = build_graph().invoke({"records": records})
    mats = res.get("materials") or []
    issues = res.get("issues") or []
    C = Counter(m["category"] for m in mats)   # Counter：统计每个分类有多少材料行
    print(f"材料 {len(mats)}；单页 {sum(1 for m in mats if len(m['members']) == 1)}")
    for cat in ("一", "二", "三", "五", "六", "九-1", "九-2", "九-4", "十"):
        if C.get(cat):
            print(f"  {cat} {C[cat]}")
    print("issues:", dict(Counter(i["code"] for i in issues)))   # 各问题代码的数量


if __name__ == "__main__":
    main()                                     # 直接跑本文件 → 进 main()
