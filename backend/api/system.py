"""
api/system.py —— 系统管理接口（V2.0 生产/质量数据演示 + 套餐信息）
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import ProductionOrder, QualityReport
from auth.deps import get_current_user, require_company

router = APIRouter(prefix="/system", tags=["系统"])


class OrderIn(BaseModel):
    order_no: str
    product: str = ""
    quantity: int = 0
    deadline_days: int = 30
    progress: int = 0
    line: str = "A01"
    load_rate: int = 0
    delay: bool = False
    company_id: int


class QualityIn(BaseModel):
    line: str
    defect_rate: float = 0.0
    defect_type: str = ""
    total_count: int = 0
    defect_count: int = 0
    company_id: int


@router.post("/order")
def create_order(data: OrderIn, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """写入 MES 生产订单（演示）"""
    require_company(data.company_id, current)
    order = ProductionOrder(
        order_no=data.order_no, product=data.product, quantity=data.quantity,
        deadline_days=data.deadline_days, progress=data.progress,
        line=data.line, load_rate=data.load_rate, delay=data.delay,
        company_id=data.company_id,
    )
    db.add(order)
    db.commit()
    return {"message": "order created", "order_id": order.id}


@router.post("/quality")
def create_quality(data: QualityIn, db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """写入 QMS 质量数据（演示）"""
    require_company(data.company_id, current)
    report = QualityReport(
        line=data.line, defect_rate=data.defect_rate, defect_type=data.defect_type,
        total_count=data.total_count, defect_count=data.defect_count,
        company_id=data.company_id,
    )
    db.add(report)
    db.commit()
    return {"message": "quality report created", "report_id": report.id}


@router.get("/plans")
def plans():
    """SaaS 套餐信息（统一来自 constants/plans.py）"""
    from constants.plans import plan_list
    return {"plans": plan_list()}
