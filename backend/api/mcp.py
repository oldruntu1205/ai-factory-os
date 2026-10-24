"""
api/mcp.py —— MCP Gateway 接入端点（JSON-RPC over HTTP）

端点：
- POST /mcp                        JSON-RPC（initialize / tools/list / tools/call / ping）
- GET  /mcp/tools                  工具清单（REST，便于非 MCP 客户端与调试）
- POST /mcp/tools/{tool}/call      单工具调用（REST）
- GET  /mcp/health                 健康检查（无需鉴权）

鉴权（两种 Bearer 都支持）：
1. 企业 API Key（api_keys 表，MCP 客户端推荐用这个）
2. 用户 JWT（内部工具/前端调试）

请求头：
- Authorization: Bearer <key|jwt>    必需
- X-Hermes-Agent: <agent_name>       可选，指定以哪个 AI 员工的权限集调用
- Mcp-Session-Id: <id>              可选，响应会回显；缺省则新建
- X-Company-Id: <id>                仅 platform_admin 需要（指定代管企业）

安全默认：
- 写工具默认关闭（MCP_ALLOW_WRITE=1 才放行）
- 所有调用经 RBAC 校验并写审计台账
"""
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import MCP_ALLOW_WRITE, MCP_ENABLED
from database import get_db
from models import ApiKey, User
from auth.jwt import decode_token
from hermes.mcp.gateway import get_gateway, McpError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["Hermes MCP Gateway"])


def _resolve_caller(authorization: str, x_company_id: str, db: Session) -> dict:
    """解析调用方：企业 API Key 优先，其次用户 JWT。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少认证令牌")
    token = authorization.split(" ", 1)[1].strip()

    # 1) 企业 API Key
    key = db.query(ApiKey).filter(ApiKey.key == token).first()
    if key:
        if not key.enabled:
            raise HTTPException(status_code=403, detail="API Key 已停用")
        return {"company_id": int(key.company_id or 0),
                "role": "api_key", "actor": f"api_key:{key.name or key.id}",
                "auth": "api_key"}

    # 2) 用户 JWT
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if getattr(user, "status", "active") == "disabled":
        raise HTTPException(status_code=403, detail="账号已被停用")

    if user.role == "platform_admin":
        if not x_company_id:
            raise HTTPException(status_code=400,
                                detail="平台超管调用 MCP 需通过 X-Company-Id 指定企业")
        return {"company_id": int(x_company_id), "role": "platform_admin",
                "actor": f"platform_admin:{user.id}", "auth": "jwt"}

    return {"company_id": int(user.company_id or 0), "role": user.role,
            "actor": f"user:{user.id}", "auth": "jwt"}


def _ctx(caller: dict, x_agent: str, session_id: str, request: Request) -> dict:
    return {
        "company_id": caller["company_id"],
        "agent_name": (x_agent or "").strip() or None,
        "actor": caller.get("actor", "mcp"),
        "session_id": session_id,
        "ip": request.client.host if request.client else "",
        "allow_write": MCP_ALLOW_WRITE,
        "source": "mcp",
    }


def _guard_enabled() -> None:
    if not MCP_ENABLED:
        raise HTTPException(status_code=403, detail="MCP Gateway 已关闭（MCP_ENABLED=0）")


# ============ JSON-RPC 主端点 ============

@router.post("")
async def mcp_rpc(request: Request,
                  authorization: str = Header(default=""),
                  x_company_id: str = Header(default=""),
                  x_hermes_agent: str = Header(default=""),
                  mcp_session_id: str = Header(default=""),
                  db: Session = Depends(get_db)):
    """MCP JSON-RPC 端点（支持单条与批量）。"""
    _guard_enabled()
    caller = _resolve_caller(authorization, x_company_id, db)
    if not caller["company_id"]:
        raise HTTPException(status_code=400, detail="无法确定企业上下文")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    session_id = mcp_session_id or uuid.uuid4().hex
    ctx = _ctx(caller, x_hermes_agent, session_id, request)
    gateway = get_gateway()

    if isinstance(body, list):
        responses = [r for r in (gateway.handle_rpc(item, ctx) for item in body) if r]
        payload = responses
    else:
        payload = gateway.handle_rpc(body, ctx)

    # 通知类消息（无 id）按协议返回 202 无正文
    if payload is None:
        return _accepted(session_id)

    from fastapi.responses import JSONResponse
    return JSONResponse(content=payload, headers={"Mcp-Session-Id": session_id})


def _accepted(session_id: str):
    from fastapi.responses import Response
    return Response(status_code=202, headers={"Mcp-Session-Id": session_id})


# ============ REST 便捷端点 ============

@router.get("/health")
def health():
    """健康检查（无需鉴权，便于探活与负载均衡）"""
    return {"status": "ok" if MCP_ENABLED else "disabled",
            "server": "hermes-mcp", "write_enabled": MCP_ALLOW_WRITE}


@router.get("/tools")
def list_tools(authorization: str = Header(default=""),
               x_company_id: str = Header(default=""),
               x_hermes_agent: str = Header(default=""),
               db: Session = Depends(get_db)):
    """工具清单（等同 tools/list，便于 curl 调试）"""
    _guard_enabled()
    caller = _resolve_caller(authorization, x_company_id, db)
    tools = get_gateway().list_tools(caller["company_id"],
                                    (x_hermes_agent or "").strip() or None,
                                    allow_write=MCP_ALLOW_WRITE)
    return {"company_id": caller["company_id"], "count": len(tools), "tools": tools}


class ToolCallBody(BaseModel):
    arguments: dict = {}


@router.post("/tools/{tool_name}/call")
def call_tool(tool_name: str, body: ToolCallBody, request: Request,
              authorization: str = Header(default=""),
              x_company_id: str = Header(default=""),
              x_hermes_agent: str = Header(default=""),
              mcp_session_id: str = Header(default=""),
              db: Session = Depends(get_db)):
    """单工具调用（REST 版 tools/call）"""
    _guard_enabled()
    caller = _resolve_caller(authorization, x_company_id, db)
    session_id = mcp_session_id or uuid.uuid4().hex
    try:
        return get_gateway().call_tool(
            caller["company_id"], tool_name, body.arguments or {},
            agent_name=(x_hermes_agent or "").strip() or None,
            actor=caller.get("actor", "mcp"), session_id=session_id,
            ip=request.client.host if request.client else "",
            allow_write=MCP_ALLOW_WRITE, source="mcp")
    except McpError as e:
        raise HTTPException(status_code=400 if e.code != -32603 else 500, detail=e.message)
