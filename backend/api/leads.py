"""
api/leads.py —— V2.1 AI 自主销售线索 API（升级版）

- POST /leads/discover             AI 客户发现（关键词/国家/行业筛选）+ 评分
- GET  /leads/list                 线索列表（grade / status 过滤）
- GET  /leads/stats                统计（等级 / 状态 / 打开 / 转化）
- GET  /leads/{id}                 线索详情（评分理由 / enriched）
- POST /leads/{id}/follow          发送首封开发信（兼容旧调用）
- POST /leads/{id}/send-sequence   发送指定步数跟进（step / language / variant）
- POST /leads/run-sequence         手动触发自动跟进（到期线索）
- POST /leads/{id}/mark-reply      标记已回复
- POST /leads/{id}/pause           暂停序列
- POST /leads/{id}/resume          恢复序列
- POST /leads/{id}/convert         转入 CRM（创建 Customer）
- GET  /leads/track/{track_id}     打开追踪像素（邮件内 1x1 图片，公开）
- GET  /leads/unsubscribe/{track_id} 退订页（公开）
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import base64
import uuid
import logging

from database import get_db
from models import Lead, Customer
from auth.deps import get_current_user, require_company
from api.company import require_verified
from sales.discovery import discover_leads
from sales.scoring import score_lead, grade_of
from channels.email.sequence import send_follow_up, run_due_follow_ups

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leads", tags=["AI自主销售"])

# 1x1 透明 PNG（打开追踪像素）
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


# ---------------- 请求模型 ----------------
class DiscoverIn(BaseModel):
    company_id: int
    keywords: str = "laboratory glassware"
    countries: list[str] = []
    industries: list[str] = []
    # 安全修复：limit 夹取 1-50（此前无上限可触发海量 LLM 调用）
    limit: int = Field(20, ge=1, le=50)


class FollowIn(BaseModel):
    company_id: int
    product: str = "laboratory glassware"
    language: str = "en"
    variant: str = "A"


class SendSeqIn(BaseModel):
    company_id: int
    step: int = 1
    language: str = "en"
    variant: str = "A"


class CompanyIn(BaseModel):
    company_id: int


class ImportLeadItem(BaseModel):
    company_name: str = ""
    email: str = ""
    country: str = ""
    contact: str = ""
    phone: str = ""
    website: str = ""
    requirement: str = ""
    source: str = "import"


class ImportIn(BaseModel):
    company_id: int
    leads: list[ImportLeadItem] = []
    auto_follow: bool = False   # 导入后自动发送首封（按国家语言）


# ---------------- 真实询盘导入（出口型制造业 AI 应用 第3项） ----------------
@router.post("/import")
def import_leads(data: ImportIn, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """批量导入真实询盘 -> AI 解析 -> 评分 -> （可选）自动首封

    区别于 /discover（Mock 生成），本端点面向真实询盘源（独立站表单/展会/业务手动录入）。
    """
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))
    from sales.parse_inquiry import parse_inquiry
    from sales.scoring import score_lead
    from utils.trade import COUNTRY_LANGUAGES

    items = (data.leads or [])[:200]  # 单次上限 200
    created = []
    followed = 0
    for item in items:
        parsed = parse_inquiry(item.requirement) if item.requirement else {}
        country = (parsed.get("country") or item.country or "").upper()
        # 出口合规快速筛查（示例规则，需人工核验）
        from utils.compliance import check_lead_compliance
        compliance = check_lead_compliance(item.company_name, country, item.contact)
        lead = Lead(
            company_name=item.company_name or parsed.get("product") or "未命名客户",
            website=item.website or "",
            country=country,
            email=item.email or "",
            industry=parsed.get("product") or "",
            status="discovered",
            followup_day=1,
            follow_up_step=0,
            source=item.source or "import",
            enriched={
                **parsed,
                "contact": item.contact or "", "phone": item.phone or "",
                "raw_inquiry": (item.requirement or "")[:1000],
                "compliance": compliance,
            },
            track_id=uuid.uuid4().hex[:16],
            company_id=data.company_id,
        )
        score_lead(lead)  # LLM 评分（失败回退规则）
        db.add(lead)
        db.flush()
        created.append({
            "id": lead.id, "company_name": lead.company_name,
            "country": country, "score": lead.score, "grade": lead.grade,
            "product": parsed.get("product", ""),
            "intent": parsed.get("intent", "medium"),
        })
    db.commit()

    # 自动首封（按国家语言；同一会话内发送）
    if data.auto_follow:
        from channels.email.sequence import send_follow_up
        for item in created:
            try:
                lang = COUNTRY_LANGUAGES.get(item["country"], "en")
                send_follow_up(item["id"], data.company_id, step=1,
                               language=lang, variant="A",
                               track_id="")
                followed += 1
            except Exception as e:
                db.rollback()
                logger.error(f"导入自动首封失败 lead={item['id']}: {e}")
    return {"message": f"已导入 {len(created)} 条询盘（自动首封 {followed} 条）",
            "leads": created, "count": len(created), "followed": followed}


# ---------------- 发现 ----------------
@router.post("/discover")
def discover(data: DiscoverIn, current: dict = Depends(get_current_user)):
    """AI 客户发现 + 自动评分（需企业已认证）"""
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))
    leads = discover_leads(data.company_id, data.keywords,
                           data.countries, data.industries, data.limit)
    return {"message": "discovery complete", "leads": leads, "count": len(leads)}


# ---------------- 列表 ----------------
@router.get("/list")
def list_leads(company_id: int, grade: str = None, status: str = None,
               db: Session = Depends(get_db),
               current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    query = db.query(Lead).filter(Lead.company_id == company_id)
    if grade:
        query = query.filter(Lead.grade == grade)
    if status:
        query = query.filter(Lead.status == status)
    leads = query.order_by(Lead.score.desc()).all()
    return {"leads": [{
        "id": l.id, "company_name": l.company_name, "website": l.website,
        "country": l.country, "email": l.email, "industry": l.industry,
        "score": l.score, "grade": l.grade, "status": l.status,
        "follow_up_step": l.follow_up_step, "variant": l.variant,
        "opened": l.opened, "open_count": l.open_count,
        "converted": l.converted, "created_at": l.created_at.isoformat(),
        # 出口合规筛查状态（示例规则；flagged 需人工核验）
        "compliance_status": ((l.enriched or {}).get("compliance") or {}).get("status", ""),
        "compliance_reasons": ((l.enriched or {}).get("compliance") or {}).get("reasons", []),
        "cert_hint": ((l.enriched or {}).get("compliance") or {}).get("cert_hint", ""),
    } for l in leads]}


# ---------------- 统计 ----------------
@router.get("/stats")
def leads_stats(company_id: int, db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    leads = db.query(Lead).filter(Lead.company_id == company_id).all()
    by_grade = {"A": 0, "B": 0, "C": 0}
    by_status = {}
    opened = converted = 0
    for l in leads:
        by_grade[l.grade] = by_grade.get(l.grade, 0) + 1
        by_status[l.status] = by_status.get(l.status, 0) + 1
        if l.opened:
            opened += 1
        if l.converted:
            converted += 1
    return {
        "total": len(leads),
        "by_grade": by_grade,
        "by_status": by_status,
        "opened": opened,
        "converted": converted,
    }


# ---------------- 详情 ----------------
@router.get("/{lead_id}")
def lead_detail(lead_id: int, company_id: int,
                db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    return {
        "id": lead.id, "company_name": lead.company_name, "website": lead.website,
        "country": lead.country, "email": lead.email, "industry": lead.industry,
        "score": lead.score, "grade": lead.grade, "status": lead.status,
        "score_reasons": lead.score_reasons, "enriched": lead.enriched or {},
        "follow_up_step": lead.follow_up_step, "variant": lead.variant,
        "opened": lead.opened, "open_count": lead.open_count,
        "converted": lead.converted, "notes": lead.notes,
    }


# ---------------- 发送首封（兼容旧） ----------------
@router.post("/{lead_id}/follow")
def follow_lead(lead_id: int, data: FollowIn, db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == data.company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    r = send_follow_up(lead.id, data.company_id, step=1,
                       language=data.language, variant=data.variant,
                       track_id=lead.track_id or "")
    db.refresh(lead)
    return {"message": "follow-up sent", "result": r,
            "status": lead.status, "follow_up_step": lead.follow_up_step}


# ---------------- 发送指定步数跟进 ----------------
@router.post("/{lead_id}/send-sequence")
def send_sequence(lead_id: int, data: SendSeqIn, db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == data.company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    r = send_follow_up(lead.id, data.company_id, step=data.step,
                       language=data.language, variant=data.variant,
                       track_id=lead.track_id or "")
    db.refresh(lead)
    return {"message": "sequence sent", "result": r,
            "status": lead.status, "follow_up_step": lead.follow_up_step}


# ---------------- 手动触发自动跟进 ----------------
@router.post("/run-sequence")
def run_sequence(data: CompanyIn, current: dict = Depends(get_current_user)):
    """手动触发本企业自动跟进（修复：此前会触发全平台所有企业发信）"""
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))
    result = run_due_follow_ups(company_id=data.company_id)
    return result


# ---------------- 标记已回复 ----------------
@router.post("/{lead_id}/mark-reply")
def mark_reply(lead_id: int, data: CompanyIn, db: Session = Depends(get_db),
               current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == data.company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    lead.status = "replied"
    db.commit()
    return {"message": "marked replied", "status": lead.status}


# ---------------- 暂停 / 恢复 ----------------
@router.post("/{lead_id}/pause")
def pause_lead(lead_id: int, data: CompanyIn, db: Session = Depends(get_db),
               current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == data.company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    lead.status = "paused"
    db.commit()
    return {"message": "paused", "status": lead.status}


@router.post("/{lead_id}/resume")
def resume_lead(lead_id: int, data: CompanyIn, db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == data.company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    lead.status = "contacted" if lead.follow_up_step else "discovered"
    db.commit()
    return {"message": "resumed", "status": lead.status}


# ---------------- 转入 CRM ----------------
@router.post("/{lead_id}/convert")
def convert_lead(lead_id: int, data: CompanyIn, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    lead = db.query(Lead).filter(Lead.id == lead_id,
                                 Lead.company_id == data.company_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="线索不存在")
    if lead.converted:
        return {"message": "already converted", "customer_id": None}
    cust = Customer(
        name=lead.company_name,
        country=lead.country,
        email=lead.email,
        phone="",
        whatsapp="",
        requirement=(lead.industry or "") + (f" | {lead.notes}" if lead.notes else ""),
        source="lead_discovery",
        status="new",
        company_id=data.company_id,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    lead.converted = True
    lead.status = "qualified"
    db.commit()
    return {"message": "converted to CRM", "customer_id": cust.id,
            "status": lead.status}


# ---------------- 打开追踪像素（公开） ----------------
@router.get("/track/{track_id}")
def track_open(track_id: str, db: Session = Depends(get_db)):
    tid = track_id.replace(".png", "")
    lead = db.query(Lead).filter(Lead.track_id == tid).first()
    if lead:
        lead.opened = True
        lead.open_count = (lead.open_count or 0) + 1
        db.commit()
    return Response(content=_PIXEL_PNG, media_type="image/png")


# ---------------- 退订页（公开） ----------------
@router.get("/unsubscribe/{track_id}")
def unsubscribe(track_id: str, db: Session = Depends(get_db)):
    tid = track_id
    lead = db.query(Lead).filter(Lead.track_id == tid).first()
    if lead and lead.status != "unsubscribed":
        lead.status = "unsubscribed"
        db.commit()
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
        "<h2>您已成功退订</h2>"
        "<p>我们将不再向该邮箱发送营销邮件。如需恢复，请联系您的销售代表。</p>"
        "</body></html>"
    )
