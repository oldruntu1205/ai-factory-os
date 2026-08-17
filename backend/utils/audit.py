"""
utils/audit.py —— 操作日志记录（安全审计）

记录关键操作：登录、修改密码、企业设置变更、营业执照上传/审核等。
"""
import logging
from datetime import datetime

from database import SessionLocal
from models import AuditLog
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def require_admin(role: str) -> None:
    """检查当前用户是否为 admin（staff 无权访问运维接口）"""
    if role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


def log_action(company_id: int, action: str, detail: str = "",
               user_id: int = 0, user_email: str = "", ip: str = "") -> None:
    """写入一条操作日志（独立会话，失败不影响主流程）"""
    try:
        db = SessionLocal()
        try:
            entry = AuditLog(
                company_id=company_id,
                user_id=user_id,
                user_email=user_email[:100],
                action=action[:50],
                detail=detail[:500],
                ip=ip[:50],
            )
            db.add(entry)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"审计日志写入失败: {e}")


def list_audit_logs(db, company_id: int, limit: int = 100) -> list:
    """读取企业操作日志（倒序）"""
    logs = db.query(AuditLog).filter(AuditLog.company_id == company_id)\
        .order_by(AuditLog.created_at.desc()).limit(limit).all()
    return [{
        "id": l.id, "action": l.action, "detail": l.detail,
        "user_email": l.user_email, "ip": l.ip,
        "created_at": l.created_at.isoformat(),
    } for l in logs]
