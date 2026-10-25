"""指定 Web 实例定时投递 jobs；不是额外服务，不执行技能业务。"""
import asyncio
import logging
import threading
import time

from station.persistence.repositories import jobs as records
from station.tasks import settings

_local_lock = threading.Lock()
log = logging.getLogger(__name__)


class Unroutable(Exception):
    """消息没有进入指定队列，不能标记 confirmed。"""


def send_message(job):
    """Celery/Kombu 处理发送确认；mandatory 返回也必须检查，不能只看 broker ACK。"""
    from station.tasks.celery_app import app
    app.conf.broker_url = settings.broker_url()
    returned = []
    with app.connection_for_write() as connection:
        connection.ensure_connection(max_retries=0)
        with connection.channel() as channel:
            app.station_dead_queue.bind(channel).declare()
            target = app.amqp.queues[job["queue"]]
            target.bind(channel).declare()
            with app.amqp.Producer(channel, on_return=lambda *a: returned.append(True)) as producer:
                app.send_task("station.tasks.execute", args=[job["id"], job["message_id"]],
                              task_id=job["message_id"], queue=job["queue"], producer=producer,
                              ignore_result=True, retry=False, mandatory=True,
                              timeout=5, confirm_timeout=5,
                              soft_time_limit=1200 if job["queue"] == settings.queue(True) else 110,
                              time_limit=1260 if job["queue"] == settings.queue(True) else 120)
            if returned:
                raise Unroutable("消息未路由到任务队列")


def publish_once(sender=None):
    """只支持一个指定实例启用；本地锁防止定时调用重叠，不冒充分布式锁。"""
    if not _local_lock.acquire(blocking=False):
        return 0
    count = 0
    try:
        maximum = settings.max_publish_attempts()
        queues = (settings.queue(True), settings.queue(False))
        records.review_exhausted_publications(maximum, queues)
        for job in records.pending_messages(max_attempts=maximum, queues=queues):
            if not records.begin_publish(job["id"], job["message_id"], maximum):
                continue
            attempt = job["publish_attempts"] + 1
            try:
                (sender or send_message)(job)
            except Exception as error:
                # 不记录异常正文：第三方连接异常可能包含带口令的 Broker URL。
                state = "failed" if isinstance(error, (Unroutable, ValueError)) else "unknown"
                records.publish_result(job["id"], job["message_id"], state,
                    type(error).__name__ + "：投递未确认，请检查 RabbitMQ 配置或网络",
                    time.time() + min(60, 2 ** attempt), exhausted=attempt >= maximum or state == "failed")
            else:
                records.publish_result(job["id"], job["message_id"], "confirmed")
                count += 1
        return count
    finally:
        _local_lock.release()


async def start_publisher():
    """应用生命周期启动一次；只有指定 Web 设置开关，其他实例不扫描。"""
    if not settings.publisher_enabled():
        return None
    settings.broker_url()  # 配置缺失要在启动时明确报错。
    stop = asyncio.Event()

    async def drive():
        while not stop.is_set():
            try:
                await asyncio.to_thread(publish_once)
            except Exception as error:
                log.warning("任务投递扫描失败：%s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.interval())
            except asyncio.TimeoutError:
                pass

    return stop, asyncio.create_task(drive())


async def stop_publisher(handle):
    if handle:
        stop, task = handle
        stop.set()
        await task
