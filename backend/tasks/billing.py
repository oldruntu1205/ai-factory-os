"""
tasks/billing.py —— 计费定时任务

- expire_grace_bills: 扫描「待财务审核」账单，超过 24h 缓冲期仍未获审核通过 →
  自动取消升级套餐（回退到升级前快照），状态置 expired。
  逻辑闭环：客户提交凭证 → 套餐临时生效（24h 缓冲）→
  平台财务核对到账审核通过 = 正式生效；24h 未审核 = 自动取消。
"""
import logging
from datetime import datetime

from tasks.celery_app import celery_app
from models import Bill, Company
from database import SessionLocal
from api.billing import _revert_plan, PAY_STATUS_LABELS

logger = logging.getLogger(__name__)


@celery_app.task
def expire_grace_bills():
    """每小时扫描：缓冲期过期且未审核的账单 → 自动取消升级（回退套餐）"""
    from sqlalchemy import update as sa_update
    db = SessionLocal()
    expired = 0
    try:
        now = datetime.utcnow()
        bills = db.query(Bill).filter(
            Bill.status == "pending_review").all()
        for bill in bills:
            if not bill.grace_expires_at or bill.grace_expires_at > now:
                continue
            # 并发抢占（修复：与财务 approve/reject 竞态双向资损）。
            # 条件 UPDATE：仅当仍为 pending_review 且缓冲确实过期才生效；
            # 已被财务审核（paid/rejected）的单 rowcount=0，跳过，不覆盖审核结果。
            res = db.execute(
                sa_update(Bill)
                .where(Bill.bill_no == bill.bill_no,
                       Bill.status == "pending_review",
                       Bill.grace_expires_at <= now)
                .values(status="expired",
                        review_note=(bill.review_note or "") +
                        f" | 缓冲期({bill.grace_expires_at})内未获审核，已自动取消升级"))
            if res.rowcount != 1:
                continue  # 已被审核/状态已变更，跳过
            # 回退套餐（抢占成功后，同事务）
            c = db.query(Company).filter(Company.id == bill.company_id).first()
            if c:
                _revert_plan(c, bill)
            expired += 1
            try:
                from utils.audit import log_action
                log_action(bill.company_id, "billing.grace_expire",
                           f"缓冲过期自动取消升级 {bill.bill_no}（{bill.plan} → {c.plan if c else bill.prev_plan}）",
                           0, "system")
            except Exception:
                pass
            logger.info(f"grace expire: {bill.bill_no} 取消升级")
        db.commit()
        logger.info(f"expire_grace_bills: {expired} 笔缓冲过期")
        return {"expired": expired}
    finally:
        db.close()
