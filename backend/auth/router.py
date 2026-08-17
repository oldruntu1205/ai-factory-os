"""
auth/router.py —— 注册 / 登录接口（第九、十章）

注册时自动创建企业（多租户），第一个注册用户即企业管理员。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from database import get_db
from models import User, Company
from auth.security import hash_password, verify_password
from auth.jwt import create_token
from auth.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["认证"])


class RegisterIn(BaseModel):
    company: str
    email: EmailStr
    password: str
    name: str = "管理员"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/register")
def register(data: RegisterIn, db: Session = Depends(get_db)):
    """企业注册：创建企业 + 管理员账号"""
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="该邮箱已注册")

    company = Company(name=data.company)
    db.add(company)
    db.commit()
    db.refresh(company)

    user = User(
        email=data.email,
        password=hash_password(data.password),
        name=data.name,
        company_id=company.id,
    )
    db.add(user)
    db.commit()

    # 为新租户初始化 Hermes RBAC 权限（默认权限集，与演示企业一致；
    # 便于后续租户级权限管理与审计有据可依）
    try:
        from hermes.runtime import get_runtime
        get_runtime().permission_system.init_tenant(company.id)
    except Exception:
        pass

    token = create_token(user.id, company.id, user.role)
    return {
        "message": "register success",
        "access_token": token,
        "company_id": company.id,
    }


@router.post("/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    """企业管理员登录"""
    user = db.query(User).filter(User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="user not found")

    if not verify_password(data.password, user.password):
        raise HTTPException(status_code=401, detail="password error")

    token = create_token(user.id, user.company_id, user.role)
    # 安全审计：登录成功日志
    try:
        from utils.audit import log_action
        log_action(user.company_id or 0, "login", f"用户 {user.email} 登录成功",
                   user.id, user.email)
    except Exception:
        pass
    return {
        "access_token": token,
        "company_id": user.company_id,
        "user_id": user.id,
        "name": user.name,
        "role": user.role,
    }


@router.get("/me")
def me(current: dict = Depends(get_current_user)):
    """当前登录用户信息"""
    return current
