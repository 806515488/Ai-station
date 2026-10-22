"""技能仓库接入点：宿主经已登记技能入口访问其数据能力，不导入业务实现包。"""


def repository(skill_id: str):
    from station.skills.registry import get_registry
    entry = get_registry().entry_module(skill_id)
    factory = getattr(entry, "repository", None)
    if not callable(factory):
        raise LookupError(f"技能没有数据仓库入口：{skill_id}")
    return factory()
