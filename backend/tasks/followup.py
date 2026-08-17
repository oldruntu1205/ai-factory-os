"""
tasks/followup.py —— 销售跟进与发现任务（V2.1 第八章 + 第九章）

跟进规则：
Day 1  发送介绍邮件
Day 3  发送案例
Day 7  发送报价
Day 14 最后跟进
"""
import logging

from tasks.celery_app import celery_app
from models import Lead
from database import SessionLocal
from channels.email.generator import create_email
from channels.email.smtp import send_email

logger = logging.getLogger(__name__)

FOLLOWUP_SUBJECTS = {
    1: "Business Cooperation - Premium Laboratory Glassware",
    3: "Case Study: How We Help European Labs Save 30%",
    7: "Special Quotation for Your Laboratory",
    14: "Final Opportunity - Partnership",
}


def _get_pending_leads(db, day: int):
    return db.query(Lead).filter(
        Lead.followup_day == day,
        Lead.status.in_(["discovered", "contacted"]),
    ).all()


@celery_app.task
def daily_follow():
    """每日跟进任务：按跟进节奏给线索发邮件"""
    db = SessionLocal()
    sent = 0
    try:
        for day in (1, 3, 7, 14):
            leads = _get_pending_leads(db, day)
            for lead in leads:
                if not lead.email:
                    continue
                content = create_email(
                    company=f"{lead.company_name} ({lead.country})",
                    product="laboratory glassware",
                    extra=f"跟进阶段: Day {day}",
                )
                result = send_email(lead.email, content, FOLLOWUP_SUBJECTS[day])
                if result["status"] in ("sent", "simulated"):
                    lead.followup_day = min(day + 1, 14)
                    sent += 1
        db.commit()
        logger.info(f"daily_follow: {sent} 封跟进邮件")
        return {"sent": sent}
    finally:
        db.close()


@celery_app.task
def scheduled_discovery(company_id: int = None, limit: int = 20):
    """定时客户发现任务"""
    from sales.discovery import discover_leads
    companies = [company_id] if company_id else _all_company_ids(db=None)
    for cid in companies:
        try:
            leads = discover_leads(cid, limit=limit)
            logger.info(f"company {cid}: 发现 {len(leads)} 个新客户")
        except Exception as e:
            logger.error(f"company {cid} 发现失败: {e}")
    return {"done": True}


@celery_app.task
def email_follow_up_sequence():
    """邮件自动化：扫描到期线索发送下一封跟进（D0/D3/D7，自动带报价 PDF）"""
    from channels.email.sequence import run_due_follow_ups
    result = run_due_follow_ups()
    logger.info(f"email_follow_up_sequence: {result['count']} 封跟进")
    return result


def _all_company_ids(db):
    from models import Company
    session = SessionLocal()
    try:
        return [c.id for c in session.query(Company).all()]
    finally:
        session.close()
