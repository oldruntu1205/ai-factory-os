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
  "delivery_days": 交期天数, "payment_terms": "付款方式", "currency": "USD",
  "incoterms": "FOB/CIF/EXW/DAP", "moq": 最小起订量, "packaging": "包装说明",
  "country": "目的国代码(如 DE/US/AE，用于运费/关税估算)"}}
"""
    result = llm.invoke(prompt)
    from services.llm_parse import parse_json_output, safe_int, safe_float, safe_str
    parsed = parse_json_output(result.content.strip(), {
        "product": "待确认产品", "quantity": 1, "unit_price": 0,
        "delivery_days": 30, "payment_terms": "T/T 30% deposit", "currency": "USD",
        "incoterms": "FOB", "moq": 1, "packaging": "Standard export carton",
        "country": "",
    })

    # 2. 外贸估算（HS Code 归类 + 汇率 + 运费/关税）
    from utils.trade import classify_hs_code, get_fx_rate, estimate_freight, estimate_tariff
    hs = classify_hs_code(safe_str(parsed.get("product")))
    country = safe_str(parsed.get("country")).upper() or "US"
    currency = safe_str(parsed.get("currency"), "USD").upper()
    incoterms = safe_str(parsed.get("incoterms"), "FOB").upper()
    fx = get_fx_rate("USD", currency) if currency != "USD" else {"rate": 1.0, "source": "identity"}
    freight = estimate_freight(volume_cbm=max(1.0, safe_int(parsed.get("quantity"), 1) * 0.005),
                               destination_country=country)
    tariff = estimate_tariff(hs.get("hs_code", ""), country)

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
    # 总价：单价 × 数量（折算到报价币种）；CIF/DAP/DDP 计入运费/关税（估算，USD 折算）
    goods_total = quantity * unit_price
    total_amount = goods_total
    if incoterms in ("CIF", "DAP", "DDP"):
        total_amount += freight["freight_usd"] * fx["rate"]
    if incoterms == "DDP":
        total_amount += goods_total * (tariff["tariff_pct"] / 100.0)
    quotation = Quotation(
        quotation_no=f"QT{data.company_id}{uuid.uuid4().hex[:8].upper()}",
        customer_id=data.customer_id,
        customer_name=customer_name or "未命名客户",
        product=safe_str(parsed.get("product")),
        quantity=quantity,
        unit_price=unit_price,
        total_amount=round(total_amount, 2),
        currency=currency,
        delivery_days=safe_int(parsed.get("delivery_days"), 30),
        payment_terms=safe_str(parsed.get("payment_terms"), "T/T 30% deposit"),
        # 外贸字段
        hs_code=hs.get("hs_code", ""),
        incoterms=incoterms,
        fx_rate=round(fx["rate"], 4),
        freight_amount=round(freight["freight_usd"], 2),
        tariff_amount=round(goods_total * (tariff["tariff_pct"] / 100.0), 2),
        tariff_pct=tariff["tariff_pct"],
        moq=safe_int(parsed.get("moq"), 1),
        packaging=safe_str(parsed.get("packaging"), "Standard export carton"),
        status="draft",
        company_id=data.company_id,
    )
    db.add(quotation)
    db.commit()
    db.refresh(quotation)

    # 3. 生成 PDF（含外贸条款）
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
            # 外贸字段
            "hs_code": quotation.hs_code,
            "incoterms": quotation.incoterms,
            "fx_rate": quotation.fx_rate,
            "freight_amount": quotation.freight_amount,
            "tariff_amount": quotation.tariff_amount,
            "tariff_pct": quotation.tariff_pct,
            "moq": quotation.moq,
            "packaging": quotation.packaging,
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
        # 外贸字段回显
        "hs_code": quotation.hs_code,
        "incoterms": quotation.incoterms,
        "fx_rate": quotation.fx_rate,
        "freight_amount": quotation.freight_amount,
        "tariff_amount": quotation.tariff_amount,
        "tariff_pct": quotation.tariff_pct,
        "moq": quotation.moq,
        "packaging": quotation.packaging,
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
            "hs_code": q.hs_code or "", "incoterms": q.incoterms or "FOB",
            "fx_rate": q.fx_rate or 1.0, "freight_amount": q.freight_amount or 0.0,
            "tariff_amount": q.tariff_amount or 0.0, "tariff_pct": q.tariff_pct or 0.0,
            "moq": q.moq or 1, "packaging": q.packaging or "",
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
