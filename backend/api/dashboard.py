"""
api/dashboard.py —— 企业驾驶舱（V2.0 第十章 + V2.1 销售驾驶舱）

- GET /dashboard/overview     经营总览（销售额/订单/效率/良率/AI建议）
- GET /dashboard/sales        销售驾驶舱（发现客户/A级/邮件/回复/商机/金额）
- GET /dashboard/daily-report  CEO 经营日报
- GET /dashboard/bigdata       大数据看板聚合（单一接口返回看板所需全部数据，按 company_id 隔离）
"""
from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import (Customer, Quotation, ProductionOrder, QualityReport,
                    Lead, Conversation, EmailLog)
from auth.deps import get_current_user, require_company
from api.company import require_verified
from agents.ceo import build_daily_report
from api.systems_data import _crm, _mes, _qms, _erp, _integration_status

router = APIRouter(prefix="/dashboard", tags=["驾驶舱"])


@router.get("/overview")
def overview(company_id: int, db: Session = Depends(get_db),
             current: dict = Depends(get_current_user)):
    """经营总览"""
    require_company(company_id, current)

    customers = db.query(Customer).filter(Customer.company_id == company_id).all()
    quotations = db.query(Quotation).filter(Quotation.company_id == company_id).all()
    orders = db.query(ProductionOrder).filter(ProductionOrder.company_id == company_id).all()
    quality = db.query(QualityReport).filter(QualityReport.company_id == company_id).all()
    conversations = db.query(Conversation).filter(Conversation.company_id == company_id).count()

    total_amount = sum(q.total_amount for q in quotations if q.status in ("sent", "accepted"))
    won_count = sum(1 for q in quotations if q.status == "accepted")
    delay_count = sum(1 for o in orders if o.delay)
    avg_defect = round(sum(r.defect_rate for r in quality) / len(quality), 2) if quality else 0
    avg_progress = round(sum(o.progress for o in orders) / len(orders), 1) if orders else 0

    # AI 建议（基于数据规则 + LLM 兜底）
    suggestions = []
    if delay_count > 0:
        suggestions.append({"level": "warning", "text": f"有 {delay_count} 个生产订单存在延期风险，建议核查产线排产"})
    if avg_defect > 3:
        suggestions.append({"level": "danger", "text": f"平均不良率 {avg_defect}% 偏高，建议质量部门排查工艺参数"})
    if total_amount == 0:
        suggestions.append({"level": "info", "text": "暂无有效报价金额，建议 AI 销售员工加大客户开发力度"})
    if not suggestions:
        suggestions.append({"level": "success", "text": "企业经营各项指标正常，AI 建议保持当前策略"})

    return {
        "revenue": round(total_amount, 2),
        "orders": len(quotations),
        "won_orders": won_count,
        "production_efficiency": avg_progress,
        "quality_yield": round(100 - avg_defect, 2) if avg_defect else 100.0,
        "customers": len(customers),
        "conversations": conversations,
        "ai_suggestions": suggestions,
    }


@router.get("/sales")
def sales_dashboard(company_id: int, db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """AI 销售驾驶舱（V2.1 第十章，需企业已认证）"""
    require_company(company_id, current)
    require_verified(current.get("license_verified", False))

    leads = db.query(Lead).filter(Lead.company_id == company_id).all()
    discovered = len(leads)
    grade_a = sum(1 for l in leads if l.grade == "A")
    contacted = sum(1 for l in leads if l.status in ("contacted", "replied"))
    replied = sum(1 for l in leads if l.status == "replied")
    qualified = sum(1 for l in leads if l.status in ("qualified", "won"))
    estimated = qualified * 50000  # 演示估值

    return {
        "discovered_today": discovered,
        "grade_a": grade_a,
        "emails_sent": contacted,
        "replies": replied,
        "opportunities": qualified,
        "estimated_amount": estimated,
        "leads": [{
            "id": l.id, "company_name": l.company_name, "country": l.country,
            "score": l.score, "grade": l.grade, "status": l.status,
        } for l in leads[:20]],
    }


@router.get("/daily-report")
def daily_report(company_id: int, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """CEO 经营日报（V2.0 第九章，需企业已认证）"""
    require_company(company_id, current)
    require_verified(current.get("license_verified", False))
    report = build_daily_report(db, company_id)
    return {"report": report}


@router.get("/bigdata")
def bigdata(company_id: int, db: Session = Depends(get_db),
            current: dict = Depends(get_current_user)):
    """大数据看板聚合（客户后台，按 company_id 隔离，非平台级）

    单一接口返回看板所需的全部真实数据：销售额、订单量、生产排程、
    质检合格率、今日新增客户、AI 建议、销售驾驶舱、邮件日志+状态分布、
    业务系统(CRM/MES/QMS/ERP)指标。CEO 经营日报为 LLM 生成，由前端单独
    调 /dashboard/daily-report 懒加载，避免阻塞看板首屏。
    """
    require_company(company_id, current)

    customers = db.query(Customer).filter(Customer.company_id == company_id).all()
    quotations = db.query(Quotation).filter(Quotation.company_id == company_id).all()
    orders = db.query(ProductionOrder).filter(ProductionOrder.company_id == company_id).all()
    quality = db.query(QualityReport).filter(QualityReport.company_id == company_id).all()
    leads = db.query(Lead).filter(Lead.company_id == company_id).all()
    conversations = db.query(Conversation).filter(Conversation.company_id == company_id).count()

    # 销售额：已发送/已接受报价金额之和
    revenue = round(sum(q.total_amount for q in quotations if q.status in ("sent", "accepted")), 2)
    quotation_count = len(quotations)
    production_order_count = len(orders)

    # 质检合格率
    avg_defect = round(sum(r.defect_rate for r in quality) / len(quality), 2) if quality else 0
    quality_yield = round(100 - avg_defect, 2) if avg_defect else 100.0
    quality_count = len(quality)

    # 生产效率（平均进度）
    avg_progress = round(sum(o.progress for o in orders) / len(orders), 1) if orders else 0
    delay_count = sum(1 for o in orders if o.delay)

    # 今日新增客户（UTC，按日期比较）
    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day, 0, 0, 0)
    end = datetime(now.year, now.month, now.day, 23, 59, 59)
    new_customers_today = db.query(Customer).filter(
        Customer.company_id == company_id,
        Customer.created_at >= start,
        Customer.created_at <= end,
    ).count()

    # AI 建议（规则，无 LLM）
    suggestions = []
    if delay_count > 0:
        suggestions.append({"level": "warning", "text": f"有 {delay_count} 个生产订单存在延期风险，建议核查产线排产"})
    if avg_defect > 3:
        suggestions.append({"level": "danger", "text": f"平均不良率 {avg_defect}% 偏高，建议质量部门排查工艺参数"})
    if revenue == 0:
        suggestions.append({"level": "info", "text": "暂无有效报价金额，建议 AI 销售员工加大客户开发力度"})
    if not suggestions:
        suggestions.append({"level": "success", "text": "企业经营各项指标正常，AI 建议保持当前策略"})

    # 销售驾驶舱
    discovered = len(leads)
    grade_a = sum(1 for l in leads if l.grade == "A")
    contacted = sum(1 for l in leads if l.status in ("contacted", "replied"))
    replied = sum(1 for l in leads if l.status == "replied")
    qualified = sum(1 for l in leads if l.status in ("qualified", "won"))
    estimated = qualified * 50000
    sales = {
        "discovered_today": discovered,
        "grade_a": grade_a,
        "emails_sent": contacted,
        "replies": replied,
        "opportunities": qualified,
        "estimated_amount": estimated,
    }

    # 生产排程列表
    production_orders = [{
        "order_no": o.order_no,
        "product": o.product,
        "quantity": o.quantity,
        "progress": o.progress,
        "line": o.line,
        "load_rate": o.load_rate,
        "delay": bool(o.delay),
        "deadline_days": o.deadline_days,
    } for o in orders]

    # 邮件日志 + 状态分布
    logs = db.query(EmailLog).filter(EmailLog.company_id == company_id) \
        .order_by(EmailLog.created_at.desc()).limit(50).all()
    email_logs = [{
        "id": l.id, "recipient": l.recipient, "subject": l.subject,
        "status": l.status, "created_at": (l.created_at.isoformat() if l.created_at else ""),
    } for l in logs]
    status_rows = db.query(EmailLog.status, func.count()) \
        .filter(EmailLog.company_id == company_id).group_by(EmailLog.status).all()
    email_status = {s: c for s, c in status_rows}

    # 业务系统数据（复用 systems_data 真实指标）
    business_systems = {}
    for st in ("crm", "mes", "qms", "erp"):
        istatus = _integration_status(db, company_id, st)
        ext = istatus == "ok"
        if st == "crm":
            business_systems["crm"] = _crm(db, company_id, istatus, ext)
        elif st == "mes":
            business_systems["mes"] = _mes(db, company_id, istatus, ext)
        elif st == "qms":
            business_systems["qms"] = _qms(db, company_id, istatus, ext)
        else:
            business_systems["erp"] = _erp(istatus, ext)

    return {
        "revenue": revenue,
        "quotation_count": quotation_count,
        "production_order_count": production_order_count,
        "avg_progress": avg_progress,
        "quality_yield": quality_yield,
        "avg_defect": avg_defect,
        "quality_count": quality_count,
        "delay_count": delay_count,
        "customers": len(customers),
        "conversations": conversations,
        "new_customers_today": new_customers_today,
        "ai_suggestions": suggestions,
        "sales": sales,
        "production_orders": production_orders,
        "email_logs": email_logs,
        "email_status": email_status,
        "business_systems": business_systems,
    }
