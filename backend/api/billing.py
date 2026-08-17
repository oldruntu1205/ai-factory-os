"""
api/billing.py —— SaaS 计费系统（落地手册第22章）

- GET  /billing/plans            套餐列表（价格/配额）
- GET  /billing/usage            当前企业用量（对话/Agent/文档/存储）
- GET  /billing/invoices         账单历史
- POST /billing/upgrade          升级套餐（演示版直接生效）
- POST /billing/pay              模拟支付账单
"""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import (Company, AIAgent, Conversation, Document, Quotation,
                    Bill, Customer)
from auth.deps import get_current_user

router = APIRouter(prefix="/billing", tags=["计费系统"])

from constants.plans import PLANS, PLAN_DAYS


def _require_admin_billing(current: dict) -> None:
    """计费操作（升级/支付）仅允许企业管理员/平台超管"""
    if current["role"] not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")


class UpgradeIn(BaseModel):
    company_id: int
    plan: str


@router.get("/plans")
def list_plans(current: dict = Depends(get_current_user)):
    """套餐列表"""
    return {"plans": PLANS}


@router.get("/usage")
def get_usage(company_id: int, db: Session = Depends(get_db),
              current: dict = Depends(get_current_user)):
    """当前企业用量（对话/Agent/文档/客户）"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")

    # 本月对话量（简单按全部统计，生产按月周期）
    conv_count = db.query(Conversation).filter(
        Conversation.company_id == company_id).count()
    agent_count = db.query(AIAgent).filter(
        AIAgent.company_id == company_id).count()
    doc_count = db.query(Document).filter(
        Document.company_id == company_id).count()
    customer_count = db.query(Customer).filter(
        Customer.company_id == company_id).count()
    quote_count = db.query(Quotation).filter(
        Quotation.company_id == company_id).count()

    plan = PLANS.get(c.plan, PLANS["starter"])
    # 套餐时间范围
    started = getattr(c, "plan_started_at", None)
    expires = getattr(c, "plan_expires_at", None)
    remaining_days = None
    expired = False
    if expires:
        from datetime import datetime
        delta = expires - datetime.utcnow()
        remaining_days = delta.days
        expired = remaining_days < 0

    agents_over = agent_count > c.max_agents

    return {
        "plan": c.plan,
        "plan_name": plan["name"],
        "plan_started_at": started.isoformat() if started else None,
        "plan_expires_at": expires.isoformat() if expires else None,
        "remaining_days": remaining_days,
        "expired": expired,
        "conversations": {"used": conv_count, "limit": c.max_conversations},
        "agents": {"used": agent_count, "limit": c.max_agents, "over": agents_over},
        "documents": doc_count,
        "customers": customer_count,
        "quotations": quote_count,
        "percent": round(min(conv_count / c.max_conversations * 100, 100), 1)
            if c.max_conversations else 100,
    }


@router.get("/invoices")
def list_invoices(company_id: int, db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """账单历史"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    bills = db.query(Bill).filter(Bill.company_id == company_id)\
        .order_by(Bill.created_at.desc()).all()
    return {"invoices": [{
        "id": b.id, "bill_no": b.bill_no, "plan": b.plan,
        "amount": b.amount, "currency": b.currency, "status": b.status,
        "paid_at": b.paid_at.isoformat() if b.paid_at else None,
        "created_at": b.created_at.isoformat(),
    } for b in bills]}


@router.post("/upgrade")
def upgrade(data: UpgradeIn, db: Session = Depends(get_db),
            current: dict = Depends(get_current_user)):
    """升级套餐（仅管理员）：生成待支付账单，支付成功后生效（修复：不再未支付即生效）"""
    _require_admin_billing(current)
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if data.plan not in PLANS:
        raise HTTPException(status_code=400, detail="套餐不存在")

    c = db.query(Company).filter(Company.id == data.company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")

    plan = PLANS[data.plan]
    # 存在未支付账单时拒绝重复下单，引导先完成支付
    pending = db.query(Bill).filter(
        Bill.company_id == data.company_id, Bill.status == "unpaid").first()
    if pending:
        raise HTTPException(status_code=400,
                            detail=f"存在待支付账单 {pending.bill_no}，请先完成支付")

    # 生成待支付账单（套餐暂不变更）
    bill = Bill(
        bill_no=f"BILL{uuid.uuid4().hex[:10].upper()}",
        company_id=data.company_id,
        plan=data.plan,
        amount=plan["price"],
        currency=plan["currency"],
        status="unpaid",
        period_start=datetime.utcnow(),
        period_end=datetime.utcnow() + timedelta(days=PLAN_DAYS.get(data.plan, 365)),
    )
    db.add(bill)
    db.commit()

    # 审计日志
    try:
        from utils.audit import log_action
        log_action(data.company_id, "billing.upgrade",
                   f"生成 {data.plan} 套餐账单 {bill.bill_no}（待支付）",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass

    return {"message": f"已生成 {data.plan} 套餐账单，请完成支付后生效",
            "bill_no": bill.bill_no, "amount": bill.amount,
            "currency": bill.currency, "status": "unpaid"}


@router.post("/pay")
def pay(bill_no: str, db: Session = Depends(get_db),
        current: dict = Depends(get_current_user)):
    """模拟支付账单：支付成功后应用套餐（配额与到期时间增量叠加）"""
    _require_admin_billing(current)
    bill = db.query(Bill).filter(Bill.bill_no == bill_no).first()
    if not bill or bill.company_id != current["company_id"]:
        raise HTTPException(status_code=404, detail="账单不存在")
    if bill.status == "paid":
        raise HTTPException(status_code=400, detail="账单已支付，请勿重复操作")
    bill.status = "paid"
    bill.paid_at = datetime.utcnow()

    # 应用套餐：配额 + 到期时间按增量叠加（max(expires, now) + plan_days，防无限续期）
    applied_plan = None
    c = db.query(Company).filter(Company.id == bill.company_id).first()
    if c and bill.plan in PLANS:
        plan = PLANS[bill.plan]
        c.plan = bill.plan
        c.max_agents = plan["max_agents"]
        c.max_conversations = plan["max_conversations"]
        plan_days = PLAN_DAYS.get(bill.plan, 365)
        base = c.plan_expires_at if (c.plan_expires_at and c.plan_expires_at > datetime.utcnow()) \
            else datetime.utcnow()
        if not c.plan_started_at:
            c.plan_started_at = datetime.utcnow()
        c.plan_expires_at = base + timedelta(days=plan_days)
        applied_plan = bill.plan
    db.commit()

    # 审计日志
    try:
        from utils.audit import log_action
        log_action(bill.company_id, "billing.pay",
                   f"支付账单 {bill.bill_no}（{bill.plan}）并生效",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass

    return {"message": "支付成功，套餐已生效", "bill_no": bill.bill_no,
            "plan": applied_plan or bill.plan, "status": bill.status}
