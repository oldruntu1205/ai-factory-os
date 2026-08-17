"""
api/billing.py —— SaaS 计费系统（落地手册第22章）

- GET  /billing/plans            套餐列表（价格/配额）
- GET  /billing/usage            当前企业用量（对话/Agent/文档/存储）
- GET  /billing/invoices         账单历史（含开票状态）
- POST /billing/upgrade          升级套餐：生成待支付账单（对公转账）
- GET  /billing/pay-account      对公收款账户信息（客户付款前展示）
- POST /billing/pay              提交转账凭证（multipart：bill_no+voucher+note）
                                 进入 pending_review，套餐立即临时生效（24h 缓冲）
- GET  /billing/invoice-info     获取/保存开票信息
- POST /billing/invoice-request  对已支付账单提交开票申请（类别：服务费）
- GET  /billing/invoice-requests 当前企业开票申请列表
"""
import os
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import (Company, AIAgent, Conversation, Document, Quotation,
                    Bill, Customer, InvoiceRequest)
from auth.deps import get_current_user

router = APIRouter(prefix="/billing", tags=["计费系统"])

from constants.plans import PLANS, PLAN_DAYS

# 开票类别：本平台 SaaS 订阅服务，统一按「服务费」开具
INVOICE_CATEGORY = "服务费"

# 付款方式：对公转账
PAY_METHOD = "bank_transfer"
GRACE_HOURS = 24   # 提交凭证后套餐临时生效的缓冲小时数（平台审核窗口）

# 对公收款账户（演示数据，生产替换为真实账户并建议移到平台配置表）
PAY_ACCOUNT = {
    "account_name": "AI Factory 科技（宁波）有限公司",
    "bank_name": "中国工商银行宁波海曙支行",
    "account_no": "3901 1122 3344 5566 778",
    "payee": "AI Factory 科技（宁波）有限公司",
    "note": "转账时请备注账单号，便于财务核对到账",
}

# 转账凭证保存目录（与官网图片同挂载：/static/site/...）
VOUCHER_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads", "site", "payments")
os.makedirs(VOUCHER_DIR, exist_ok=True)

# 支付状态 → 中文标签
PAY_STATUS_LABELS = {
    "unpaid": "待支付",
    "pending_review": "待财务审核",
    "paid": "已支付",
    "rejected": "审核驳回",
    "expired": "缓冲过期",
}


class InvoiceInfoIn(BaseModel):
    invoice_title: str = ""
    invoice_tax_no: str = ""
    invoice_bank: str = ""
    invoice_bank_account: str = ""
    invoice_address: str = ""
    invoice_phone: str = ""
    invoice_type: str = "normal"      # normal=电子普票 special=电子专票


class InvoiceRequestIn(BaseModel):
    bill_no: str


def _require_admin_billing(current: dict) -> None:
    """计费操作（升级/支付）仅允许企业管理员/平台超管"""
    if current["role"] not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")


class UpgradeIn(BaseModel):
    company_id: int
    plan: str


def _apply_plan(c: Company, plan_key: str) -> None:
    """应用套餐：配额 + 到期时间增量叠加（max(expires, now) + plan_days，防无限续期）"""
    plan = PLANS[plan_key]
    c.plan = plan_key
    c.max_agents = plan["max_agents"]
    c.max_conversations = plan["max_conversations"]
    plan_days = PLAN_DAYS.get(plan_key, 365)
    base = c.plan_expires_at if (c.plan_expires_at and c.plan_expires_at > datetime.utcnow()) \
        else datetime.utcnow()
    if not c.plan_started_at:
        c.plan_started_at = datetime.utcnow()
    c.plan_expires_at = base + timedelta(days=plan_days)


def _revert_plan(c: Company, bill: Bill) -> None:
    """回退套餐到升级前快照（审核驳回 / 24h 缓冲过期时）"""
    snap = bill.prev_snapshot or {}
    prev_plan = snap.get("plan") or bill.prev_plan or "starter"
    plan = PLANS.get(prev_plan, PLANS["starter"])
    c.plan = prev_plan
    c.max_agents = snap.get("max_agents", plan["max_agents"])
    c.max_conversations = snap.get("max_conversations", plan["max_conversations"])
    if snap.get("plan_expires_at"):
        try:
            c.plan_expires_at = datetime.fromisoformat(str(snap["plan_expires_at"]))
        except Exception:
            c.plan_expires_at = None
    else:
        c.plan_expires_at = None
    if snap.get("plan_started_at"):
        try:
            c.plan_started_at = datetime.fromisoformat(str(snap["plan_started_at"]))
        except Exception:
            pass


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
    """账单历史（含开票状态：未申请/待开票/已开票/已驳回）"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    bills = db.query(Bill).filter(Bill.company_id == company_id)\
        .order_by(Bill.created_at.desc()).all()
    # 关联开票申请：一账单最多一条申请
    reqs = {r.bill_no: r for r in db.query(InvoiceRequest).filter(
        InvoiceRequest.company_id == company_id).all()}
    result = []
    for b in bills:
        req = reqs.get(b.bill_no)
        if req:
            inv_status = {"none": "未申请", "pending": "待开票",
                          "issued": "已开票", "rejected": "已驳回"}.get(
                              req.status, req.status)
            inv_detail = {
                "applied": True,
                "status": req.status,          # pending/issued/rejected
                "status_label": inv_status,
                "req_no": req.req_no,
                "invoice_no": req.invoice_no,
                "reject_reason": req.reject_reason,
            }
        else:
            inv_detail = {"applied": False, "status": "none",
                          "status_label": "未申请", "req_no": "",
                          "invoice_no": "", "reject_reason": ""}
        result.append({
            "id": b.id, "bill_no": b.bill_no, "plan": b.plan,
            "amount": b.amount, "currency": b.currency, "status": b.status,
            "status_label": PAY_STATUS_LABELS.get(b.status, b.status),
            "paid_at": b.paid_at.isoformat() if b.paid_at else None,
            "created_at": b.created_at.isoformat(),
            "voucher_path": b.voucher_path or "",
            "voucher_note": b.voucher_note or "",
            "submitted_at": b.submitted_at.isoformat() if b.submitted_at else None,
            "grace_expires_at": b.grace_expires_at.isoformat() if b.grace_expires_at else None,
            "review_note": b.review_note or "",
            "invoice": inv_detail,
        })
    return {"invoices": result}


@router.post("/upgrade")
def upgrade(data: UpgradeIn, db: Session = Depends(get_db),
            current: dict = Depends(get_current_user)):
    """升级套餐（仅管理员）：生成待支付账单（对公转账），提交凭证后临时生效"""
    _require_admin_billing(current)
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if data.plan not in PLANS:
        raise HTTPException(status_code=400, detail="套餐不存在")

    c = db.query(Company).filter(Company.id == data.company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")

    plan = PLANS[data.plan]
    # 存在未支付/待审核账单时拒绝重复下单，引导先完成支付
    pending = db.query(Bill).filter(
        Bill.company_id == data.company_id,
        Bill.status.in_(["unpaid", "pending_review"])).first()
    if pending:
        raise HTTPException(status_code=400,
                            detail=f"存在处理中的账单 {pending.bill_no}（{PAY_STATUS_LABELS.get(pending.status)}），请先完成该笔")

    # 记录升级前套餐快照（审核驳回 / 缓冲过期时回退）
    prev_snapshot = {
        "plan": c.plan,
        "max_agents": c.max_agents,
        "max_conversations": c.max_conversations,
        "plan_started_at": c.plan_started_at.isoformat() if c.plan_started_at else None,
        "plan_expires_at": c.plan_expires_at.isoformat() if c.plan_expires_at else None,
    }

    # 生成待支付账单（套餐暂不变更，提交凭证后临时生效）
    bill = Bill(
        bill_no=f"BILL{uuid.uuid4().hex[:10].upper()}",
        company_id=data.company_id,
        plan=data.plan,
        amount=plan["price"],
        currency=plan["currency"],
        status="unpaid",
        period_start=datetime.utcnow(),
        period_end=datetime.utcnow() + timedelta(days=PLAN_DAYS.get(data.plan, 365)),
        prev_plan=c.plan,
        prev_snapshot=prev_snapshot,
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

    return {"message": f"已生成 {data.plan} 套餐账单，请转账后上传付款凭证",
            "bill_no": bill.bill_no, "amount": bill.amount,
            "currency": bill.currency, "status": "unpaid",
            "pay_method": PAY_METHOD, "grace_hours": GRACE_HOURS}


@router.get("/pay-account")
def get_pay_account(current: dict = Depends(get_current_user)):
    """对公收款账户信息（付款前展示给客户，含复制内容）"""
    return {"pay_method": PAY_METHOD, "account": PAY_ACCOUNT}


@router.post("/pay")
async def pay(bill_no: str = Form(...),
              voucher: UploadFile = File(...),
              note: str = Form(""),
              db: Session = Depends(get_db),
              current: dict = Depends(get_current_user)):
    """提交转账凭证（multipart）：账单进入待财务审核，套餐立即临时生效（24h 缓冲）。

    客户付款 → 上传凭证 → 平台财务核对到账后审核；
    24h 内未获审核通过 → 自动取消升级（回退套餐）。
    """
    _require_admin_billing(current)
    bill = db.query(Bill).filter(Bill.bill_no == bill_no).first()
    if not bill or bill.company_id != current["company_id"]:
        raise HTTPException(status_code=404, detail="账单不存在")
    if bill.status == "paid":
        raise HTTPException(status_code=400, detail="账单已支付，请勿重复操作")
    if bill.status != "unpaid":
        raise HTTPException(status_code=400,
                            detail=f"账单当前状态为「{PAY_STATUS_LABELS.get(bill.status)}」，无法重复提交")

    # 保存凭证图片（限图片类型 + 大小）
    content_type = (voucher.content_type or "").lower()
    if content_type not in ("image/png", "image/jpeg", "image/jpg", "image/webp"):
        raise HTTPException(status_code=400, detail="仅支持 PNG / JPG / WEBP 图片凭证")
    ext = content_type.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    max_bytes = 10 * 1024 * 1024
    data = await voucher.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="凭证图片过大，最大 10MB")
    filename = f"{bill.bill_no}_{uuid.uuid4().hex[:8]}.{ext}"
    save_path = os.path.join(VOUCHER_DIR, filename)
    with open(save_path, "wb") as f:
        f.write(data)

    # 状态流转：unpaid → pending_review（缓冲期内套餐临时生效）
    now = datetime.utcnow()
    bill.status = "pending_review"
    bill.voucher_path = f"/static/site/payments/{filename}"
    bill.voucher_note = (note or "").strip()[:300]
    bill.submitted_at = now
    bill.grace_expires_at = now + timedelta(hours=GRACE_HOURS)

    # 立即应用套餐（24h 缓冲，让客户无缝使用）
    c = db.query(Company).filter(Company.id == bill.company_id).first()
    if c and bill.plan in PLANS:
        _apply_plan(c, bill.plan)
    db.commit()

    # 审计日志
    try:
        from utils.audit import log_action
        log_action(bill.company_id, "billing.pay_submit",
                   f"提交转账凭证 {bill.bill_no}（{bill.plan}），缓冲至 {bill.grace_expires_at}",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass

    return {
        "message": f"凭证已提交，套餐已临时生效（{GRACE_HOURS} 小时缓冲期），等待财务审核",
        "bill_no": bill.bill_no,
        "plan": bill.plan,
        "status": "pending_review",
        "status_label": "待财务审核",
        "grace_expires_at": bill.grace_expires_at.isoformat(),
        "voucher_path": bill.voucher_path,
    }


@router.get("/invoice-info")
def get_invoice_info(company_id: int, db: Session = Depends(get_db),
                     current: dict = Depends(get_current_user)):
    """获取企业开票信息"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")
    return {"info": {
        "invoice_title": c.invoice_title or "",
        "invoice_tax_no": c.invoice_tax_no or "",
        "invoice_bank": c.invoice_bank or "",
        "invoice_bank_account": c.invoice_bank_account or "",
        "invoice_address": c.invoice_address or "",
        "invoice_phone": c.invoice_phone or "",
        "invoice_type": c.invoice_type or "normal",
        "category": INVOICE_CATEGORY,
    }}


@router.put("/invoice-info")
def save_invoice_info(data: InvoiceInfoIn, company_id: int,
                      db: Session = Depends(get_db),
                      current: dict = Depends(get_current_user)):
    """保存企业开票信息（仅管理员）"""
    _require_admin_billing(current)
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")
    c.invoice_title = data.invoice_title.strip()
    c.invoice_tax_no = data.invoice_tax_no.strip()
    c.invoice_bank = data.invoice_bank.strip()
    c.invoice_bank_account = data.invoice_bank_account.strip()
    c.invoice_address = data.invoice_address.strip()
    c.invoice_phone = data.invoice_phone.strip()
    c.invoice_type = data.invoice_type if data.invoice_type in ("normal", "special") else "normal"
    db.commit()
    try:
        from utils.audit import log_action
        log_action(company_id, "billing.invoice_info",
                   "更新开票信息", current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass
    return {"message": "开票信息已保存"}


@router.post("/invoice-request")
def create_invoice_request(data: InvoiceRequestIn, db: Session = Depends(get_db),
                           current: dict = Depends(get_current_user)):
    """对已支付账单提交开票申请（开票类别：服务费）"""
    _require_admin_billing(current)
    bill = db.query(Bill).filter(Bill.bill_no == data.bill_no).first()
    if not bill or bill.company_id != current["company_id"]:
        raise HTTPException(status_code=404, detail="账单不存在")
    if bill.status != "paid":
        raise HTTPException(status_code=400, detail="仅已支付账单可申请开票")

    # 同一账单不可重复申请
    dup = db.query(InvoiceRequest).filter(
        InvoiceRequest.bill_no == data.bill_no).first()
    if dup:
        raise HTTPException(status_code=400, detail=f"该账单已提交开票申请（{dup.req_no}）")

    c = db.query(Company).filter(Company.id == bill.company_id).first()
    if not c or not (c.invoice_title and c.invoice_tax_no):
        raise HTTPException(status_code=400,
                            detail="请先在「开票信息」中填写发票抬头与税号")

    req = InvoiceRequest(
        req_no=f"INV{uuid.uuid4().hex[:10].upper()}",
        company_id=bill.company_id,
        bill_no=bill.bill_no,
        amount=bill.amount,
        invoice_type=c.invoice_type or "normal",
        category=INVOICE_CATEGORY,
        title=c.invoice_title,
        tax_no=c.invoice_tax_no,
        bank=c.invoice_bank or "",
        bank_account=c.invoice_bank_account or "",
        address=c.invoice_address or "",
        phone=c.invoice_phone or "",
        status="pending",
    )
    db.add(req)
    db.commit()

    try:
        from utils.audit import log_action
        log_action(bill.company_id, "billing.invoice_request",
                   f"提交开票申请 {req.req_no}（账单 {bill.bill_no}，类别：{INVOICE_CATEGORY}）",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass

    return {"message": f"开票申请已提交（{req.req_no}），类别：{INVOICE_CATEGORY}",
            "req_no": req.req_no, "status": req.status, "category": INVOICE_CATEGORY}


@router.get("/invoice-requests")
def list_invoice_requests(company_id: int, db: Session = Depends(get_db),
                          current: dict = Depends(get_current_user)):
    """当前企业开票申请列表"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    reqs = db.query(InvoiceRequest).filter(
        InvoiceRequest.company_id == company_id)\
        .order_by(InvoiceRequest.created_at.desc()).all()
    return {"requests": [{
        "id": r.id, "req_no": r.req_no, "bill_no": r.bill_no,
        "amount": r.amount, "invoice_type": r.invoice_type,
        "category": r.category, "title": r.title,
        "status": r.status, "invoice_no": r.invoice_no,
        "reject_reason": r.reject_reason,
        "created_at": r.created_at.isoformat(),
        "issued_at": r.issued_at.isoformat() if r.issued_at else None,
    } for r in reqs]}
