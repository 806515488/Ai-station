"""存储层可识别的错误；不依赖任务执行器或 HTTP。"""


class DuplicateKey(ValueError):
    """唯一键冲突，由业务服务转换为对应用户提示。"""


class TransactionAborted(RuntimeError):
    """内部事务失败被调用者捕获后，外层仍须回滚，不能悄悄提交部分结果。"""


class LeaseLost(BaseException):
    """失去任务执行权；穿过普通 Exception 兜底，同时执行资源清理。"""
