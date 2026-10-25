"""存储层可识别的错误；不依赖任务执行器或 HTTP。"""


class DuplicateKey(ValueError):
    """唯一键冲突，由业务服务转换为对应用户提示。"""


class TransactionAborted(RuntimeError):
    """内部事务失败被调用者捕获后，外层仍须回滚，不能悄悄提交部分结果。"""


class ThreadConflict(RuntimeError):
    """会话在读取后被其他请求修改；拒绝旧快照覆盖新状态，不自动重放工具。"""


class ExecutionLost(BaseException):
    """执行消息或持锁数据库连接已失效，不允许旧执行现场继续写回。"""
