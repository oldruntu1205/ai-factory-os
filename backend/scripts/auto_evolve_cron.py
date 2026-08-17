"""
scripts/auto_evolve_cron.py —— 自进化周期任务（供系统 crontab / 容器 schedule 调用）

一次性跑完所有 active 企业的 auto_evolve_cycle 后退出（EXIT 0）。
仅产出 pending 候选，需人工 approve 才写回运行态（Human-in-the-loop）。
单企业失败不影响整体（记录日志后继续）。

用法：
    python scripts/auto_evolve_cron.py
    python scripts/auto_evolve_cron.py --lookback-days 14

crontab 示例（每日 03:30 跑一轮）：
    30 3 * * * cd /path/to/backend && /usr/bin/python scripts/auto_evolve_cron.py >> logs/auto_evolve.log 2>&1

也可与后台守护线程二选一：若用本脚本，可在 .env 设 EVOLUTION_SCHEDULER_ENABLED=0 关闭进程内调度。
"""
import logging
import os
import sys
import time

# 允许以脚本方式直接运行（python scripts/auto_evolve_cron.py）：
# 把 backend/ 加入 sys.path，使 config / database / hermes 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("auto_evolve_cron")


def _parse_args(argv):
    lookback = None
    if "--lookback-days" in argv:
        i = argv.index("--lookback-days")
        if i + 1 < len(argv):
            lookback = int(argv[i + 1])
    return lookback


def main():
    from config import (EVOLUTION_SCHEDULER_LOOKBACK_DAYS,
                        EVOLUTION_SCHEDULER_TARGET_TYPES)
    from database import SessionLocal
    from models import Company
    from hermes.evolution import service

    lookback = _parse_args(sys.argv[1:]) or EVOLUTION_SCHEDULER_LOOKBACK_DAYS
    target_types = EVOLUTION_SCHEDULER_TARGET_TYPES

    db = SessionLocal()
    try:
        now = time.time()
        rows = db.query(Company).all()
        company_ids = []
        for c in rows:
            exp = c.plan_expires_at
            if exp is None or getattr(exp, "timestamp", lambda: 0)() >= now:
                company_ids.append(c.id)
    finally:
        db.close()

    if not company_ids:
        logger.info("无 active 企业，跳过")
        return 0

    total = 0
    for cid in company_ids:
        try:
            res = service.auto_evolve_cycle(
                cid, lookback_days=lookback, target_types=target_types)
            n = len(res.get("candidates_created", []))
            total += n
            logger.info("企业 %s: 扫描 %s agent, 生成 %s 候选",
                        cid, res.get("agents_scanned"), n)
        except Exception as e:
            logger.warning("企业 %s 进化失败: %s", cid, e)
    logger.info("自进化周期任务完成：企业 %d, 候选 %d", len(company_ids), total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
