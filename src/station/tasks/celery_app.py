"""工作站自己的 Celery 应用；Web 只发送编号，Worker 加载同一项目执行真实任务。"""
from celery import Celery, signals
from kombu import Exchange, Queue

from station.tasks import settings


def create_app():
    url = settings.broker_url()
    heavy, remote = settings.queue(True), settings.queue(False)
    dead = remote.rsplit(".", 1)[0] + ".dead"
    queues = tuple(Queue(name, Exchange(name, type="topic", durable=True), routing_key=name,
                         durable=True, queue_arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": dead,
                                                       "x-consumer-timeout": 1800000}) for name in (heavy, remote))
    application = Celery("station", broker=url, include=["station.tasks.worker"])
    application.conf.update(
        task_queues=queues, task_default_queue=remote,
        task_default_queue_type="quorum", task_default_exchange_type="topic",
        task_default_delivery_mode="persistent",
        task_serializer="json", accept_content=["json"],
        task_ignore_result=True, result_backend=None,
        task_acks_late=True, task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=False,
        worker_prefetch_multiplier=1, worker_detect_quorum_queues=True,
        control_queue_durable=True,
        # gossip 也会声明事件接收队列；只配置控制队列还不够。
        # RabbitMQ 4.3+ 拒绝非持久且非独占队列，显式设置以免 Worker 反复重连。
        event_queue_durable=True, event_queue_exclusive=False,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        broker_transport_options={"confirm_publish": True},
        broker_connection_retry_on_startup=True, broker_connection_timeout=5,
        broker_heartbeat=30, worker_max_tasks_per_child=30,
        task_soft_time_limit=1200, task_time_limit=1260,
        worker_send_task_events=True, timezone="Asia/Shanghai",
    )
    application.station_dead_queue = Queue(dead, Exchange(dead, type="topic", durable=True), routing_key="#",
                                          durable=True, queue_arguments={"x-queue-type": "quorum"})
    return application


app = create_app()


@signals.worker_init.connect
def validate_worker_database(**kwargs):
    """独立/多进程 Worker 使用 PG；SQLite 的进程内锁只用于离线测试。"""
    from station import config
    if not settings.broker_url().startswith("memory:") and not config.database_url():
        raise SystemExit("正式 Celery Worker 必须配置共享 PostgreSQL；SQLite 仅用于单进程离线验证")


@signals.worker_process_init.connect
def reset_forked_connection(**kwargs):
    """prefork 子进程不能复用父进程数据库连接。"""
    from station.persistence import database
    database.reset()


@signals.task_failure.connect
def record_unhandled_failure(sender=None, args=None, exception=None, **kwargs):
    """兜住框架层最终失败；WorkerLost 会重投，不在这里提前结束业务。"""
    if getattr(sender, "name", "") != "station.tasks.execute" or not args or len(args) != 2:
        return
    if type(exception).__name__ in ("WorkerLostError", "Retry"):
        return
    from station.persistence.repositories.task_execution import exclusive
    from station.persistence.repositories.jobs import review_unhandled_failure
    from station.persistence.errors import ExecutionLost
    try:
        with exclusive(args[0]) as acquired:
            if acquired:
                review_unhandled_failure(*args)
    except (Exception, ExecutionLost):
        # DB 故障时无法补写；未处理消息进入持久死信队列供核验。
        pass
