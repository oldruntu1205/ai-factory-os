"""
api/account.py —— 账户设置（修改手机号 / 修改密码）

- POST /account/change-password  修改账户密码（需先手机验证码验证，purpose=change_password）
- 修改手机号复用 /company/sms/send + /company/sms/verify（验证通过自动写入新手机号）
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import User, Company, SmsCode
from auth.deps import get_current_user
from auth.security import hash_password, verify_password
from utils.sms import generate_code, send_sms
from utils.audit import log_action

router = APIRouter(prefix="/account", tags=["账户设置"])


class ChangePasswordIn(BaseModel):
    mobile: str
    code: str
    old_password: str = ""          # 安全修复：改密必须校验旧密码
    new_password: str


def _validate_mobile(mobile: str) -> bool:
    mobile = mobile.replace(" ", "").replace("-", "").replace("+", "")
    return mobile.isdigit() and 7 <= len(mobile) <= 15


@router.post("/send-code")
def send_change_password_code(mobile: str, db: Session = Depends(get_db),
                              current: dict = Depends(get_current_user)):
    """发送修改密码验证码（purpose=change_password）"""
    if not _validate_mobile(mobile):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    user = db.query(User).filter(User.id == current["user_id"]).first()
    # 校验手机号属于当前用户（账户安全）
    if current.get("role") == "platform_admin":
        if not user.mobile or user.mobile != mobile:
            raise HTTPException(status_code=400, detail="请先绑定手机号")
    else:
        company = db.query(Company).filter(Company.id == current["company_id"]).first()
        if not company or (company.mobile or "") != mobile:
            raise HTTPException(status_code=400, detail="请使用当前企业绑定的手机号")

    db.query(SmsCode).filter(
        SmsCode.company_id == current["company_id"],
        SmsCode.mobile == mobile,
        SmsCode.purpose == "change_password",
        SmsCode.used.is_(False),
    ).update({"used": True})
    code = generate_code()
    s = SmsCode(
        company_id=current["company_id"], mobile=mobile,
        code=code, purpose="change_password",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db.add(s)
    db.commit()
    send_sms(mobile, code, purpose="change_password")
    log_action(current["company_id"], "account_send_code",
               f"修改密码验证码发送至 {mobile[:6]}***", current["user_id"], current["email"])
    # 安全修复：接口不再回显验证码（此前 mock_code 明文返回）
    return {"message": "验证码已发送", "expires_in": 600}


@router.post("/change-password")
def change_password(data: ChangePasswordIn, db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """修改密码：旧密码 + 手机验证码双因素校验通过后更新密码"""
    if not _validate_mobile(data.mobile):
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")

    user = db.query(User).filter(User.id == current["user_id"]).first()
    # 安全修复：必须校验旧密码（此前仅凭可回显的验证码即可改密）
    if not data.old_password or not verify_password(data.old_password, user.password):
        raise HTTPException(status_code=400, detail="旧密码不正确")

    record = db.query(SmsCode).filter(
        SmsCode.company_id == current["company_id"],
        SmsCode.mobile == data.mobile,
        SmsCode.purpose == "change_password",
        SmsCode.used.is_(False),
    ).order_by(SmsCode.created_at.desc()).first()

    if not record:
        raise HTTPException(status_code=400, detail="请先获取验证码")
    if record.expires_at and datetime.utcnow() > record.expires_at:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if record.code != data.code.strip():
        raise HTTPException(status_code=400, detail="验证码不正确")

    record.used = True
    user.password = hash_password(data.new_password)
    db.commit()
    log_action(current["company_id"], "password_change",
               f"账户密码已修改（手机验证）", current["user_id"], current["email"])
    return {"message": "密码修改成功（已通过手机验证）"}
