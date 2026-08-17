"""
api/systems_data.py —— 业务系统数据中心（CRM / ERP / MES / QMS 明细展示）

客户对接外部系统后，本接口汇总并展示各系统的业务明细：
- GET /systems/{system_type}?company_id=   返回某系统的指标 + 明细列表

source 含义：
  platform  —— 平台已沉淀的真实数据（生产订单 / 质量报告 / 客户 / 报价 / 线索）
  sample    —— 示例数据（ERP 暂无对应数据模型，或该系统尚未沉淀数据，已明确标注）

同时返回 integration_status（该外部系统对接状态）与 external_connected，
供前端展示“已对接外部系统”标识。

说明：真实外部系统（CRM/MES/QMS/ERP）的实时拉取为待升级项，
      当前优先级为：平台沉淀数据 → 示例数据。对接外部系统后将在该接口接入实时同步。
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import (ProductionOrder, QualityReport, Customer, Quotation,
                    Lead, IntegrationConfig)
from auth.deps import get_current_user, require_company

router = APIRouter(prefix="/systems", tags=["业务系统数据中心"])

_VALID = {"crm", "erp", "mes", "qms"}


def _integration_status(db: Session, company_id: int, system_type: str) -> str:
    row = db.query(IntegrationConfig).filter(
        IntegrationConfig.company_id == company_id,
        IntegrationConfig.system_type == system_type).first()
    return row.status if row else "unconfigured"


def _recent_customers(db: Session, company_id: int, limit: int = 8) -> list:
    rows = db.query(Customer).filter(Customer.company_id == company_id) \
        .order_by(Customer.id.desc()).limit(limit).all()
    out = []
    for c in rows:
        out.append({
            "id": c.id,
            "name": c.name,
            "email": getattr(c, "email", "") or "",
            "phone": getattr(c, "phone", "") or "",
            "whatsapp": getattr(c, "whatsapp", "") or "",
            "company": getattr(c, "company", "") or "",
            "created_at": c.created_at.isoformat() if c.created_at else "",
        })
    return out


@router.get("/{system_type}")
def system_data(system_type: str, company_id: int,
                db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    """某业务系统的数据中心明细"""
    require_company(company_id, current)
    if system_type not in _VALID:
        raise HTTPException(status_code=400, detail="未知的系统类型")

    istatus = _integration_status(db, company_id, system_type)
    external_connected = istatus == "ok"

    if system_type == "crm":
        return _crm(db, company_id, istatus, external_connected)
    if system_type == "mes":
        return _mes(db, company_id, istatus, external_connected)
    if system_type == "qms":
        return _qms(db, company_id, istatus, external_connected)
    if system_type == "erp":
        return _erp(istatus, external_connected)

    raise HTTPException(status_code=400, detail="未知的系统类型")


# ============ CRM ============
def _crm(db, company_id, istatus, external_connected):
    customers = db.query(Customer).filter(Customer.company_id == company_id).all()
    quotations = db.query(Quotation).filter(Quotation.company_id == company_id).all()
    leads = db.query(Lead).filter(Lead.company_id == company_id).all()
    won = sum(1 for q in quotations if q.status == "accepted")
    pending = sum(1 for q in quotations if q.status in ("draft", "sent"))
    return {
        "system": "crm",
        "source": "platform",
        "integration_status": istatus,
        "external_connected": external_connected,
        "metrics": {
            "customers": len(customers),
            "quotations": len(quotations),
            "leads": len(leads),
            "won_orders": won,
            "pending_quotations": pending,
        },
        "recent_customers": _recent_customers(db, company_id),
    }


# ============ MES（生产详情） ============
def _mes(db, company_id, istatus, external_connected):
    orders = db.query(ProductionOrder).filter(
        ProductionOrder.company_id == company_id).all()
    if orders:
        avg_progress = round(sum(o.progress for o in orders) / len(orders), 1)
        delay = sum(1 for o in orders if o.delay)
        today_output = sum(o.quantity for o in orders if o.progress >= 100)
        return {
            "system": "mes",
            "source": "platform",
            "integration_status": istatus,
            "external_connected": external_connected,
            "metrics": {
                "work_orders": len(orders),
                "avg_progress": avg_progress,
                "delay_count": delay,
                "today_output": today_output,
            },
            "work_orders": [{
                "order_no": o.order_no,
                "product": o.product,
                "quantity": o.quantity,
                "progress": o.progress,
                "line": o.line,
                "load_rate": o.load_rate,
                "delay": bool(o.delay),
                "deadline_days": o.deadline_days,
            } for o in orders],
        }
    return _sample_mes(istatus, external_connected)


def _sample_mes(istatus, external_connected):
    return {
        "system": "mes",
        "source": "sample",
        "integration_status": istatus,
        "external_connected": external_connected,
        "note": "当前平台未采集到生产订单数据，以下为示例数据（玻璃仪器行业）。",
        "metrics": {
            "work_orders": 6, "avg_progress": 72.5, "delay_count": 1, "today_output": 2400,
        },
        "work_orders": [
            {"order_no": "WO-2026-001", "product": "烧杯 250ml", "quantity": 1000, "progress": 100, "line": "A01", "load_rate": 82, "delay": False, "deadline_days": 0},
            {"order_no": "WO-2026-002", "product": "量筒 100ml", "quantity": 800, "progress": 65, "line": "A02", "load_rate": 88, "delay": False, "deadline_days": 20},
            {"order_no": "WO-2026-003", "product": "容量瓶 500ml", "quantity": 500, "progress": 40, "line": "A01", "load_rate": 90, "delay": True, "deadline_days": 10},
            {"order_no": "WO-2026-004", "product": "试管 16x150", "quantity": 2000, "progress": 20, "line": "B01", "load_rate": 70, "delay": False, "deadline_days": 25},
            {"order_no": "WO-2026-005", "product": "滴管 玻璃", "quantity": 1500, "progress": 0, "line": "B02", "load_rate": 60, "delay": False, "deadline_days": 18},
            {"order_no": "WO-2026-006", "product": "培养皿 90mm", "quantity": 600, "progress": 100, "line": "A02", "load_rate": 78, "delay": False, "deadline_days": 0},
        ],
    }


# ============ QMS（质量详情） ============
def _qms(db, company_id, istatus, external_connected):
    reports = db.query(QualityReport).filter(
        QualityReport.company_id == company_id).all()
    if reports:
        avg_defect = round(sum(r.defect_rate for r in reports) / len(reports), 2)
        yield_rate = round(100 - avg_defect, 2)
        open_ncs = sum(1 for r in reports if r.defect_rate >= 5)
        return {
            "system": "qms",
            "source": "platform",
            "integration_status": istatus,
            "external_connected": external_connected,
            "metrics": {
                "inspections": len(reports),
                "avg_defect": avg_defect,
                "yield_rate": yield_rate,
                "open_ncs": open_ncs,
            },
            "reports": [{
                "report_date": r.report_date.isoformat() if r.report_date else "",
                "line": r.line,
                "defect_rate": r.defect_rate,
                "defect_type": r.defect_type,
                "total_count": r.total_count,
                "defect_count": r.defect_count,
            } for r in reports],
        }
    return _sample_qms(istatus, external_connected)


def _sample_qms(istatus, external_connected):
    return {
        "system": "qms",
        "source": "sample",
        "integration_status": istatus,
        "external_connected": external_connected,
        "note": "当前平台未采集到质量检验数据，以下为示例数据（玻璃仪器行业）。",
        "metrics": {
            "inspections": 6, "avg_defect": 1.42, "yield_rate": 98.58, "open_ncs": 0,
        },
        "reports": [
            {"report_date": (datetime.utcnow() - timedelta(days=6)).isoformat(), "line": "A01", "defect_rate": 1.2, "defect_type": "bubble", "total_count": 1000, "defect_count": 12},
            {"report_date": (datetime.utcnow() - timedelta(days=5)).isoformat(), "line": "A02", "defect_rate": 0.8, "defect_type": "crack", "total_count": 800, "defect_count": 6},
            {"report_date": (datetime.utcnow() - timedelta(days=4)).isoformat(), "line": "A01", "defect_rate": 2.5, "defect_type": "warp", "total_count": 500, "defect_count": 13},
            {"report_date": (datetime.utcnow() - timedelta(days=3)).isoformat(), "line": "B01", "defect_rate": 0.5, "defect_type": "none", "total_count": 2000, "defect_count": 10},
            {"report_date": (datetime.utcnow() - timedelta(days=2)).isoformat(), "line": "A02", "defect_rate": 3.2, "defect_type": "bubble", "total_count": 600, "defect_count": 19},
            {"report_date": (datetime.utcnow() - timedelta(days=1)).isoformat(), "line": "B02", "defect_rate": 0.3, "defect_type": "none", "total_count": 1500, "defect_count": 5},
        ],
    }


# ============ ERP ============
def _erp(istatus, external_connected):
    return {
        "system": "erp",
        "source": "sample",
        "integration_status": istatus,
        "external_connected": external_connected,
        "note": "当前平台未对接 ERP 数据模型，以下为示例数据（接入 ERP 后将展示实时财务/库存/采购数据）。",
        "metrics": {
            "inventory_value": 1280000,
            "open_po": 18,
            "ar_balance": 860000,
            "ap_balance": 540000,
        },
        "recent_docs": [
            {"doc_no": "PO-2026-0188", "type": "采购订单", "supplier": "鲁阳耐火材料", "amount": 124000, "status": "已审批"},
            {"doc_no": "SO-2026-0902", "type": "销售订单", "customer": "华东医药", "amount": 320000, "status": "已发货"},
            {"doc_no": "INV-2026-0331", "type": "发票", "customer": "康美药业", "amount": 88000, "status": "已开具"},
            {"doc_no": "GRN-2026-0145", "type": "入库单", "supplier": "信义玻璃", "amount": 56000, "status": "已入库"},
            {"doc_no": "PAY-2026-0077", "type": "付款单", "supplier": "鲁阳耐火材料", "amount": 124000, "status": "待付款"},
        ],
    }
