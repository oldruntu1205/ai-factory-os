"""
auth/deps.py —— 当前用户依赖（JWT 认证 + 多租户上下文）
"""
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from database import get_db
from models import User, Company
from auth.jwt import decode_token
from jose import JWTError


def get_current_user(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """从 Authorization: Bearer <token> 解析当前用户与租户"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少认证令牌")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if getattr(user, "status", "active") == "disabled":
        raise HTTPException(status_code=403, detail="账号已被停用")

    # 平台超级管理员（不属于任何企业）
    if user.role == "platform_admin":
        # 安全修复：platform_admin 必须不属于任何企业（company_id 为空），
        # 防止通过成员邀请越权构造出的伪超管获得平台权限
        if user.company_id is not None:
            raise HTTPException(status_code=403, detail="平台超管不应属于任何企业，账号异常")
        return {
            "user_id": user.id, "company_id": 0, "role": "platform_admin",
            "email": user.email, "company": "平台管理",
            "license_verified": True, "plan": "platform",
        }

    company = db.query(Company).filter(Company.id == user.company_id).first()
    if not company:
        raise HTTPException(status_code=401, detail="企业不存在")

    return {
        "user_id": user.id, "company_id": user.company_id, "role": user.role,
        "email": user.email, "company": company.name,
        "license_verified": bool(getattr(company, "license_verified", False)),
        "plan": getattr(company, "plan", "starter"),
    }


def require_company(company_id: int, current: dict) -> None:
    """多租户校验：用户只能操作自己企业的数据"""
    if int(company_id) != int(current["company_id"]):
        raise HTTPException(status_code=403, detail="无权访问其他企业的数据")
