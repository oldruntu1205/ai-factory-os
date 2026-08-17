"""
auth/router.py —— 注册 / 登录接口（第九、十章）

注册时自动创建企业（多租户），第一个注册用户即企业管理员。

安全加固（2026-08）：
- 登录限流（IP+账号，5 次/分钟）防暴力破解
- 登录失败统一文案（消除用户枚举）
"""
import re
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from database import get_db
from models import User, Company
from auth.security import hash_password, verify_password
from auth.jwt import create_token
from auth.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["认证"])

# 登录限流：按 (IP, 邮箱) 5 次/分钟（进程内实现；多 worker 部署建议换 Redis）
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60
_login_attempts: dict = defaultdict(list)
# 注册限流：按 IP 10 次/小时（P2：防批量注册/邮箱枚举）
REGISTER_MAX_ATTEMPTS = 10
REGISTER_WINDOW_SECONDS = 3600
_register_attempts: dict = defaultdict(list)


def _login_rate_limited(key: str) -> bool:
    now = time.time()
    lst = _login_attempts[key]
    lst[:] = [t for t in lst if now - t < LOGIN_WINDOW_SECONDS]
    if len(lst) >= LOGIN_MAX_ATTEMPTS:
        return True
    lst.append(now)
    return False


def _register_rate_limited(ip: str) -> bool:
    now = time.time()
    lst = _register_attempts[ip]
    lst[:] = [t for t in lst if now - t < REGISTER_WINDOW_SECONDS]
    if len(lst) >= REGISTER_MAX_ATTEMPTS:
        return True
    lst.append(now)
    return False


class RegisterIn(BaseModel):
    company: str
    email: EmailStr
    password: str
    name: str = "管理员"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/register")
def register(data: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """企业注册：创建企业 + 管理员账号（密码策略校验 + IP 限流）"""
    # P2 安全修复：注册 IP 限流（10 次/小时，防批量注册与邮箱枚举）
    ip = request.client.host if request.client else "unknown"
    if _register_rate_limited(ip):
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")
    # 安全修复：密码至少 8 位且含字母数字；企业名限长
    if len(data.password) < 8 or not re.search(r"[A-Za-z]", data.password) \
            or not re.search(r"\d", data.password):
        raise HTTPException(status_code=400, detail="密码至少 8 位且包含字母和数字")
    if not data.company.strip() or len(data.company) > 100:
        raise HTTPException(status_code=400, detail="企业名称不合法（1-100 字符）")
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
def login(data: LoginIn, request: Request, db: Session = Depends(get_db)):
    """企业管理员登录（限流 + 统一错误文案防枚举）"""
    ip = request.client.host if request.client else "unknown"
    if _login_rate_limited(f"{ip}:{data.email.lower()}"):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 1 分钟后再试")

    user = db.query(User).filter(User.email == data.email).first()
    # 统一文案：不区分"用户不存在"/"密码错误"，消除用户枚举
    if not user or not verify_password(data.password, user.password):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

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
