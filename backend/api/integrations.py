"""
api/integrations.py —— 业务系统对接管理（平台超管）

为企业配置并对接其自有的 CRM / ERP / MES / QMS 系统：
- GET  /integrations/systems              4 类业务系统的定义（key/名称/图标/说明）
- GET  /integrations?company_id=          某企业的全部对接配置（平台超管）
- PUT  /integrations/{system_type}        新增/更新某企业的某系统对接配置
- POST /integrations/{system_type}/test   测试与该系统的连接

说明：凭证（api_key / password）当前以明文落库，生产环境应加密存储；
      连接测试为轻量级探测（GET + 超时），用于校验地址与鉴权是否可用。
"""
import ssl
import base64
import urllib.request
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import IntegrationConfig, Company
from auth.deps import get_current_user

router = APIRouter(prefix="/integrations", tags=["业务系统对接"])

# 4 类业务系统定义（静态元数据）
SYSTEM_DEFINITIONS = [
    {
        "key": "crm", "name": "CRM 客户关系管理", "icon": "📇",
        "desc": "对接客户、商机、订单与跟进数据，让 AI 销售/客服员工实时读取客户全景。",
    },
    {
        "key": "erp", "name": "ERP 企业资源计划", "icon": "🧾",
        "desc": "对接财务、采购、库存与主数据，支撑报价成本核算与经营分析。",
    },
    {
        "key": "mes", "name": "MES 制造执行系统", "icon": "🏭",
        "desc": "对接生产工单、设备状态与产量实绩，支撑 AI 生产排程与延期预警。",
    },
    {
        "key": "qms", "name": "QMS 质量管理系统", "icon": "✅",
        "desc": "对接检验记录、不良率与合规文档，支撑 AI 质量根因分析与整改。",
    },
]

_VALID_TYPES = {s["key"] for s in SYSTEM_DEFINITIONS}
_AUTH_TYPES = {"none", "basic", "apikey", "bearer"}


def _is_platform(current: dict) -> bool:
    return current.get("role") == "platform_admin"


def _target_company_id(payload_company_id: int | None, current: dict) -> int:
    """解析并校验企业 ID：超管可操作任意企业，企业用户只能操作本企业"""
    if _is_platform(current):
        if not payload_company_id:
            raise HTTPException(status_code=400, detail="需要指定 company_id")
        return int(payload_company_id)
    # 企业用户强制使用自身 company_id
    return int(current["company_id"])


def _ensure_company_access(company_id: int, current: dict):
    if not _is_platform(current) and int(company_id) != int(current["company_id"]):
        raise HTTPException(status_code=403, detail="无权访问其他企业的数据")


# ============ Pydantic ============

class IntegrationIn(BaseModel):
    company_id: int
    enabled: bool = False
    endpoint: str = ""
    auth_type: str = "none"
    api_key: str = ""
    username: str = ""
    password: str = ""
    extra: dict = {}


# ============ 内部工具 ============

def _row_to_dict(row: IntegrationConfig) -> dict:
    return {
        "id": row.id,
        "company_id": row.company_id,
        "system_type": row.system_type,
        "enabled": bool(row.enabled),
        "endpoint": row.endpoint or "",
        "auth_type": row.auth_type or "none",
        "api_key": row.api_key or "",
        "username": row.username or "",
        "password": row.password or "",
        "extra": row.extra or {},
        "status": row.status or "unconfigured",
        "last_error": row.last_error or "",
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _build_headers(cfg: IntegrationConfig) -> dict:
    headers = {"User-Agent": "AI-Factory-Integration/1.0"}
    at = cfg.auth_type or "none"
    if at == "apikey" and cfg.api_key:
        headers["X-API-Key"] = cfg.api_key
    elif at == "bearer" and cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    elif at == "basic" and (cfg.username or cfg.password):
        token = base64.b64encode(
            f"{cfg.username or ''}:{cfg.password or ''}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


def _test_connection(cfg: IntegrationConfig):
    """轻量级连接探测，返回 (ok, message)"""
    if not cfg.endpoint:
        return False, "未配置访问地址"
    try:
        req = urllib.request.Request(
            cfg.endpoint, headers=_build_headers(cfg), method="GET")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            return True, f"连接成功（HTTP {resp.status}）"
    except Exception as e:  # noqa: BLE001 - 连接测试需捕获所有异常
        return False, str(e)[:300]


# ============ 接口 ============

@router.get("/systems")
def list_systems(current: dict = Depends(get_current_user)):
    """4 类业务系统定义（任何登录用户均可查看）"""
    return {"systems": SYSTEM_DEFINITIONS}


@router.get("")
def list_integrations(company_id: int | None = None,
                      db: Session = Depends(get_db),
                      current: dict = Depends(get_current_user)):
    """某企业全部对接配置；超管可指定 company_id，企业用户只能查看本企业"""
    query = db.query(IntegrationConfig)
    if _is_platform(current):
        if company_id is not None:
            query = query.filter(IntegrationConfig.company_id == company_id)
    else:
        query = query.filter(IntegrationConfig.company_id == current["company_id"])
    rows = query.order_by(IntegrationConfig.company_id, IntegrationConfig.system_type).all()
    return {"integrations": [_row_to_dict(r) for r in rows]}


@router.get("/overview")
def integration_overview(db: Session = Depends(get_db),
                         current: dict = Depends(get_current_user)):
    """平台超管：查看所有企业的对接汇总（只读）"""
    if not _is_platform(current):
        raise HTTPException(status_code=403, detail="需要平台管理员权限")

    rows = db.query(IntegrationConfig).all()
    companies = {c.id: c.name for c in db.query(Company.id, Company.name).all()}

    grouped = {}
    for r in rows:
        cid = r.company_id
        if cid not in grouped:
            grouped[cid] = {"systems": [], "connected_count": 0}
        grouped[cid]["systems"].append(r.system_type)
        if r.status == "ok":
            grouped[cid]["connected_count"] += 1

    overview = []
    for cid, info in grouped.items():
        overview.append({
            "company_id": cid,
            "company_name": companies.get(cid, "未知企业"),
            "systems": info["systems"],
            "connected_count": info["connected_count"],
            "total_count": len(info["systems"]),
        })

    # 按已连通数降序，方便查看活跃企业
    overview.sort(key=lambda x: x["connected_count"], reverse=True)
    return {"overview": overview}


@router.put("/{system_type}")
def upsert_integration(system_type: str, payload: IntegrationIn,
                       db: Session = Depends(get_db),
                       current: dict = Depends(get_current_user)):
    """新增/更新某企业的某系统对接配置；超管可指定任意企业，企业用户仅可操作本企业"""
    if system_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="未知的系统类型")
    if payload.auth_type not in _AUTH_TYPES:
        raise HTTPException(status_code=400, detail="未知的鉴权类型")

    target_cid = _target_company_id(payload.company_id, current)

    company = db.query(Company).filter(Company.id == target_cid).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")

    row = db.query(IntegrationConfig).filter(
        IntegrationConfig.company_id == target_cid,
        IntegrationConfig.system_type == system_type).first()
    if not row:
        row = IntegrationConfig(
            company_id=target_cid, system_type=system_type)
        db.add(row)

    row.enabled = payload.enabled
    row.endpoint = payload.endpoint.strip()
    row.auth_type = payload.auth_type
    row.api_key = payload.api_key
    row.username = payload.username
    row.password = payload.password
    row.extra = payload.extra or {}
    # 保存时按是否配置地址推导状态（实际连通性以 test 为准）
    if not row.endpoint:
        row.status = "unconfigured"
    elif row.status not in ("ok", "error"):
        row.status = "configured"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "对接配置已保存", "integration": _row_to_dict(row)}


@router.post("/{system_type}/test")
def test_integration(system_type: str, payload: IntegrationIn,
                     db: Session = Depends(get_db),
                     current: dict = Depends(get_current_user)):
    """测试与某系统的连接，并更新状态；权限同 upsert"""
    if system_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="未知的系统类型")

    target_cid = _target_company_id(payload.company_id, current)

    company = db.query(Company).filter(Company.id == target_cid).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")

    # 先持久化最新配置，再据此测试
    row = db.query(IntegrationConfig).filter(
        IntegrationConfig.company_id == target_cid,
        IntegrationConfig.system_type == system_type).first()
    if not row:
        row = IntegrationConfig(
            company_id=target_cid, system_type=system_type)
        db.add(row)
    row.enabled = payload.enabled
    row.endpoint = payload.endpoint.strip()
    row.auth_type = payload.auth_type
    row.api_key = payload.api_key
    row.username = payload.username
    row.password = payload.password
    row.extra = payload.extra or {}
    row.updated_at = datetime.utcnow()

    ok, message = _test_connection(row)
    row.status = "ok" if ok else "error"
    row.last_error = "" if ok else message
    row.last_tested_at = datetime.utcnow()
    db.commit()
    return {
        "ok": ok,
        "message": message,
        "integration": _row_to_dict(row),
    }
