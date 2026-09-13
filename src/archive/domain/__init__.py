"""领域：分类体系 + 实体类型。分类常量在 classes.py；已从 legacy 拆出。

新手视角（Java 朋友版）：本包 = 档案业务的“领域模型/枚举区”。一进来就 import
两个子模块再导出去，别处写 `from archive.domain import classes, models` 即可。
"""
from archive.domain import classes, models  # noqa F401   # noqa：告诉 lint 这行是故意这么写
