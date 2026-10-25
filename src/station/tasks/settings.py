"""任务配置：Web 与 Worker 共用，读取环境变量但不连接 MQ。"""
import os
from urllib.parse import urlsplit

from station import config


def broker_url():
    config.load_env()
    value = os.environ.get("STATION_CELERY_BROKER_URL", "").strip()
    if not value:
        raise ValueError("未配置 RabbitMQ：请设置 STATION_CELERY_BROKER_URL 并启动项目 Worker")
    if urlsplit(value).scheme not in ("amqp", "amqps", "memory"):
        raise ValueError("任务 Broker 仅支持 RabbitMQ；memory 仅用于隔离测试")
    return value


def publisher_enabled():
    config.load_env()
    return os.environ.get("STATION_TASK_PUBLISHER_ENABLED", "0") == "1"


def interval():
    return max(1.0, float(os.environ.get("STATION_TASK_PUBLISH_INTERVAL", "3")))


def queue(heavy):
    namespace = os.environ.get("STATION_TASK_NAMESPACE", "station").strip()
    if not namespace or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in namespace):
        raise ValueError("STATION_TASK_NAMESPACE 只能包含字母、数字、横线和下划线")
    return namespace + (".heavy" if heavy else ".remote")


def max_publish_attempts():
    return max(1, int(os.environ.get("STATION_TASK_PUBLISH_ATTEMPTS", "6")))
