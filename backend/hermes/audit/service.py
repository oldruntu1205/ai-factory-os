"""
hermes/audit/service.py —— 工具调用审计与回放服务（第8章 可治理）

为什么单独做一张表：
TraceRecorder 的轨迹是给"自进化"用的（重点在步骤与质量分），
审计台账是给"客户与合规"用的（重点在谁、何时、动了什么、能不能查回来）。
两者目的不同，不能共用。

三条硬约束：
1. 审计失败不阻断业务：所有写入都包 try/except，只记日志。
2. 参数先脱敏再入库：token / password / secret / authorization 一律替换为 ***。
3. 与会话强绑定：session_id 可把同一轮任务的多次调用串成一条可回放的时间线。
"""
import hashlib
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 命中即脱敏的参数键（子串匹配，小写）
_SENSITIVE_HINTS = ("token", "password", "passwd", "secret", "authorization",
                    "api_key", "apikey", "access_key", "private_key")
_EXCERPT_LIMIT = 500


def mask_params(params) -> dict:
    """参数脱敏：敏感键值替换为 ***，其余保留（截断超长值）。"""
    if not isinstance(params, dict):
        return {"_raw": str(params)[:200]}
    out = {}
    for k, v in params.items():
        key = str(k).lower()
        if any(h in key for h in _SENSITIVE_HINTS):
            out[k] = "***"
        elif isinstance(v, str):
            out[k] = v[:200]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)[:200]
    return out


def params_hash(params) -> str:
    """参数指纹：用于"同参数重复调用"排查与去重统计。"""
    try:
        canon = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        canon = str(params)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _excerpt(result, limit: int = _EXCERPT_LIMIT) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        if result.get("ok") is False:
            return f"ERROR: {result.get('error', '')}"[:limit]
        data = result.get("data")
        return str(data if data is not None else result)[:limit]
    return str(result)[:limit]


def record_tool_call(company_id: int, tool_name: str, params: dict,
                     result: dict, latency_ms: float,
                     agent_name: str = "", actor: str = "agent",
                     source: str = "", session_id: Optional[str] = None,
                     ip: str = "") -> None:
    """写一条工具调用审计。任何异常都被吞掉，只记 warning。"""
    try:
        from database import SessionLocal
        from models import ToolCallAudit

        ok = bool(result.get("ok")) if isinstance(result, dict) else None
        err = ""
        if isinstance(result, dict) and result.get("ok") is False:
            err = str(result.get("error", ""))[:500]

        masked = mask_params(params)
        db = SessionLocal()
        try:
            db.add(ToolCallAudit(
                company_id=company_id,
                agent_name=(agent_name or "")[:100],
                tool_name=(tool_name or "")[:100],
                actor=(actor or "agent")[:30],
                source=(source or "")[:30],
                session_id=session_id,
                params_json=json.dumps(masked, ensure_ascii=False)[:4000],
                params_hash=params_hash(masked),
                ok=bool(ok),
                error=err,
                latency_ms=round(float(latency_ms or 0), 1),
                result_excerpt=_excerpt(result),
                ip=(ip or "")[:50],
                created_at=time.time(),
            ))
            db.commit()
        finally:
            db.close()

        # 联动"安全审计"页：让企业管理员在既有页面也能看到 AI 员工动作
        try:
            from utils.audit import log_action
            log_action(company_id, "tool.call",
                       f"{agent_name or source or 'api'} -> {tool_name} "
                       f"{'OK' if ok else 'FAIL'} {latency_ms:.0f}ms", ip=ip)
        except Exception:
            pass

    except Exception as e:
        logger.warning("工具调用审计写入失败（不影响业务）: %s", e)


def query_tool_calls(company_id: int, agent_name: str = None, tool_name: str = None,
                     ok: bool = None, since: float = None, until: float = None,
                     source: str = None, limit: int = 100, offset: int = 0) -> dict:
    """按租户查询工具调用审计（倒序）。"""
    from database import SessionLocal
    from models import ToolCallAudit

    db = SessionLocal()
    try:
        q = db.query(ToolCallAudit).filter(ToolCallAudit.company_id == company_id)
        if agent_name:
            q = q.filter(ToolCallAudit.agent_name == agent_name)
        if tool_name:
            q = q.filter(ToolCallAudit.tool_name == tool_name)
        if ok is not None:
            q = q.filter(ToolCallAudit.ok == ok)
        if source:
            q = q.filter(ToolCallAudit.source == source)
        if since:
            q = q.filter(ToolCallAudit.created_at >= float(since))
        if until:
            q = q.filter(ToolCallAudit.created_at <= float(until))

        limit = max(1, min(int(limit or 100), 500))
        total = q.count()
        rows = q.order_by(ToolCallAudit.created_at.desc())\
                .offset(max(0, int(offset or 0))).limit(limit).all()
        return {"total": total, "items": [_to_dict(r) for r in rows]}
    finally:
        db.close()


def replay_session(company_id: int, session_id: str) -> dict:
    """回放一次会话的完整调用链（正序时间线）。"""
    from database import SessionLocal
    from models import ToolCallAudit

    db = SessionLocal()
    try:
        rows = db.query(ToolCallAudit).filter(
            ToolCallAudit.company_id == company_id,
            ToolCallAudit.session_id == session_id,
        ).order_by(ToolCallAudit.created_at.asc()).all()
        timeline = [_to_dict(r) for r in rows]
        return {
            "company_id": company_id,
            "session_id": session_id,
            "steps": len(timeline),
            "failed": sum(1 for s in timeline if not s["ok"]),
            "total_latency_ms": round(sum(s["latency_ms"] for s in timeline), 1),
            "timeline": timeline,
        }
    finally:
        db.close()


def stats(company_id: int, days: int = 7) -> dict:
    """近 N 天调用统计：总量、失败率、耗时、Top 工具。"""
    from database import SessionLocal
    from models import ToolCallAudit
    from sqlalchemy import func

    since = time.time() - max(1, int(days or 7)) * 86400
    db = SessionLocal()
    try:
        base = db.query(ToolCallAudit).filter(
            ToolCallAudit.company_id == company_id,
            ToolCallAudit.created_at >= since)
        total = base.count()
        failed = base.filter(ToolCallAudit.ok == False).count()  # noqa: E712
        avg_latency = db.query(func.avg(ToolCallAudit.latency_ms)).filter(
            ToolCallAudit.company_id == company_id,
            ToolCallAudit.created_at >= since).scalar() or 0
        top = db.query(ToolCallAudit.tool_name, func.count(ToolCallAudit.id))\
            .filter(ToolCallAudit.company_id == company_id,
                    ToolCallAudit.created_at >= since)\
            .group_by(ToolCallAudit.tool_name)\
            .order_by(func.count(ToolCallAudit.id).desc()).limit(5).all()
        return {
            "days": days,
            "total": total,
            "failed": failed,
            "fail_rate": round(failed / total, 4) if total else 0.0,
            "avg_latency_ms": round(float(avg_latency), 1),
            "top_tools": [{"tool": t, "count": c} for t, c in top],
        }
    finally:
        db.close()


def _to_dict(r) -> dict:
    return {
        "id": r.id,
        "company_id": r.company_id,
        "agent_name": r.agent_name,
        "tool_name": r.tool_name,
        "actor": r.actor,
        "source": r.source,
        "session_id": r.session_id,
        "params": _loads(r.params_json),
        "params_hash": r.params_hash,
        "ok": r.ok,
        "error": r.error,
        "latency_ms": r.latency_ms,
        "result_excerpt": r.result_excerpt,
        "ip": r.ip,
        "created_at": r.created_at,
    }


def _loads(text) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {"_raw": v}
    except Exception:
        return {"_raw": str(text)[:200]}
