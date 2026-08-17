"""
api/hermes.py —— Hermes Agent Runtime API（落地手册第二篇）

- POST /hermes/agent/register    注册 Agent（动态加载 AI 员工）
- GET  /hermes/agent/list        发现 Agent（按行业/状态）
- GET  /hermes/agent/{name}      Agent 详情
- POST /hermes/execute           执行 Agent 任务（Hermes Runtime）
- POST /hermes/tool/call         Agent 调用 Tool（权限校验）
- GET  /hermes/tool/list         Tool 清单
- POST /hermes/task/enqueue      异步任务入队
- POST /hermes/task/process      同步处理一条任务
- GET  /hermes/memory/short      短期记忆
- POST /hermes/permissions       租户权限管理
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from auth.deps import get_current_user, require_company
from hermes.runtime import get_runtime
from hermes.registry.models import AgentManifest
from hermes.registry.soul import (read_soul, write_soul, reset_soul,
                                    list_company_souls, soul_status)
from hermes.tool.tools import BUILTIN_TOOLS
from api.company import require_verified

router = APIRouter(prefix="/hermes", tags=["Hermes Runtime"])


# ============ 请求模型 ============

class AgentRegisterIn(BaseModel):
    name: str
    version: str = "1.0"
    industry: str = "general"
    label: str = ""
    description: str = ""
    tools: list = []
    permissions: list = []


class ExecuteIn(BaseModel):
    agent_name: str
    message: str
    company_id: int
    session_id: str = None


class ToolCallIn(BaseModel):
    session_id: str
    tool_name: str
    params: dict = {}


class TaskIn(BaseModel):
    task_name: str
    payload: dict = {}


class PermissionIn(BaseModel):
    company_id: int
    action: str                 # grant / revoke / allow_agent / deny_agent
    permission: str = ""
    agent_name: str = ""


# ============ Agent 注册中心 ============

@router.post("/agent/register")
def register_agent(data: AgentRegisterIn, current: dict = Depends(get_current_user)):
    """注册 Agent（第5章）：动态加载 AI 员工"""
    if data.name in BUILTIN_TOOLS or not data.name:
        raise HTTPException(status_code=400, detail="Agent 名称不合法")
    manifest = AgentManifest(
        name=data.name,
        version=data.version,
        industry=data.industry,
        label=data.label or data.name,
        description=data.description,
        tools=data.tools,
        permissions=data.permissions,
    )
    # 关联内置 AI 员工实现（sales/customer/quotation 等角色直调）
    from agents.service import execute_specialized
    from database import SessionLocal
    from models import AIAgent

    def _generic_handler(message: str, runtime_ctx: dict):
        """通用 Agent 处理器：按 manifest 名匹配 AI 员工角色"""
        role_map = {
            "sales_agent": "sales",
            "customer_agent": "customer",
            "quotation_agent": "quotation",
            "production_agent": "production",
            "quality_agent": "quality",
            "ceo_agent": "ceo",
            "process_agent": "process",
        }
        role = role_map.get(data.name)
        if not role:
            return f"[{data.name}] Agent 已运行，暂无自定义逻辑（请注册 handler）"
        db = SessionLocal()
        try:
            agent = db.query(AIAgent).filter(
                AIAgent.role == role,
                AIAgent.company_id == runtime_ctx["company_id"]).first()
            if not agent:
                return "企业尚未创建对应 AI 员工，请先在 AI员工 页面创建"
            return execute_specialized(agent, message, runtime_ctx["company_id"],
                                       db, runtime_ctx.get("customer_id"),
                                       tools=runtime_ctx.get("tools"))
        finally:
            db.close()

    manifest.handler = _generic_handler
    reg = get_runtime().agent_registry
    reg.register(manifest)

    # 同步到权限系统
    perm_sys = get_runtime().permission_system
    perm_sys.allow_agent(current["company_id"], data.name)

    return {"message": "agent registered", "agent": manifest.to_dict()}


@router.get("/agent/list")
def list_agents(industry: str = None, current: dict = Depends(get_current_user)):
    """发现 Agent（第5章）"""
    reg = get_runtime().agent_registry
    agents = reg.list_agents(industry=industry)
    return {"agents": [a.to_dict() for a in agents], "count": len(agents)}


@router.get("/agent/{name}")
def get_agent(name: str, current: dict = Depends(get_current_user)):
    reg = get_runtime().agent_registry
    agent = reg.get_agent(name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 未注册")
    return agent.to_dict()


# ============ AI 员工 Soul（灵魂/系统提示）============

class SoulIn(BaseModel):
    content: str


@router.get("/agent/{name}/soul")
def get_soul(name: str, company_id: int, current: dict = Depends(get_current_user)):
    """读取生效 Soul（覆盖层 > 底座层 > 默认），并返回双层状态。"""
    require_company(company_id, current)
    content = read_soul(company_id, name)
    layers = soul_status(company_id, name)
    # is_custom / source 保持原语义（企业定制 / 平台默认），供前端徽章兼容
    is_custom = layers["override"] or layers["base"]
    source = "company" if layers["override"] else (
        "company" if layers["base"] else ("default" if content else "none"))
    return {
        "agent": name,
        "company_id": company_id,
        "content": content,
        "is_custom": is_custom,
        "source": source,
        "layers": layers,
        "size": len(content),
    }


@router.put("/agent/{name}/soul")
def set_soul(name: str, data: SoulIn, company_id: int,
             current: dict = Depends(get_current_user)):
    """写入企业级 Soul（写进 hermes/registry/souls/company_{id}/{agent}.md）"""
    require_company(company_id, current)
    if len(data.content) > 50000:
        raise HTTPException(status_code=400, detail="Soul 内容不能超过 50000 字符")
    result = write_soul(company_id, name, data.content)
    return {"message": "soul saved", **result}


@router.delete("/agent/{name}/soul")
def delete_soul(name: str, company_id: int,
                current: dict = Depends(get_current_user)):
    """清除企业定制 Soul（恢复默认）。若指纹变化，会一并隔离该 Agent 旧版本技能。"""
    require_company(company_id, current)
    res = reset_soul(company_id, name)
    if res.get("reset"):
        msg = "soul reset to default"
        if res.get("quarantined_skills"):
            msg += f"（已隔离 {res['quarantined_skills']} 个旧版本技能）"
        return {"message": msg, "quarantined_skills": res.get("quarantined_skills", 0)}
    return {"message": "no custom soul to reset", "quarantined_skills": 0}


@router.get("/souls")
def list_my_souls(company_id: int, current: dict = Depends(get_current_user)):
    """列出企业已定制的 Soul"""
    require_company(company_id, current)
    return {"souls": list_company_souls(company_id)}


# ============ Agent 执行 ============

@router.post("/execute")
def execute(data: ExecuteIn, current: dict = Depends(get_current_user)):
    """执行 Agent 任务（Hermes Runtime 编排，需企业已认证）"""
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))
    try:
        result = get_runtime().execute(
            agent_name=data.agent_name,
            message=data.message,
            tenant_id=current["company_id"],
            company_id=data.company_id,
            session_id=data.session_id,
        )
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ============ Tool 系统 ============

@router.get("/tool/list")
def list_tools(current: dict = Depends(get_current_user)):
    """Tool 清单（第6章）"""
    center = get_runtime().tool_center
    return {"tools": center.list_tools(), "count": len(BUILTIN_TOOLS)}


@router.post("/tool/call")
def call_tool(data: ToolCallIn, current: dict = Depends(get_current_user)):
    """Agent 调用 Tool（Tool Router + Permission Check）"""
    try:
        result = get_runtime().call_tool(
            session_id=data.session_id,
            tool_name=data.tool_name,
            params=data.params,
            actor="agent",
        )
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ============ 消息队列 ============

@router.post("/task/enqueue")
def enqueue_task(data: TaskIn, current: dict = Depends(get_current_user)):
    """异步任务入队（第7章）：如每日扫描客户"""
    result = get_runtime().dispatch(data.task_name, data.payload)
    return result


@router.post("/task/process")
def process_task(current: dict = Depends(get_current_user)):
    """同步处理一条队列任务（演示用；生产用 Celery Worker）"""
    result = get_runtime().queue.process_once()
    return result


# ============ 权限管理 ============

@router.post("/permissions")
def manage_permissions(data: PermissionIn, current: dict = Depends(get_current_user)):
    """租户权限管理（第8章 RBAC）"""
    require_company(data.company_id, current)
    perm_sys = get_runtime().permission_system
    if data.action == "grant":
        perm_sys.grant_permission(data.company_id, data.permission)
    elif data.action == "revoke":
        perm_sys.revoke_permission(data.company_id, data.permission)
    elif data.action == "allow_agent":
        perm_sys.allow_agent(data.company_id, data.agent_name)
    elif data.action == "deny_agent":
        perm_sys.deny_agent(data.company_id, data.agent_name)
    else:
        raise HTTPException(status_code=400, detail="未知操作")
    return {"message": "permission updated"}


# ============ Memory ============

@router.get("/memory/short")
def short_memory(session_id: str, current: dict = Depends(get_current_user)):
    """短期记忆（第9章）：当前对话上下文"""
    memory = get_runtime().memory_system.load_short_term_memory(session_id, limit=20)
    return {"session_id": session_id, "entries": memory, "count": len(memory)}
