"""
api/members.py —— 企业成员管理（② 企业成员：邀请/角色/停用）

- GET    /members/list            成员列表
- POST   /members/invite          邀请成员（生成邀请码）
- POST   /members/invite/accept   接受邀请（注册为成员）
- PUT    /members/{id}/role       分配角色（admin/staff）
- PUT    /members/{id}/status     停用/启用
- DELETE /members/{id}            移除成员
"""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from database import get_db
from models import User, Company, MemberInvite
from auth.deps import get_current_user
from auth.security import hash_password
from utils.audit import log_action, require_admin
from constants.status import MEMBER_STATUS


def _require_admin(current):
    require_admin(current.get("role", "admin"))


router = APIRouter(prefix="/members", tags=["企业成员"])


class InviteIn(BaseModel):
    company_id: int
    email: EmailStr
    role: str = "staff"


class AcceptIn(BaseModel):
    code: str
    email: EmailStr
    password: str
    name: str = "成员"


@router.get("/list")
def list_members(company_id: int, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """企业成员列表"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    members = db.query(User).filter(User.company_id == company_id)\
        .order_by(User.id).all()
    return {"members": [{
        "id": u.id, "email": u.email, "name": u.name, "role": u.role,
        "status": getattr(u, "status", "active"),
        "created_at": u.created_at.isoformat(),
    } for u in members]}


@router.post("/invite")
def invite_member(data: InviteIn, db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """邀请成员：生成邀请码（有效期 7 天）"""
    _require_admin(current)
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")

    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="该邮箱已注册，直接添加为成员")

    code = uuid.uuid4().hex[:24]
    invite = MemberInvite(
        company_id=data.company_id,
        email=data.email,
        role=data.role,
        code=code,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.add(invite)
    db.commit()

    log_action(data.company_id, "member_invite",
               f"邀请 {data.email}（{data.role}）", current["user_id"],
               current["email"])
    return {"message": "邀请已创建", "code": code,
            "register_link": f"/register?invite={code}"}


@router.post("/invite/accept")
def accept_invite(data: AcceptIn, db: Session = Depends(get_db)):
    """接受邀请：通过邀请码注册为企业成员"""
    invite = db.query(MemberInvite).filter(MemberInvite.code == data.code).first()
    if not invite or invite.status != "pending":
        raise HTTPException(status_code=400, detail="邀请码无效或已过期")
    if invite.email != data.email:
        raise HTTPException(status_code=400, detail="邀请邮箱不匹配")
    if invite.expires_at and datetime.utcnow() > invite.expires_at:
        invite.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="邀请已过期")

    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="该邮箱已注册")

    user = User(
        email=data.email,
        password=hash_password(data.password),
        name=data.name,
        role=invite.role,
        company_id=invite.company_id,
    )
    db.add(user)
    invite.status = "accepted"
    db.commit()

    log_action(invite.company_id, "member_joined",
               f"成员 {data.email} 加入", user.id, user.email)
    return {"message": "加入成功", "company_id": invite.company_id,
            "role": invite.role}


class RoleIn(BaseModel):
    role: str
    company_id: int


class StatusIn(BaseModel):
    status: str
    company_id: int


class RemoveIn(BaseModel):
    company_id: int


@router.put("/{member_id}/role")
def set_role(member_id: int, data: RoleIn, db: Session = Depends(get_db),
             current: dict = Depends(get_current_user)):
    """分配角色（admin/staff）"""
    _require_admin(current)
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if data.role not in ("admin", "staff"):
        raise HTTPException(status_code=400, detail="角色无效")
    if member_id == current["user_id"]:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")

    member = db.query(User).filter(
        User.id == member_id, User.company_id == data.company_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="成员不存在")
    member.role = data.role
    db.commit()
    log_action(data.company_id, "member_role", f"成员 {member.email} 角色改为 {data.role}",
               current["user_id"], current["email"])
    return {"message": "角色已更新", "role": member.role}


@router.put("/{member_id}/status")
def set_status(member_id: int, data: StatusIn, db: Session = Depends(get_db),
               current: dict = Depends(get_current_user)):
    """停用/启用成员"""
    _require_admin(current)
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if member_id == current["user_id"]:
        raise HTTPException(status_code=400, detail="不能停用自己的账号")

    if data.status not in MEMBER_STATUS:
        raise HTTPException(status_code=400, detail=f"状态无效，可选: {sorted(MEMBER_STATUS)}")
    member = db.query(User).filter(
        User.id == member_id, User.company_id == data.company_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="成员不存在")
    member.status = data.status
    db.commit()
    log_action(data.company_id, "member_status", f"成员 {member.email} {'停用' if data.status=='disabled' else '启用'}",
               current["user_id"], current["email"])
    return {"message": "状态已更新"}


@router.delete("/{member_id}")
def remove_member(member_id: int, data: RemoveIn = None,
                  db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """移除成员"""
    _require_admin(current)
    company_id = (data.company_id if data else None) or current["company_id"]
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if member_id == current["user_id"]:
        raise HTTPException(status_code=400, detail="不能移除自己")

    member = db.query(User).filter(
        User.id == member_id, User.company_id == company_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="成员不存在")
    email = member.email
    db.delete(member)
    db.commit()
    log_action(company_id, "member_remove", f"移除成员 {email}",
               current["user_id"], current["email"])
    return {"message": "成员已移除"}
