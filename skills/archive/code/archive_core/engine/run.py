"""引擎 CLI（需 conda env langgragh；在仓库根执行）：
  PYTHONPATH=src python -m station.skills.cli archive <项目目录> [--limit N]

读一个已建档或未建档项目（project.json + 原图），跑 LangGraph → 打印 materials/issues。

新手视角（Java 朋友版）：
  - 这是一个 main() 命令行入口，等价于 Java 里 `public static void main` 那种可执行类。
    不依赖 Web，最适合“先搞清楚引擎到底跑出什么” —— 想断点调试识别链，就跑这个。
  - 用法：先给一个含 project.json+photos.json 的项目目录（建项目见 storage/project.py）。
"""
from __future__ import annotations

import os
import argparse
from collections import Counter

def main(argv=None):
    parser = argparse.ArgumentParser(description="档案识别引擎：读取项目并运行识别链")
    parser.add_argument("project", nargs="?", help="包含 project.json 的项目目录")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 页；0 表示全部")
    args = parser.parse_args(argv)
    if args.project is None:
        parser.print_help()
        return
    proj_dir = os.path.abspath(args.project)
    # 函数内 import：等用到再加载，让“打 --help/参数不对”时不用拉起重型依赖
    from archive_core.storage.project import load_project
    from archive_core.engine.graph import build_graph

    _, records = load_project(os.path.join(proj_dir, "project.json"))  # 读记录（一页一条）
    if args.limit:
        records = records[:args.limit]         # --limit：只跑前 N 张（联调用，省 key）
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
