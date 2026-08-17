"""
api/crm.py —— CRM 客户管理系统（第四、五阶段）

- 创建客户/线索
- 客户列表（多租户隔离）
- 更新客户状态（new/following/quoted/won/lost）
- AI 自动创建线索入口
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import Customer
from auth.deps import get_current_user, require_company
from constants.status import CUSTOMER_STATUS

router = APIRouter(prefix="/crm", tags=["CRM客户"])


class CustomerIn(BaseModel):
    name: str
    country: str = ""
    email: str = ""
    phone: str = ""
    whatsapp: str = ""
    requirement: str = ""
    source: str = "manual"
    company_id: int


class CustomerUpdate(BaseModel):
    status: str = None
    score: int = None
    note: str = None


@router.post("/customer")
def create_customer(data: CustomerIn, db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """创建客户"""
    require_company(data.company_id, current)
    cust = Customer(
        name=data.name, country=data.country, email=data.email,
        phone=data.phone, whatsapp=data.whatsapp, requirement=data.requirement,
        source=data.source, company_id=data.company_id,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return {"message": "create success", "customer_id": cust.id}


@router.get("/customers")
def list_customers(company_id: int, page: int = 1, page_size: int = 100,
                   db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """客户列表（分页，默认返回前 100 条兼容旧前端）"""
    require_company(company_id, current)
    query = db.query(Customer).filter(Customer.company_id == company_id)
    total = query.count()
    customers = query.order_by(Customer.created_at.desc())\
        .offset((page - 1) * page_size).limit(page_size).all()
    return {
        "customers": [{
            "id": c.id, "name": c.name, "country": c.country, "email": c.email,
            "phone": c.phone, "whatsapp": c.whatsapp, "requirement": c.requirement,
            "source": c.source, "status": c.status, "score": c.score,
            "created_at": c.created_at.isoformat(),
        } for c in customers],
        "total": total, "page": page, "page_size": page_size,
    }


@router.put("/customer/{customer_id}")
def update_customer(customer_id: int, data: CustomerUpdate, company_id: int,
                    db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """更新客户状态/评分"""
    require_company(company_id, current)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.company_id == company_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="客户不存在")
    if data.status:
        if data.status not in CUSTOMER_STATUS:
            raise HTTPException(status_code=400, detail=f"状态无效，可选: {sorted(CUSTOMER_STATUS)}")
        cust.status = data.status
    if data.score is not None:
        cust.score = data.score
    db.commit()
    return {"message": "update success"}


@router.delete("/customer/{customer_id}")
def delete_customer(customer_id: int, company_id: int,
                    db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.company_id == company_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="客户不存在")
    db.delete(cust)
    db.commit()
    return {"message": "delete success"}
