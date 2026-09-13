"""station.skills —— 技能注册表（宿主不认识业务，只认 manifest + entry）。

新手视角：skills 包负责“装载/发现技能”——把 skills/<id>/manifest.json 变成宿主
能用的 Skill 对象。真实技能目录在仓库根 skills/（不是这里），这里的 registry
只是扫描并解析它们的“控制器”。
"""
