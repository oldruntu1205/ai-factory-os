"""
api/platform.py —— 平台超级管理员 API

- GET  /platform/stats                       平台全局统计（企业数/对话量/营收）
- GET  /platform/companies                   企业列表（含认证状态/套餐）
- GET  /platform/verifications               营业执照待审核列表
- POST /platform/companies/{id}/verify       审核通过/拒绝
- POST /platform/companies/{id}/suspend      挂起/恢复企业
- GET  /platform/bills                       平台账单流水
- GET  /platform/llm-config                  读取平台 LLM 网关配置（API Key 脱敏）
- PUT  /platform/llm-config                  保存平台 LLM 网关配置（热切换，无需重启）
"""
from datetime import datetime
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from config import LLM_GATEWAY, LLM_FALLBACK
from database import get_db, SessionLocal
from models import Company, User, Bill, Conversation, AIAgent, PlatformLLMConfig, InvoiceRequest, SiteContent
from auth.deps import get_current_user
from ai.llm import GATEWAY_ROUTES, set_config_override, get_config_override
from api.billing import _revert_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform", tags=["平台管理"])


def _require_platform(current: dict):
    if current.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要平台管理员权限")


# ---------- LLM 网关配置 ----------
LLM_PROVIDERS = ["deepseek", "fireworks", "openai", "zhipu", "dashscope", "ollama"]
_PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "fireworks": "Fireworks AI",
    "openai": "OpenAI",
    "zhipu": "智谱 AI (GLM)",
    "dashscope": "通义千问 (阿里云)",
    "ollama": "Ollama (本地)",
}


class LLMProviderIn(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""


class LLMConfigIn(BaseModel):
    gateway: str = "deepseek"
    providers: dict = {}   # { gw: {api_key, base_url, model} }


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "•" * (len(key) - 8) + key[-4:]


def _cfg_to_override(cfg: dict) -> dict:
    """把落库 config 转成 ai.llm 运行时 override 结构"""
    providers = cfg.get("providers") or {}
    return {
        "gateway": cfg.get("gateway") or "deepseek",
        "keys": {gw: (providers.get(gw) or {}).get("api_key") or "" for gw in LLM_PROVIDERS},
        "base_urls": {gw: (providers.get(gw) or {}).get("base_url") or "" for gw in LLM_PROVIDERS},
        "models": {gw: (providers.get(gw) or {}).get("model") or "" for gw in LLM_PROVIDERS},
    }


def load_config_from_db():
    """启动时从库加载平台 LLM 配置到运行时（热路径快取）"""
    try:
        db = SessionLocal()
        try:
            row = db.query(PlatformLLMConfig).filter(PlatformLLMConfig.id == 1).first()
            if row and row.config:
                set_config_override(_cfg_to_override(row.config))
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"加载平台 LLM 配置失败（将使用环境变量回退）: {e}")


@router.get("/llm-config")
def get_llm_config(db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """读取平台 LLM 网关配置；API Key 脱敏返回"""
    _require_platform(current)
    row = db.query(PlatformLLMConfig).filter(PlatformLLMConfig.id == 1).first()
    cfg = row.config if row and row.config else {}
    gateway = cfg.get("gateway") or LLM_GATEWAY or "deepseek"
    providers = {}
    for gw in LLM_PROVIDERS:
        p = (cfg.get("providers") or {}).get(gw) or {}
        key = p.get("api_key") or ""
        default_base, default_model = GATEWAY_ROUTES.get(gw, ("", "deepseek-chat"))
        providers[gw] = {
            "label": _PROVIDER_LABELS.get(gw, gw),
            "api_key_set": bool(key),
            "api_key_mask": _mask_key(key),
            "base_url": p.get("base_url") or default_base,
            "model": p.get("model") or default_model,
        }
    return {
        "gateway": gateway,
        "providers": providers,
        "available": LLM_PROVIDERS,
        "labels": _PROVIDER_LABELS,
        "defaults": {gw: {"base_url": GATEWAY_ROUTES[gw][0],
                          "model": GATEWAY_ROUTES[gw][1]} for gw in LLM_PROVIDERS},
        "fallback": bool(LLM_FALLBACK),
        # 运行时当前生效网关（优先 override，否则环境变量）
        "effective_gateway": (get_config_override() or {}).get("gateway")
                              or LLM_GATEWAY or "deepseek",
    }


@router.put("/llm-config")
def put_llm_config(payload: LLMConfigIn, db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """保存平台 LLM 网关配置；api_key 留空表示保留原值（不覆盖）"""
    _require_platform(current)
    gateway = (payload.gateway or "deepseek").lower()
    if gateway not in LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支持的网关：{gateway}")

    row = db.query(PlatformLLMConfig).filter(PlatformLLMConfig.id == 1).first()
    old_cfg = row.config if row and row.config else {}
    old_providers = old_cfg.get("providers") or {}

    new_providers = {}
    for gw in LLM_PROVIDERS:
        inp = payload.providers.get(gw) or {}
        old = old_providers.get(gw) or {}
        # api_key 非空则更新；空则保留库中原值（避免误清空）
        ak = (inp.get("api_key") or "").strip()
        key = ak if ak else (old.get("api_key") or "")
        new_providers[gw] = {
            "api_key": key,
            "base_url": (inp.get("base_url") or "").strip() or (old.get("base_url") or ""),
            "model": (inp.get("model") or "").strip() or (old.get("model") or ""),
        }

    new_cfg = {
        "gateway": gateway,
        "providers": new_providers,
        "updated_at": datetime.utcnow().isoformat(),
    }

    now = datetime.utcnow()
    if row:
        row.config = new_cfg
        row.updated_at = now
    else:
        row = PlatformLLMConfig(id=1, config=new_cfg, updated_at=now)
        db.add(row)
    db.commit()

    # 热切换：刷新运行时 override，下次对话即时生效
    set_config_override(_cfg_to_override(new_cfg))
    return {"message": "模型配置已保存并即时生效", "gateway": gateway}


class VerifyIn(BaseModel):
    approve: bool = True
    reject_reason: str = ""


@router.get("/stats")
def platform_stats(db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """平台全局统计"""
    _require_platform(current)
    companies = db.query(Company).count()
    users = db.query(User).count()
    conversations = db.query(Conversation).count()
    agents = db.query(AIAgent).count()
    bills = db.query(Bill).all()
    paid_amount = round(sum(b.amount for b in bills if b.status == "paid"), 2)
    verified = db.query(Company).filter(Company.license_verified.is_(True)).count()

    return {
        "companies": companies,
        "users": users,
        "conversations": conversations,
        "agents": agents,
        "paid_revenue": paid_amount,
        "verified_companies": verified,
        "unverified_companies": companies - verified,
    }


@router.get("/companies")
def platform_companies(db: Session = Depends(get_db),
                       current: dict = Depends(get_current_user)):
    """平台企业列表（N+1 优化：一次聚合查询统计成员/AI员工数）"""
    _require_platform(current)
    companies = db.query(Company).order_by(Company.created_at.desc()).all()
    if not companies:
        return {"companies": [], "count": 0}

    from sqlalchemy import func
    cids = [c.id for c in companies]
    user_counts = dict(db.query(User.company_id, func.count(User.id))
                       .filter(User.company_id.in_(cids))
                       .group_by(User.company_id).all())
    agent_counts = dict(db.query(AIAgent.company_id, func.count(AIAgent.id))
                        .filter(AIAgent.company_id.in_(cids))
                        .group_by(AIAgent.company_id).all())

    result = [{
        "id": c.id, "name": c.name, "plan": c.plan,
        "license_status": c.license_status or "none",
        "license_verified": bool(c.license_verified),
        "members": user_counts.get(c.id, 0), "agents": agent_counts.get(c.id, 0),
        "max_agents": c.max_agents,
        "created_at": c.created_at.isoformat(),
    } for c in companies]
    return {"companies": result, "count": len(result)}


@router.get("/verifications")
def pending_verifications(db: Session = Depends(get_db),
                          current: dict = Depends(get_current_user)):
    """营业执照待审核列表（pending / rejected 也展示便于复审）"""
    _require_platform(current)
    rows = db.query(Company).filter(
        Company.license_status.in_(["pending", "rejected"])).all()
    return {"verifications": [{
        "id": c.id, "name": c.name, "license_status": c.license_status,
        "has_license": bool(c.business_license_path),
        "license_reject_reason": c.license_reject_reason or "",
        "uploaded_at": c.license_uploaded_at.isoformat() if c.license_uploaded_at else None,
    } for c in rows]}


@router.post("/companies/{company_id}/verify")
def verify_company(company_id: int, data: VerifyIn,
                   db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """审核企业营业执照"""
    _require_platform(current)
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")

    if data.approve:
        c.license_status = "approved"
        c.license_verified = True
        c.license_verified_at = datetime.utcnow()
        c.license_reject_reason = ""
        msg = "已通过认证"
    else:
        c.license_status = "rejected"
        c.license_verified = False
        c.license_reject_reason = data.reject_reason
        msg = "已拒绝（请企业重新提交）"
    db.commit()
    return {"message": msg, "company": c.name, "license_status": c.license_status}


@router.post("/companies/{company_id}/suspend")
def suspend_company(company_id: int, suspend: bool = True,
                    db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """挂起/恢复企业"""
    _require_platform(current)
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")
    # 用 plan 前加前缀标记挂起（或直接操作 users 停用）
    if suspend:
        db.query(User).filter(User.company_id == company_id).update({"status": "disabled"})
        msg = "企业已挂起（全员停用）"
    else:
        db.query(User).filter(User.company_id == company_id).update({"status": "active"})
        msg = "企业已恢复"
    db.commit()
    return {"message": msg}


@router.get("/bills")
def platform_bills(db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """平台账单流水（含对公转账凭证/审核状态）"""
    _require_platform(current)
    bills = db.query(Bill).order_by(Bill.created_at.desc()).limit(200).all()
    return {"bills": [{
        "id": b.id, "bill_no": b.bill_no, "company_id": b.company_id,
        "plan": b.plan, "amount": b.amount, "currency": b.currency,
        "status": b.status, "paid_at": b.paid_at.isoformat() if b.paid_at else None,
        "created_at": b.created_at.isoformat(),
        "voucher_path": b.voucher_path or "",
        "voucher_note": b.voucher_note or "",
        "submitted_at": b.submitted_at.isoformat() if b.submitted_at else None,
        "grace_expires_at": b.grace_expires_at.isoformat() if b.grace_expires_at else None,
        "review_note": b.review_note or "",
    } for b in bills]}


@router.get("/bills/pending-review")
def platform_pending_review_bills(db: Session = Depends(get_db),
                                  current: dict = Depends(get_current_user)):
    """待财务审核的对公转账账单（凭证 + 缓冲倒计时）"""
    _require_platform(current)
    now = datetime.utcnow()
    bills = db.query(Bill).filter(
        Bill.status == "pending_review").order_by(Bill.submitted_at.asc()).all()
    result = []
    for b in bills:
        grace_left = None
        if b.grace_expires_at:
            grace_left = max(0, int((b.grace_expires_at - now).total_seconds() // 3600))
        result.append({
            "id": b.id, "bill_no": b.bill_no, "company_id": b.company_id,
            "plan": b.plan, "amount": b.amount, "currency": b.currency,
            "voucher_path": b.voucher_path or "",
            "voucher_note": b.voucher_note or "",
            "submitted_at": b.submitted_at.isoformat() if b.submitted_at else None,
            "grace_expires_at": b.grace_expires_at.isoformat() if b.grace_expires_at else None,
            "grace_left_hours": grace_left,
        })
    return {"bills": result}


class BillReviewIn(BaseModel):
    action: str = "approve"    # approve / reject
    note: str = ""


@router.post("/bills/{bill_no}/review")
def review_bill(bill_no: str, data: BillReviewIn,
                db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    """财务审核对公转账：approve 确认到账（套餐正式生效）/ reject 驳回（回退套餐）"""
    _require_platform(current)
    bill = db.query(Bill).filter(Bill.bill_no == bill_no).first()
    if not bill:
        raise HTTPException(status_code=404, detail="账单不存在")
    if bill.status != "pending_review":
        raise HTTPException(status_code=400, detail="仅待审核账单可审核")

    c = db.query(Company).filter(Company.id == bill.company_id).first()
    now = datetime.utcnow()

    if data.action == "approve":
        bill.status = "paid"
        bill.paid_at = now
        bill.reviewed_by = current.get("email", "")
        bill.reviewed_at = now
        bill.review_note = (data.note or "").strip()[:300]
        # 套餐已在提交凭证时临时生效，正式确认无需重复应用
        msg = f"已确认到账，套餐正式生效（{bill.plan}）"
        act = "finance.bill_approve"
    elif data.action == "reject":
        bill.status = "rejected"
        bill.reviewed_by = current.get("email", "")
        bill.reviewed_at = now
        bill.review_note = (data.note or "凭证与到账不符").strip()[:300]
        # 回退套餐到升级前
        if c:
            _revert_plan(c, bill)
        msg = f"已驳回，套餐已回退至 {c.plan if c else bill.prev_plan}"
        act = "finance.bill_reject"
    else:
        raise HTTPException(status_code=400, detail="action 仅支持 approve/reject")

    db.commit()

    try:
        from utils.audit import log_action
        log_action(bill.company_id, act,
                   f"{'确认到账' if data.action == 'approve' else '驳回'} {bill.bill_no}：{bill.review_note}",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass

    return {"message": msg, "bill_no": bill.bill_no, "status": bill.status}


@router.get("/finance")
def platform_finance(db: Session = Depends(get_db),
                     current: dict = Depends(get_current_user)):
    """平台财务总览：收入汇总 + 按套餐/月份统计 + 账单流水"""
    _require_platform(current)
    from sqlalchemy import func

    bills = db.query(Bill).all()

    # 收入汇总
    total_revenue = sum(b.amount for b in bills if b.status == "paid")
    paid_count = sum(1 for b in bills if b.status == "paid")
    pending_amount = sum(b.amount for b in bills if b.status == "pending")
    pending_count = sum(1 for b in bills if b.status == "pending")

    # 按套餐统计
    plan_stats = {}
    for b in bills:
        if b.status != "paid":
            continue
        p = plan_stats.setdefault(b.plan, {"count": 0, "amount": 0.0})
        p["count"] += 1
        p["amount"] += b.amount

    # 按月份统计（近 12 个月）
    month_stats = {}
    for b in bills:
        if b.status != "paid":
            continue
        key = b.created_at.strftime("%Y-%m")
        m = month_stats.setdefault(key, {"month": key, "count": 0, "amount": 0.0})
        m["count"] += 1
        m["amount"] += b.amount
    months = sorted(month_stats.keys())[-12:]
    month_list = [month_stats[m] for m in months]

    # 最近 50 条流水
    recent = sorted(bills, key=lambda b: b.created_at, reverse=True)[:50]

    return {
        "summary": {
            "total_revenue": round(total_revenue, 2),
            "paid_count": paid_count,
            "pending_amount": round(pending_amount, 2),
            "pending_count": pending_count,
            "bill_count": len(bills),
        },
        "plan_stats": [{"plan": k, **v} for k, v in plan_stats.items()],
        "month_stats": month_list,
        "recent_bills": [{
            "id": b.id, "bill_no": b.bill_no, "company_id": b.company_id,
            "plan": b.plan, "amount": b.amount, "currency": b.currency,
            "status": b.status, "created_at": b.created_at.isoformat(),
        } for b in recent],
    }


# ============ 对公收款账户配置（财务）============

class PayAccountIn(BaseModel):
    account_name: str = ""
    bank_name: str = ""
    account_no: str = ""
    note: str = ""


@router.get("/pay-account")
def get_pay_account_config(db: Session = Depends(get_db),
                           current: dict = Depends(get_current_user)):
    """平台财务：查看当前对公收款账户配置（含是否使用演示数据）"""
    _require_platform(current)
    from api.billing import _get_pay_account
    row = db.query(SiteContent).filter(SiteContent.id == 1).first()
    configured = bool(row and row.pay_account and any(row.pay_account.values()))
    return {"account": _get_pay_account(db), "configured": configured}


@router.put("/pay-account")
def save_pay_account_config(data: PayAccountIn, db: Session = Depends(get_db),
                            current: dict = Depends(get_current_user)):
    """平台财务：维护对公收款账户（保存后付款弹窗实时生效）"""
    _require_platform(current)
    row = db.query(SiteContent).filter(SiteContent.id == 1).first()
    if not row:
        row = SiteContent(id=1)
        db.add(row)
    row.pay_account = {
        "account_name": data.account_name.strip(),
        "bank_name": data.bank_name.strip(),
        "account_no": data.account_no.strip(),
        "note": data.note.strip(),
    }
    db.commit()
    try:
        from utils.audit import log_action
        log_action(0, "finance.pay_account_config",
                   "更新对公收款账户", current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass
    return {"message": "对公收款账户已保存，付款弹窗实时生效"}


# ============ 开票管理（财务）============

@router.get("/invoice-requests")
def platform_invoice_requests(db: Session = Depends(get_db),
                              current: dict = Depends(get_current_user)):
    """平台全部开票申请（财务侧）"""
    _require_platform(current)
    reqs = db.query(InvoiceRequest).order_by(InvoiceRequest.created_at.desc()).limit(200).all()
    return {"requests": [{
        "id": r.id, "req_no": r.req_no, "company_id": r.company_id,
        "bill_no": r.bill_no, "amount": r.amount, "invoice_type": r.invoice_type,
        "category": r.category, "title": r.title, "tax_no": r.tax_no,
        "bank": r.bank, "bank_account": r.bank_account,
        "address": r.address, "phone": r.phone,
        "status": r.status, "invoice_no": r.invoice_no,
        "reject_reason": r.reject_reason,
        "created_at": r.created_at.isoformat(),
        "issued_at": r.issued_at.isoformat() if r.issued_at else None,
    } for r in reqs]}


@router.post("/invoice-requests/{req_id}/issue")
def issue_invoice(req_id: int, invoice_no: str = "",
                  db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """财务标记开票完成（回填发票号）"""
    _require_platform(current)
    req = db.query(InvoiceRequest).filter(InvoiceRequest.id == req_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="开票申请不存在")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="该申请已处理")
    req.status = "issued"
    req.invoice_no = invoice_no.strip()
    req.issued_at = datetime.utcnow()
    db.commit()
    try:
        from utils.audit import log_action
        log_action(req.company_id, "finance.invoice_issue",
                   f"开票完成 {req.req_no}（发票号 {invoice_no}）",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass
    return {"message": f"已标记开票完成：{req.req_no}", "status": "issued"}


@router.post("/invoice-requests/{req_id}/reject")
def reject_invoice(req_id: int, reason: str = "",
                   db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """财务驳回开票申请"""
    _require_platform(current)
    req = db.query(InvoiceRequest).filter(InvoiceRequest.id == req_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="开票申请不存在")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="该申请已处理")
    req.status = "rejected"
    req.reject_reason = reason.strip()[:300]
    db.commit()
    try:
        from utils.audit import log_action
        log_action(req.company_id, "finance.invoice_reject",
                   f"驳回开票申请 {req.req_no}：{req.reject_reason}",
                   current.get("user_id", 0), current.get("email", ""))
    except Exception:
        pass
    return {"message": f"已驳回：{req.req_no}", "status": "rejected"}
