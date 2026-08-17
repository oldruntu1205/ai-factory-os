"""
tasks/celery_app.py —— Celery 任务调度（V2.1 第九章）

架构：FastAPI -> Redis -> Celery -> AI 任务 Worker

- 每日跟进任务 daily_follow
- 客户发现任务 scheduled_discovery
"""
import os

from celery import Celery

from config import REDIS_URL

celery_app = Celery(
    "industrial_ai",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks.followup", "tasks.billing"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 可靠性加固：broker 未就绪重试、邮件任务晚确认（防 worker 崩溃丢任务）、
    # 软/硬超时（防 LLM 任务无限挂起）、结果过期
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_soft_time_limit=int(os.getenv("CELERY_SOFT_TIME_LIMIT", "300")),
    task_time_limit=int(os.getenv("CELERY_TIME_LIMIT", "600")),
    result_expires=86400,
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
        # 对公转账 24h 缓冲过期自动取消升级（每小时扫描，缓冲精度为小时级）
        "expire-grace-bills": {
            "task": "tasks.billing.expire_grace_bills",
            "schedule": 3600.0,
        },
        # 注：旧机制 daily_follow（followup_day 字段、无追踪/退订）已从 beat 移除，
        # 避免与新序列机制双轨并行对同一条线索重复发信；函数保留供兼容调用。
    },
)
