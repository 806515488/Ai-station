"""持久化基础设施入口；业务数据访问在 repositories/ 或各技能自己的仓库。"""
from .database import Database, database

__all__ = ["Database", "database"]
