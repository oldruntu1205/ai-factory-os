"""
hermes/scheduler/service.py —— 定时调度服务（让 AI 员工按时上班）

职责边界（重要）：
- 调度器只做两件事：到点判断 + 投递任务到 TaskQueue。
- 真正的执行由 Worker（scripts/hermes_worker.py 或进程内 worker loop）完成。
  这样"什么时候干"和"怎么干"解耦，调度器重启不会丢正在执行的任务。

已知限制（写清楚，不装作没有）：
- 去重状态在内存里（_fired）。单实例部署没问题；
  多实例同时跑调度器会对同一分钟重复投递，那时需换成 DB 抢锁（SELECT ... FOR UPDATE）
  或只让一个实例开启 SCHEDULER_ENABLED。
"""
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from hermes.scheduler.cron import CronExpression, CronParseError
from hermes.scheduler.jobs import JOBS, get_job

logger = logging.getLogger(__name__)

TICK_SECONDS = 30


class SchedulerService:
    """cron 调度器：按分钟粒度触发 job，投递到 TaskQueue。"""

    def __init__(self, queue, runtime=None, jobs: List[Dict] = None,
                 tick_seconds: int = TICK_SECONDS, enabled: bool = True):
        self.queue = queue
        self.runtime = runtime
        self.jobs = jobs if jobs is not None else JOBS
        self.tick_seconds = max(5, int(tick_seconds or TICK_SECONDS))
        self.enabled = bool(enabled)

        self._compiled: Dict[str, CronExpression] = {}
        self._invalid: Dict[str, str] = {}
        for job in self.jobs:
            try:
                self._compiled[job["name"]] = CronExpression(job["cron"])
            except CronParseError as e:
                self._invalid[job["name"]] = str(e)
                logger.error("调度任务 [%s] cron 非法，已跳过: %s", job["name"], e)

        self._fired: set = set()          # {job_name}:{YYYY-MM-DD HH:MM}
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._last_tick: Optional[float] = None
        self._dispatch_count = 0

    # ---------- 生命周期 ----------

    def start(self) -> Optional[threading.Thread]:
        if not self.enabled:
            logger.info("Scheduler: 已禁用（SCHEDULER_ENABLED=0）")
            return None
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hermes-scheduler")
        self._thread.start()
        logger.info("Scheduler: 已启动，任务数=%d，tick=%ds",
                    len(self._compiled), self.tick_seconds)
        return self._thread

    def stop(self) -> None:
        self._stop_flag.set()
        logger.info("Scheduler: 已停止")

    def _loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.warning("Scheduler tick 异常: %s", e)
            self._stop_flag.wait(self.tick_seconds)

    # ---------- 触发 ----------

    def tick(self, now: Optional[datetime] = None) -> List[dict]:
        """检查一次当前时间是否命中任务；命中则投递。返回本次投递结果列表。"""
        now = now or datetime.now()
        self._last_tick = time.time()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        results = []

        for job in self.jobs:
            name = job["name"]
            cron = self._compiled.get(name)
            if cron is None or not cron.matches(now):
                continue
            if f"{name}:{minute_key}" in self._fired:
                continue
            self._fired.add(f"{name}:{minute_key}")
            results.extend(self._dispatch(job))

        if len(self._fired) > 4096:          # 防无限增长
            self._fired = {k for k in self._fired if k.endswith(minute_key)}

        return results

    def _dispatch(self, job: Dict) -> List[dict]:
        out = []
        for company_id in self._target_companies():
            payload = {"job": job["name"], "company_id": company_id,
                       "agent": job.get("agent", ""), "triggered_by": "scheduler"}
            try:
                res = self.queue.enqueue(job["task"], payload, priority=4)
                self._dispatch_count += 1
                logger.info("Scheduler: 投递 [%s] company=%s task_id=%s",
                            job["name"], company_id, res.get("task_id"))
                out.append({"job": job["name"], "company_id": company_id, **res})
            except Exception as e:
                logger.error("Scheduler: 投递 [%s] company=%s 失败: %s", job["name"], company_id, e)
                out.append({"job": job["name"], "company_id": company_id,
                            "status": "error", "error": str(e)})
        return out

    def _target_companies(self) -> List[int]:
        """需要巡检的企业列表（排除平台级 0）。多租户：一个任务对所有企业各跑一次。"""
        try:
            from database import SessionLocal
            from models import Company
            db = SessionLocal()
            try:
                rows = db.query(Company.id).filter(Company.id > 0).all()
                return [r[0] for r in rows]
            finally:
                db.close()
        except Exception as e:
            logger.warning("Scheduler: 读取企业列表失败: %s", e)
            return []

    def run_now(self, job_name: str, company_id: Optional[int] = None) -> dict:
        """手动触发一次（管理端"立即巡检"按钮 / 排障用）。"""
        job = get_job(job_name)
        if not job:
            return {"ok": False, "error": f"任务 [{job_name}] 不存在"}
        companies = [company_id] if company_id else self._target_companies()
        results = []
        for cid in companies:
            payload = {"job": job_name, "company_id": cid,
                       "agent": job.get("agent", ""), "triggered_by": "manual"}
            try:
                res = self.queue.enqueue(job["task"], payload, priority=2)
                self._dispatch_count += 1
                results.append({"company_id": cid, **res})
            except Exception as e:
                results.append({"company_id": cid, "status": "error", "error": str(e)})
        return {"ok": True, "job": job_name, "dispatched": len(results), "results": results}

    # ---------- 状态 ----------

    def status(self) -> dict:
        now = datetime.now()
        jobs = []
        for job in self.jobs:
            name = job["name"]
            cron = self._compiled.get(name)
            nxt = cron.next_after(now) if cron else None
            jobs.append({
                "name": name,
                "cron": job["cron"],
                "description": job.get("description", ""),
                "agent": job.get("agent", ""),
                "task": job.get("task", ""),
                "valid": cron is not None,
                "error": self._invalid.get(name, ""),
                "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
            })
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "tick_seconds": self.tick_seconds,
            "queue_mode": getattr(self.queue, "_mode", "unknown"),
            "last_tick": self._last_tick,
            "dispatch_count": self._dispatch_count,
            "jobs": jobs,
        }


# ---------- Worker 侧：把调度任务接到 AI 员工 ----------

def register_scheduler_workers(queue, runtime=None) -> int:
    """把 scheduler.* 任务名注册到 TaskQueue，执行体 = runtime.execute(agent, prompt)。

    返回注册成功的任务数。未初始化 runtime 时静默跳过（worker 启动早期可能还没装配）。
    """
    registered = 0
    for job in JOBS:
        task_name = job.get("task")
        if not task_name:
            continue
        queue.register_worker(task_name, _make_handler(job, runtime))
        registered += 1
    return registered


def _make_handler(job: Dict, runtime=None):
    def _handler(payload: dict) -> dict:
        payload = payload or {}
        company_id = int(payload.get("company_id") or 0)
        agent_name = job.get("agent") or payload.get("agent") or ""
        if not company_id:
            return {"status": "skipped", "reason": "missing company_id"}
        if not agent_name:
            return {"status": "skipped", "reason": "missing agent"}

        rt = runtime
        if rt is None:
            try:
                from hermes.runtime import get_runtime
                rt = get_runtime()
            except Exception as e:
                return {"status": "skipped", "reason": f"runtime 未就绪: {e}"}

        try:
            result = rt.execute(
                agent_name=agent_name,
                message=job.get("prompt", ""),
                tenant_id=company_id,
                company_id=company_id,
            )
            return {"status": "done", "job": job["name"],
                    "company_id": company_id, "agent": agent_name,
                    "session_id": result.get("session_id") if isinstance(result, dict) else None}
        except Exception as e:
            logger.error("Scheduler 任务 [%s] 执行失败 company=%s: %s", job["name"], company_id, e)
            return {"status": "failed", "job": job["name"],
                    "company_id": company_id, "error": str(e)}

    return _handler
