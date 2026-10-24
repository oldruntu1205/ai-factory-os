"""
api/audit.py —— AI 员工工具调用审计与回放 API

端点：
- GET /hermes/audit/tool-calls        查询调用台账（多条件过滤）
- GET /hermes/audit/replay/{session}  回放一次会话的完整调用链
- GET /hermes/audit/stats             近 N 天统计（失败率 / 平均耗时 / Top 工具）

用途：客户问"AI 为什么改了这个报价"，用 replay 把当时的每一步参数与结果调出来。
"""
from fastapi import APIRouter, Depends, HTTPException

from auth.deps import get_current_user, require_company
from hermes.audit import service as audit_service

router = APIRouter(prefix="/hermes/audit", tags=["Hermes Audit"])


@router.get("/tool-calls")
def list_tool_calls(company_id: int, agent_name: str = None, tool_name: str = None,
                    ok: bool = None, source: str = None,
                    since: float = None, until: float = None,
                    limit: int = 100, offset: int = 0,
                    current: dict = Depends(get_current_user)):
    """按租户查询工具调用台账（倒序）"""
    require_company(company_id, current)
    try:
        return audit_service.query_tool_calls(
            company_id, agent_name=agent_name, tool_name=tool_name, ok=ok,
            since=since, until=until, source=source, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")


@router.get("/replay/{session_id}")
def replay(session_id: str, company_id: int,
           current: dict = Depends(get_current_user)):
    """回放一次会话的完整调用链（正序时间线）"""
    require_company(company_id, current)
    data = audit_service.replay_session(company_id, session_id)
    if not data.get("steps"):
        raise HTTPException(status_code=404, detail="该会话无调用记录（或不属于当前企业）")
    return data


@router.get("/stats")
def stats(company_id: int, days: int = 7,
          current: dict = Depends(get_current_user)):
    """近 N 天调用统计——给老板看的那三个数：调了多少、失败多少、慢在哪"""
    require_company(company_id, current)
    return audit_service.stats(company_id, days=days)
