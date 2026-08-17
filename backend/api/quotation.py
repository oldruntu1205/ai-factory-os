"""
api/quotation.py —— 报价单接口（第五阶段 第三部分）

- 创建报价（AI 生成 + PDF）
- 报价列表（多租户隔离）
- 报价状态流转
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import Quotation, Customer
from auth.deps import get_current_user, require_company
from ai.llm import get_llm
from api.company import require_verified
from quote_pdf import create_quote_pdf
from constants.status import QUOTATION_STATUS

router = APIRouter(prefix="/quotation", tags=["报价单"])


class QuoteIn(BaseModel):
    requirement: str
    company_id: int
    customer_id: int = None
    customer_name: str = None


@router.post("/create")
def create_quotation(data: QuoteIn, db: Session = Depends(get_db),
                     current: dict = Depends(get_current_user)):
    """AI 生成报价并创建报价单 + PDF（需企业已认证）"""
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))

    # 1. AI 解析需求
    llm = get_llm()
    prompt = f"""
根据客户需求生成报价，输出 JSON：

{data.requirement}

JSON 格式：
{{"product": "产品", "quantity": 数量, "unit_price": 单价,
  "delivery_days": 交期天数, "payment_terms": "付款方式", "currency": "USD"}}
"""
    result = llm.invoke(prompt)
    from services.llm_parse import parse_json_output, safe_int, safe_float, safe_str
    parsed = parse_json_output(result.content.strip(), {
        "product": "待确认产品", "quantity": 1, "unit_price": 0,
        "delivery_days": 30, "payment_terms": "T/T 30% deposit", "currency": "USD",
    })

    import uuid
    quantity = safe_int(parsed.get("quantity"), 1)
    unit_price = safe_float(parsed.get("unit_price"), 0)
    # 客户归属校验：customer_id 必须存在且属于当前企业（防跨租户引用/悬空 500）
    customer_name = data.customer_name
    if data.customer_id:
        cust = db.query(Customer).filter(
            Customer.id == data.customer_id,
            Customer.company_id == data.company_id).first()
        if not cust:
            raise HTTPException(status_code=400,
                                detail="客户不存在或不属于当前企业")
        customer_name = cust.name
    quotation = Quotation(
        quotation_no=f"QT{data.company_id}{uuid.uuid4().hex[:8].upper()}",
        customer_id=data.customer_id,
        customer_name=customer_name or "未命名客户",
        product=safe_str(parsed.get("product")),
        quantity=quantity,
        unit_price=unit_price,
        total_amount=quantity * unit_price,
        currency=safe_str(parsed.get("currency"), "USD"),
        delivery_days=safe_int(parsed.get("delivery_days"), 30),
        payment_terms=safe_str(parsed.get("payment_terms"), "T/T 30% deposit"),
        status="draft",
        company_id=data.company_id,
    )
    db.add(quotation)
    db.commit()
    db.refresh(quotation)

    # 2. 生成 PDF
    try:
        q_dict = {
            "quotation_no": quotation.quotation_no,
            "customer_name": quotation.customer_name,
            "product": quotation.product,
            "quantity": quotation.quantity,
            "unit_price": quotation.unit_price,
            "total_amount": quotation.total_amount,
            "currency": quotation.currency,
            "delivery_days": quotation.delivery_days,
            "payment_terms": quotation.payment_terms,
        }
        pdf_path = create_quote_pdf(q_dict)
        quotation.pdf_path = pdf_path
        db.commit()
    except Exception:
        pass

    return {
        "message": "quote created",
        "quotation_id": quotation.id,
        "quotation_no": quotation.quotation_no,
        "product": quotation.product,
        "quantity": quotation.quantity,
        "unit_price": quotation.unit_price,
        "total_amount": quotation.total_amount,
        "currency": quotation.currency,
        "delivery_days": quotation.delivery_days,
        "payment_terms": quotation.payment_terms,
        "pdf_path": quotation.pdf_path or "",
    }


@router.get("/list")
def list_quotations(company_id: int, page: int = 1, page_size: int = 100,
                    db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    query = db.query(Quotation).filter(Quotation.company_id == company_id)
    total = query.count()
    quotes = query.order_by(Quotation.created_at.desc())\
        .offset((page - 1) * page_size).limit(page_size).all()
    return {
        "quotations": [{
            "id": q.id, "quotation_no": q.quotation_no, "customer_name": q.customer_name,
            "product": q.product, "quantity": q.quantity, "unit_price": q.unit_price,
            "total_amount": q.total_amount, "currency": q.currency,
            "delivery_days": q.delivery_days, "payment_terms": q.payment_terms,
            "status": q.status, "created_at": q.created_at.isoformat(),
        } for q in quotes],
        "total": total, "page": page, "page_size": page_size,
    }


@router.put("/{quotation_id}/status")
def update_status(quotation_id: int, status: str, company_id: int,
                  db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """报价状态流转：draft -> sent -> accepted/rejected"""
    require_company(company_id, current)
    q = db.query(Quotation).filter(
        Quotation.id == quotation_id, Quotation.company_id == company_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="报价单不存在")
    if status not in QUOTATION_STATUS:
        raise HTTPException(status_code=400, detail=f"无效状态，可选: {sorted(QUOTATION_STATUS)}")
    q.status = status
    db.commit()
    return {"message": "status updated", "status": q.status}
