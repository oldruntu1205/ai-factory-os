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
        # 邮件跟进序列（新机制，含追踪像素/退订，D0/D3/D7/D14）
        "email-follow-up-sequence": {
            "task": "tasks.followup.email_follow_up_sequence",
            "schedule": 86400.0,  # 每天
        },
        "scheduled-discovery": {
            "task": "tasks.followup.scheduled_discovery",
            "schedule": 86400.0,
        },
        # 注：旧机制 daily_follow（followup_day 字段、无追踪/退订）已从 beat 移除，
        # 避免与新序列机制双轨并行对同一条线索重复发信；函数保留供兼容调用。
    },
)
