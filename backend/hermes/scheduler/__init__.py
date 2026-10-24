"""
hermes/scheduler —— AI 员工定时调度（cron）

对外接口：
    from hermes.scheduler.service import SchedulerService, register_scheduler_workers
    from hermes.scheduler.jobs import JOBS, get_job
    from hermes.scheduler.cron import CronExpression

为什么不引 APScheduler：见 cron.py 头部说明（内网部署依赖最小化）。
"""
from hermes.scheduler.jobs import JOBS, get_job
from hermes.scheduler.service import SchedulerService, register_scheduler_workers

__all__ = ["JOBS", "get_job", "SchedulerService", "register_scheduler_workers"]
