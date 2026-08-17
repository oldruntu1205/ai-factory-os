"""
api/marketplace.py —— Agent 行业商城（落地手册第24章）

- GET  /marketplace/packages         浏览商城（按行业/关键词）
- GET  /marketplace/packages/{id}    包详情
- POST /marketplace/install          安装到企业（创建 AI 员工 + 注册 Hermes Agent + 授权 Tool）
- POST /marketplace/uninstall        卸载
- GET  /marketplace/installed        企业已安装列表
- GET  /marketplace/industries       行业分类

安装流程：
1. 校验包存在且已发布
2. 为包内每个 Agent 创建企业 AIAgent（若不存在）
3. 注册到 Hermes Runtime（agent_registry）
4. 授权 Tool（permission_system）
5. 记录 CompanyPackageInstall，包安装计数 +1
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db, SessionLocal
from models import AgentPackage, CompanyPackageInstall, AIAgent, Company
from auth.deps import get_current_user, require_company
from constants.industries import INDUSTRY_NAME_BY_CODE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplace", tags=["Agent商城"])


def _resolve_industry_name(company_id: int, fallback_code: str = "") -> str:
    """根据企业选定的 industry 翻译行业代码为中文名；用于商城包 title 占位符替换。"""
    if not company_id:
        return fallback_code or "行业"
    try:
        with SessionLocal() as db:
            co = db.query(Company).filter(Company.id == company_id).first()
            if co and co.industry:
                return INDUSTRY_NAME_BY_CODE.get(co.industry, co.industry)
    except Exception:
        pass
    return fallback_code or "行业"


class InstallIn(BaseModel):
    company_id: int
    package_id: int


# ============ 浏览 ============

@router.get("/packages")
def list_packages(industry: str = None, keyword: str = None,
                  db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """浏览商城（第24章）：按行业 / 关键词筛选；
    title/description 中的 {INDUSTRY} 占位符按当前企业所选行业动态替换。"""
    query = db.query(AgentPackage).filter(AgentPackage.status == "published")
    if industry and industry != "all":
        query = query.filter(AgentPackage.industry == industry)
    if keyword:
        query = query.filter(AgentPackage.title.like(f"%{keyword}%"))
    packages = query.order_by(AgentPackage.install_count.desc()).all()

    # 标记当前企业已安装
    installed_ids = {i.package_id for i in db.query(CompanyPackageInstall).filter(
        CompanyPackageInstall.company_id == current["company_id"],
        CompanyPackageInstall.status == "installed").all()}

    industry_name = _resolve_industry_name(current["company_id"])

    # 当前企业 AI 员工席位占用（两层模型：技能包在席位内安装）
    co = db.query(Company).filter(Company.id == current["company_id"]).first()
    used_agents = db.query(AIAgent).filter(AIAgent.company_id == current["company_id"]).count()
    max_agents = co.max_agents if co else 0
    company_seats = {
        "max": max_agents,
        "used": used_agents,
        "remaining": max(max_agents - used_agents, 0),
    }

    return {"packages": [{
        "id": p.id, "name": p.name,
        "title": (p.title or "").replace("{INDUSTRY}", industry_name),
        "industry": p.industry, "industry_name": industry_name,
        "version": p.version,
        "description": (p.description or "").replace("{INDUSTRY}", industry_name),
        "price": p.price,
        "icon": p.icon, "agent_count": len(p.agents or []),
        "tools": p.tools or [], "install_count": p.install_count,
        "installed": p.id in installed_ids,
    } for p in packages], "count": len(packages), "company_seats": company_seats}


@router.get("/packages/{package_id}")
def package_detail(package_id: int, db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """包详情：含完整 Agent 清单（title/description 占位符替换）"""
    p = db.query(AgentPackage).filter(AgentPackage.id == package_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="包不存在")
    industry_name = _resolve_industry_name(current["company_id"])
    return {
        "id": p.id, "name": p.name,
        "title": (p.title or "").replace("{INDUSTRY}", industry_name),
        "industry": p.industry, "industry_name": industry_name,
        "version": p.version,
        "description": (p.description or "").replace("{INDUSTRY}", industry_name),
        "price": p.price, "icon": p.icon,
        "agents": p.agents or [], "tools": p.tools or [],
        "install_count": p.install_count,
    }


@router.get("/industries")
def list_industries(db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """行业分类：返回 [{code, name}] 列表，只含当前企业所选行业 + 通用"""
    # 当前企业行业
    company_code = ""
    try:
        with SessionLocal() as db_c:
            co = db_c.query(Company).filter(Company.id == current["company_id"]).first()
            company_code = (co.industry if co else "") or ""
    except Exception:
        pass
    out = []
    if company_code and company_code in INDUSTRY_NAME_BY_CODE:
        out.append({"code": company_code, "name": INDUSTRY_NAME_BY_CODE[company_code]})
    out.append({"code": "general", "name": "通用"})
    return {"industries": out, "current_industry": company_code,
            "current_industry_name": INDUSTRY_NAME_BY_CODE.get(company_code, company_code)}


# ============ 安装 / 卸载 ============

def _install_package_to_hermes(company_id: int, package: AgentPackage):
    """把包内 Agent 注册到 Hermes Runtime 并授权 Tool"""
    from hermes.runtime import get_runtime
    from hermes.registry.models import AgentManifest

    runtime = get_runtime()
    role_map = {"customer": "customer_agent", "sales": "sales_agent",
                "quotation": "quotation_agent", "production": "production_agent",
                "quality": "quality_agent", "purchasing": "purchasing_agent",
                "ceo": "ceo_agent", "process": "process_agent"}

    for agent in (package.agents or []):
        manifest_name = role_map.get(agent.get("role"), f"{agent.get('role', 'custom')}_agent")
        # 注册平台级 Agent（若未注册）
        if not runtime.agent_registry.get_agent(manifest_name):
            manifest = AgentManifest(
                name=manifest_name, version=package.version,
                industry=package.industry,
                label=agent.get("label", manifest_name),
                description=agent.get("description", ""),
                tools=package.tools or [],
            )
            runtime.agent_registry.register(manifest)
        # 租户授权
        runtime.permission_system.allow_agent(company_id, manifest_name)
        for tool in (package.tools or []):
            tool_perm = _tool_permission_of(tool)
            if tool_perm:
                runtime.permission_system.grant_permission(company_id, tool_perm)


def _tool_permission_of(tool_name: str) -> str:
    """tool -> 权限点映射"""
    mapping = {
        "crm.get_customer": "crm:read",
        "crm.create_customer": "crm:write",
        "email.send": "email:send",
        "quotation.create": "quotation:write",
        "knowledge.search": "knowledge:read",
        "erp.get_stock": "erp:read",
        "mes.get_production": "mes:read",
        "dashboard.snapshot": "dashboard:read",
    }
    return mapping.get(tool_name, "")


@router.post("/install")
def install_package(data: InstallIn, db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """安装包到企业（第24章）：一键部署整套行业 AI 员工"""
    require_company(data.company_id, current)
    p = db.query(AgentPackage).filter(AgentPackage.id == data.package_id).first()
    if not p or p.status != "published":
        raise HTTPException(status_code=404, detail="包不存在或未发布")

    existing = db.query(CompanyPackageInstall).filter(
        CompanyPackageInstall.company_id == data.company_id,
        CompanyPackageInstall.package_id == p.id,
        CompanyPackageInstall.status == "installed").first()
    if existing:
        raise HTTPException(status_code=400, detail="该包已安装")

    # 0. 席位容量校验（两层模型：技能包在底座套餐的 AI 员工席位内安装）
    company = db.query(Company).filter(Company.id == data.company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    needed = len(p.agents or [])
    used_agents = db.query(AIAgent).filter(AIAgent.company_id == data.company_id).count()
    remaining = company.max_agents - used_agents
    if needed > remaining:
        raise HTTPException(
            status_code=400,
            detail=(f"当前底座套餐仅剩 {max(remaining, 0)} 个 AI 员工席位，"
                    f"安装「{p.title}」需 {needed} 个，请先升级底座套餐或卸载其他技能包"))

    # 1. 创建企业 AI 员工（每个包内 Agent）
    created_agents = []
    for agent in (p.agents or []):
        exists = db.query(AIAgent).filter(
            AIAgent.role == agent.get("role"),
            AIAgent.company_id == data.company_id).first()
        if exists:
            created_agents.append({"id": exists.id, "name": exists.name, "role": exists.role})
            continue
        new_agent = AIAgent(
            name=agent.get("name", agent.get("label", "")),
            role=agent.get("role", "custom"),
            role_label=agent.get("label", agent.get("role", "")),
            description=agent.get("description", ""),
            company_id=data.company_id,
        )
        db.add(new_agent)
        db.commit()
        db.refresh(new_agent)
        created_agents.append({"id": new_agent.id, "name": new_agent.name,
                               "role": new_agent.role})

    # 2. 注册到 Hermes Runtime + 授权
    _install_package_to_hermes(data.company_id, p)

    # 3. 记录安装
    install = CompanyPackageInstall(
        company_id=data.company_id, package_id=p.id,
        package_name=p.title, status="installed")
    db.add(install)
    p.install_count = (p.install_count or 0) + 1
    db.commit()

    logger.info(f"Marketplace: 企业 {data.company_id} 安装 [{p.title}]")
    return {"message": "install success", "package": p.title,
            "agents": created_agents, "tools": p.tools or []}


@router.post("/uninstall")
def uninstall_package(data: InstallIn, db: Session = Depends(get_db),
                      current: dict = Depends(get_current_user)):
    """卸载包：停用包内 AI 员工 + 移除授权记录"""
    require_company(data.company_id, current)
    install = db.query(CompanyPackageInstall).filter(
        CompanyPackageInstall.company_id == data.company_id,
        CompanyPackageInstall.package_id == data.package_id,
        CompanyPackageInstall.status == "installed").first()
    if not install:
        raise HTTPException(status_code=404, detail="未安装该包")

    p = db.query(AgentPackage).filter(AgentPackage.id == data.package_id).first()
    # 停用包内 AI 员工
    if p:
        for agent in (p.agents or []):
            db.query(AIAgent).filter(
                AIAgent.role == agent.get("role"),
                AIAgent.company_id == data.company_id).update(
                {"status": "disabled"})
        # 移除 Hermes 授权
        from hermes.runtime import get_runtime
        runtime = get_runtime()
        role_map = {"customer": "customer_agent", "sales": "sales_agent",
                    "quotation": "quotation_agent", "production": "production_agent",
                    "quality": "quality_agent", "purchasing": "purchasing_agent",
                    "ceo": "ceo_agent", "process": "process_agent"}
        for agent in (p.agents or []):
            manifest_name = role_map.get(agent.get("role"), f"{agent.get('role', 'custom')}_agent")
            runtime.permission_system.deny_agent(data.company_id, manifest_name)

    install.status = "uninstalled"
    if p and p.install_count > 0:
        p.install_count -= 1
    db.commit()
    return {"message": "uninstall success", "package": install.package_name}


# ============ 已安装 ============

@router.get("/installed")
def installed_packages(company_id: int, db: Session = Depends(get_db),
                       current: dict = Depends(get_current_user)):
    """企业已安装包"""
    require_company(company_id, current)
    installs = db.query(CompanyPackageInstall).filter(
        CompanyPackageInstall.company_id == company_id,
        CompanyPackageInstall.status == "installed").all()
    result = []
    for ins in installs:
        p = db.query(AgentPackage).filter(AgentPackage.id == ins.package_id).first()
        result.append({
            "package_id": ins.package_id,
            "package_name": ins.package_name,
            "industry": p.industry if p else "",
            "icon": p.icon if p else "🧩",
            "agent_count": len(p.agents or []) if p else 0,
            "price": p.price if p else 0,
            "installed_at": ins.created_at.isoformat(),
        })
    return {"installed": result, "count": len(result)}
