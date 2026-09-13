"""识别编排（LangGraph）：建档→切份→分批定类→二次合并→产出（已实现，见 graph.py）。

prompt/口径一律经 archive.skill.loader 读取（改识别 = 改 skills/archive，不改代码）。
节点契约见 docs/ARCHITECTURE.md §3 与 graph.py 的 build_graph()。

新手视角（Java 朋友版）：engine 包 = archive 的“识别服务层”。graph.py 定义那条
有方向的流水线，seg/providers/run 分别是 切份算法 / 模型工厂 / CLI 入口。
"""
