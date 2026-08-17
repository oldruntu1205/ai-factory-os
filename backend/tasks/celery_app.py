"""
tasks/celery_app.py —— Celery 任务调度（V2.1 第九章）

架构：FastAPI -> Redis -> Celery -> AI 任务 Worker

- 每日跟进任务 daily_follow
- 客户发现任务 scheduled_discovery
"""
from celery import Celery

from config import REDIS_URL

celery_app = Celery(
    "industrial_ai",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks.followup"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    beat_schedule={
        "daily-follow-up": {
            "task": "tasks.followup.daily_follow",
            "schedule": 86400.0,  # 每天
        },
        "scheduled-discovery": {
            "task": "tasks.followup.scheduled_discovery",
            "schedule": 86400.0,
        },
    },
)
