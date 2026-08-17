"""
hermes/evolution/scheduler.py —— auto_evolve_cycle 定时调度器（Phase 5 落地）

后台守护线程，按可配置间隔遍历所有 active 企业，调用 service.auto_evolve_cycle
生成 pending 候选（Human-in-the-loop，绝不自动 apply）。

设计要点：
- 仅在 main.py lifespan 显式 start() 时运行；测试/脚本 import 不会自动启动。
- start()/stop()/status() 控制；单实例运行保护（_running 标志），避免间隔过短导致重叠。
- 分段 sleep（30s 一拍），stop() 可及时响应。
- 默认只进化 soul + skill；tool / tool_code 仍走人工 /approve 流程（代码进化只产提案文件）。
- 任何一轮异常都被捕获并记录，不影响主服务与其它企业。
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)


def _active_company_ids():
    """返回需要进化的企业 id 列表。

    active 判定：plan_expires_at 为 NULL（永久/试用）或未过期。
    （Company 无显式 status 列，用到期时间兜底；无轨迹的企业在
     auto_evolve_cycle 内部会因轨迹 < 3 被自然跳过。）
    """
    from database import SessionLocal
    from models import Company
    db = SessionLocal()
    try:
        now = time.time()
        rows = db.query(Company).all()
        ids = []
        for c in rows:
            exp = c.plan_expires_at
            if exp is None:
                ids.append(c.id)
            else:
                exp_ts = getattr(exp, "timestamp", lambda: 0)()
                if exp_ts >= now:
                    ids.append(c.id)
        return ids
    finally:
        db.close()


class EvolutionScheduler:
    def __init__(self, interval=None, start_delay=None,
                 lookback_days=None, target_types=None):
        from config import (EVOLUTION_SCHEDULER_INTERVAL,
                            EVOLUTION_SCHEDULER_START_DELAY,
                            EVOLUTION_SCHEDULER_LOOKBACK_DAYS,
                            EVOLUTION_SCHEDULER_TARGET_TYPES)
        self.interval = int(interval or EVOLUTION_SCHEDULER_INTERVAL)
        self.start_delay = int(start_delay or EVOLUTION_SCHEDULER_START_DELAY)
        self.lookback_days = int(lookback_days or EVOLUTION_SCHEDULER_LOOKBACK_DAYS)
        self.target_types = tuple(target_types or EVOLUTION_SCHEDULER_TARGET_TYPES)
        self._thread = None
        self._stop = threading.Event()
        self._running = False           # 是否正在执行一轮
        self._lock = threading.Lock()
        self.last_run_at = None
        self.last_summary = None
        self.total_cycles = 0
        self.total_candidates = 0

    # ---------- 执行一轮 ----------
    def run_once(self):
        """遍历所有 active 企业跑 auto_evolve_cycle，返回本轮汇总。"""
        with self._lock:
            if self._running:
                logger.info("[scheduler] 上一轮仍在运行，跳过本轮")
                return {"skipped": True, "reason": "already_running"}
            self._running = True
        try:
            from hermes.evolution import service
            company_ids = _active_company_ids()
            per_company = []
            total_created = 0
            for cid in company_ids:
                try:
                    res = service.auto_evolve_cycle(
                        cid, lookback_days=self.lookback_days,
                        target_types=self.target_types)
                    n = len(res.get("candidates_created", []))
                    total_created += n
                    per_company.append({
                        "company_id": cid,
                        "agents_scanned": res.get("agents_scanned"),
                        "candidates_created": n,
                    })
                except Exception as e:
                    logger.warning("[scheduler] 企业 %s 进化循环异常: %s", cid, e)
                    per_company.append({"company_id": cid, "error": str(e)})
            self.total_cycles += 1
            self.total_candidates += total_created
            self.last_run_at = time.time()
            self.last_summary = {
                "companies": len(company_ids),
                "candidates_created": total_created,
                "per_company": per_company,
            }
            logger.info("[scheduler] 周期自进化完成: 企业=%d 候选=%d",
                        len(company_ids), total_created)
            return self.last_summary
        finally:
            self._running = False

    # ---------- 守护线程主循环 ----------
    def _loop(self):
        if self.start_delay > 0:
            self._stop.wait(self.start_delay)
        while not self._stop.is_set():
            if not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as e:  # 兜底，避免线程意外退出
                    logger.exception("[scheduler] 运行异常: %s", e)
            # 分段 sleep（每 30s 一拍），便于及时响应 stop
            waited = 0
            while waited < self.interval and not self._stop.wait(min(30, self.interval - waited)):
                waited += 30

    def start(self):
        from config import EVOLUTION_SCHEDULER_ENABLED
        if not EVOLUTION_SCHEDULER_ENABLED:
            logger.info("[scheduler] 已通过配置关闭（EVOLUTION_SCHEDULER_ENABLED=0），不启动")
            return False
        if self._thread and self._thread.is_alive():
            logger.info("[scheduler] 已在运行")
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="evolution-scheduler", daemon=True)
        self._thread.start()
        logger.info("[scheduler] 启动: 间隔=%ds 延迟=%ds 回溯=%d天 目标=%s",
                    self.interval, self.start_delay, self.lookback_days,
                    self.target_types)
        return True

    def stop(self):
        self._stop.set()
        logger.info("[scheduler] 已请求停止")

    def status(self):
        return {
            "enabled": True,
            "running_thread": bool(self._thread and self._thread.is_alive()),
            "busy": self._running,
            "interval_seconds": self.interval,
            "start_delay_seconds": self.start_delay,
            "lookback_days": self.lookback_days,
            "target_types": list(self.target_types),
            "last_run_at": self.last_run_at,
            "total_cycles": self.total_cycles,
            "total_candidates": self.total_candidates,
            "last_summary": self.last_summary,
        }


# ---------- 全局单例 ----------
_scheduler = None
_scheduler_lock = threading.Lock()


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = EvolutionScheduler()
    return _scheduler


def start_scheduler():
    return get_scheduler().start()


def stop_scheduler():
    if _scheduler:
        _scheduler.stop()


def scheduler_status():
    if _scheduler is None:
        from config import EVOLUTION_SCHEDULER_ENABLED
        return {"enabled": bool(EVOLUTION_SCHEDULER_ENABLED),
                "running_thread": False, "started": False}
    return _scheduler.status()
