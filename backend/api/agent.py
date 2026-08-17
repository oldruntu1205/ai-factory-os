"""
api/agent.py —— AI 员工管理接口（第十二章）

多租户：所有操作强制校验 company_id 归属当前登录用户企业。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AIAgent, Company
from auth.deps import get_current_user, require_company
from constants.status import AGENT_STATUS

router = APIRouter(prefix="/agent", tags=["AI员工"])

ROLE_LABELS = {
    "customer": "AI客服",
    "sales": "AI销售",
    "quotation": "AI报价",
    "production": "AI生产计划",
    "quality": "AI质量工程师",
    "purchasing": "AI采购",
    "process": "工艺/图纸助手",
    "ceo": "CEO企业大脑",
}


class AgentIn(BaseModel):
    name: str
    role: str
    description: str = ""
    avatar: str = ""
    company_id: int


@router.post("/create")
def create_agent(data: AgentIn, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """创建 AI 员工"""
    require_company(data.company_id, current)

    company = db.query(Company).filter(Company.id == data.company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    if data.role not in ROLE_LABELS:
        raise HTTPException(status_code=400, detail=f"未知角色，可选: {list(ROLE_LABELS)}")

    # 套餐限制
    agent_count = db.query(AIAgent).filter(AIAgent.company_id == data.company_id).count()
    if agent_count >= company.max_agents:
        raise HTTPException(status_code=400, detail=f"当前套餐最多创建 {company.max_agents} 个 AI 员工，请升级套餐")

    agent = AIAgent(
        name=data.name,
        role=data.role,
        role_label=ROLE_LABELS[data.role],
        description=data.description,
        avatar=data.avatar,
        company_id=data.company_id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    # 安全修复（P2 #3）：创建后同步 Hermes RBAC Agent 白名单（业务 AIAgent 与 Hermes
    # manifest 对齐，防止未来 Hermes 路径校验时被白名单拒绝）
    try:
        from hermes.runtime import get_runtime
        _role_manifest = {"customer": "customer_agent", "sales": "sales_agent",
                          "quotation": "quotation_agent", "production": "production_agent",
                          "quality": "quality_agent", "ceo": "ceo_agent",
                          "process": "process_agent", "purchasing": "purchasing_agent"}
        manifest_name = _role_manifest.get(data.role)
        if manifest_name:
            get_runtime().permission_system.allow_agent(data.company_id, manifest_name)
    except Exception:
        pass  # runtime 未初始化（纯 DB 测试场景）不阻塞创建

    return {"message": "create success", "agent": {
        "id": agent.id, "name": agent.name, "role": agent.role,
        "role_label": agent.role_label, "description": agent.description,
        "status": agent.status,
    }}


@router.get("/list")
def list_agents(company_id: int, db: Session = Depends(get_db),
                current: dict = Depends(get_current_user)):
    """列出企业全部 AI 员工（多租户隔离）"""
    require_company(company_id, current)
    agents = db.query(AIAgent).filter(
        AIAgent.company_id == company_id).order_by(AIAgent.id).all()
    return {"agents": [{
        "id": a.id, "name": a.name, "role": a.role,
        "role_label": a.role_label, "description": a.description,
        "avatar": a.avatar, "status": a.status,
    } for a in agents]}


@router.delete("/{agent_id}")
def delete_agent(agent_id: int, company_id: int, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """删除 AI 员工"""
    require_company(company_id, current)
    agent = db.query(AIAgent).filter(
        AIAgent.id == agent_id, AIAgent.company_id == company_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="AI 员工不存在")
    db.delete(agent)
    db.commit()
    return {"message": "delete success"}


@router.put("/{agent_id}")
def update_agent(agent_id: int, data: dict, company_id: int,
                 db: Session = Depends(get_db), current: dict = Depends(get_current_user)):
    """更新 AI 员工（改名/停用等）"""
    require_company(company_id, current)
    agent = db.query(AIAgent).filter(
        AIAgent.id == agent_id, AIAgent.company_id == company_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="AI 员工不存在")
    if "name" in data:
        agent.name = data["name"]
    if "description" in data:
        agent.description = data["description"]
    if "status" in data:
        if data["status"] not in AGENT_STATUS:
            raise HTTPException(status_code=400, detail=f"状态无效，可选: {sorted(AGENT_STATUS)}")
        agent.status = data["status"]
    db.commit()
    return {"message": "update success"}
