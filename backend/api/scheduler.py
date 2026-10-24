"""
api/scheduler.py —— 定时调度管理 API

端点：
- GET  /hermes/scheduler/status              调度器状态 + 各任务下次运行时间
- GET  /hermes/scheduler/jobs                任务清单
- POST /hermes/scheduler/jobs/{name}/run     立即触发一次（管理员）
- POST /hermes/scheduler/reload              重载任务配置（管理员）

说明：调度器只负责"到点投递"，执行由 Worker 完成；因此"立即触发"也只是入队，
任务真正跑完要看 Worker 日志与审计台账。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from auth.deps import get_current_user, require_company
from hermes.scheduler.jobs import get_job
from hermes.scheduler.service import SchedulerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hermes/scheduler", tags=["Hermes Scheduler"])


def _require_admin(current: dict) -> None:
    if current["role"] not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _service() -> SchedulerService:
    """从 runtime 取调度器实例（未启用时给出明确报错，而不是 500）"""
    from hermes.runtime import get_runtime
    svc = getattr(get_runtime(), "scheduler", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="调度器未初始化（HERMES_SCHEDULER_ENABLED=0？）")
    return svc


@router.get("/status")
def status(current: dict = Depends(get_current_user)):
    _require_admin(current)
    return _service().status()


@router.get("/jobs")
def jobs(current: dict = Depends(get_current_user)):
    _require_admin(current)
    return {"jobs": _service().status().get("jobs", [])}


@router.post("/jobs/{name}/run")
def run_job(name: str, company_id: int = None,
            current: dict = Depends(get_current_user)):
    """立即触发一次任务（默认对当前企业执行；platform_admin 不传则对所有企业）"""
    _require_admin(current)
    if not get_job(name):
        raise HTTPException(status_code=404, detail=f"任务 [{name}] 不存在")
    if company_id is None:
        company_id = current["company_id"] or None
    else:
        require_company(company_id, current)
    result = _service().run_now(name, company_id=company_id)
    logger.info("手动触发调度任务 %s by %s -> %s", name, current.get("email"), result.get("dispatched"))
    return result


@router.post("/reload")
def reload_jobs(current: dict = Depends(get_current_user)):
    """重载任务配置（改完 cron 后免重启）"""
    _require_admin(current)
    svc = _service()
    from hermes.scheduler.jobs import JOBS
    from hermes.scheduler.cron import CronExpression, CronParseError
    svc.jobs = JOBS
    svc._compiled.clear()
    svc._invalid.clear()
    for job in JOBS:
        try:
            svc._compiled[job["name"]] = CronExpression(job["cron"])
        except CronParseError as e:
            svc._invalid[job["name"]] = str(e)
    return {"reloaded": len(JOBS), "valid": len(svc._compiled),
            "invalid": svc._invalid}
