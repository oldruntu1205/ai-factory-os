"""
api/company.py —— 企业设置（基本信息 + 认证 + 安全）

- GET  /company/settings           读取企业基本信息 + 认证状态
- PUT  /company/settings           更新企业名称/地址/电话/联系人/邮箱
- POST /company/license/upload      上传营业执照照片（自动审核：演示版通过）
- POST /company/license/approve    审核通过（管理端演示用）
- DELETE /company/license           清除已上传的营业执照
- PUT  /company/password            修改管理员密码（安全设置）
- GET  /company/audit-logs          操作日志列表（安全审计）

注：营业执照图片按 company_id 隔离保存在 uploads/licenses/。
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db, SessionLocal
from models import Company, User, SmsCode
from auth.deps import get_current_user
from auth.security import hash_password, verify_password
from config import BASE_DIR, SMS_MOCK_CODE, MAX_UPLOAD_SIZE_MB
from utils.audit import log_action, list_audit_logs, require_admin
from constants.industries import get_all_options

router = APIRouter(prefix="/company", tags=["企业设置"])

LICENSE_DIR = os.path.join(BASE_DIR, "uploads", "licenses")
os.makedirs(LICENSE_DIR, exist_ok=True)


class CompanyUpdate(BaseModel):
    company_id: int
    name: str = None
    address: str = None
    phone: str = None
    mobile: str = None              # 联系手机（需先通过 /company/sms/verify 验证）
    contact_name: str = None
    contact_email: str = None
    industry: str = None             # 所属行业（GB/T 4754-2017 门类或大类代码）


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class SmsSendIn(BaseModel):
    company_id: int
    mobile: str
    purpose: str = "verify_mobile"


class SmsVerifyIn(BaseModel):
    company_id: int
    mobile: str
    code: str
    purpose: str = "verify_mobile"


@router.get("/settings")
def get_settings(company_id: int, db: Session = Depends(get_db),
                 current: dict = Depends(get_current_user)):
    """读取企业信息（含认证状态）"""
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c or c.id != current["company_id"]:
        raise HTTPException(status_code=404, detail="企业不存在")
    return _to_dict(c)


@router.put("/settings")
def update_settings(data: CompanyUpdate, db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """更新企业基本信息"""
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权修改其他企业")
    c = db.query(Company).filter(Company.id == data.company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="企业名称不能为空")
        if len(name) > 100:
            raise HTTPException(status_code=400, detail="企业名称不能超过 100 字符")
        c.name = name
        # 同步 localStorage 中的企业名
    if data.address is not None:
        c.address = data.address.strip()[:300]
    if data.phone is not None:
        c.phone = data.phone.strip()[:50]
    if data.contact_name is not None:
        c.contact_name = data.contact_name.strip()[:100]
    if data.contact_email is not None:
        c.contact_email = data.contact_email.strip()[:100]

    # 联系手机：必须先通过 /company/sms/verify 验证
    if data.mobile is not None and data.mobile.strip():
        new_mobile = data.mobile.strip()[:50]
        if new_mobile != (c.mobile or ""):
            # 新手机号必须已验证
            verified = db.query(SmsCode).filter(
                SmsCode.company_id == data.company_id,
                SmsCode.mobile == new_mobile,
                SmsCode.purpose == "verify_mobile",
                SmsCode.used.is_(True),
            ).first()
            if not verified:
                raise HTTPException(status_code=400,
                                    detail="请先获取并填写验证码完成手机验证")
            c.mobile = new_mobile
            c.mobile_verified = True
        elif not c.mobile_verified:
            # 同样的号码但未验证 → 也要求验证
            verified = db.query(SmsCode).filter(
                SmsCode.company_id == data.company_id,
                SmsCode.mobile == new_mobile,
                SmsCode.purpose == "verify_mobile",
                SmsCode.used.is_(True),
            ).first()
            if not verified:
                raise HTTPException(status_code=400,
                                    detail="请先获取并填写验证码完成手机验证")
            c.mobile_verified = True
    elif data.mobile is not None:
        # 显式传空字符串 = 清除
        c.mobile = ""
        c.mobile_verified = False

    # 所属行业（设置后锁定，不允许再次修改）
    if data.industry is not None and data.industry.strip():
        if c.industry and c.industry.strip():
            if data.industry.strip() != c.industry.strip():
                raise HTTPException(status_code=400,
                                    detail="所属行业已设置，锁定后不可修改")
        else:
            c.industry = data.industry.strip()[:50]
    elif data.industry is not None and not data.industry.strip():
        # 显式传空：首次可清空（用于演示重置），已有值则忽略
        if not (c.industry or "").strip():
            c.industry = ""

    db.commit()
    db.refresh(c)
    return {"message": "settings updated", "company": _to_dict(c)}


@router.post("/license/upload")
def upload_license(
    company_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: dict = Depends(get_current_user),
):
    """
    上传营业执照照片：
    - 限制 jpg/png/pdf，最大 5MB
    - 提交后 status 进入 pending，等待平台超管人工审核（不再自动通过）
    """
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作其他企业")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 JPG/PNG/PDF 格式")

    # 大小限制：先整块限长读取（> max_bytes+1 即超限），避免大文件先吃满内存
    max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    content = file.file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件过大，最大 {MAX_UPLOAD_SIZE_MB}MB")

    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")

    # 保存文件（按企业 ID 命名空间隔离）
    safe_name = f"company_{company_id}_license{ext}"
    path = os.path.join(LICENSE_DIR, safe_name)
    with open(path, "wb") as f:
        f.write(content)

    c.business_license_path = path
    c.license_status = "pending"
    c.license_uploaded_at = datetime.utcnow()
    db.commit()

    # 安全修复：移除演示版 1 秒自动通过——认证门必须由平台超管人工审核
    # （见 /platform/companies/{id}/verify 与 /company/license/approve）

    log_action(company_id, "license_upload", "上传营业执照，等待平台审核",
               current.get("user_id", 0), current.get("email", ""))
    return {
        "message": "营业执照已上传，等待平台审核",
        "license_status": "pending",
        "file_name": safe_name,
        "file_size": len(content),
    }


@router.get("/license/download")
def download_license(company_id: int, db: Session = Depends(get_db),
                     current: dict = Depends(get_current_user)):
    """下载本企业营业执照（鉴权：仅本企业成员可访问，防匿名/跨租户枚举下载）"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c or not c.business_license_path or not os.path.exists(c.business_license_path):
        raise HTTPException(status_code=404, detail="营业执照不存在")
    return FileResponse(c.business_license_path,
                        filename=os.path.basename(c.business_license_path))


def _schedule_auto_approve(company_id: int, delay: int = 1):
    """（已废弃）演示版自动审核——安全修复后移除自动通过，保留函数避免历史引用报错"""
    return


@router.post("/license/approve")
def approve_license(company_id: int, approve: bool = True,
                    reject_reason: str = "", db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """管理端审核（演示用）：通过或拒绝"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    c = db.query(Company).filter(Company.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="企业不存在")
    if approve:
        c.license_status = "approved"
        c.license_verified = True
        c.license_verified_at = datetime.utcnow()
        c.license_reject_reason = ""
    else:
        c.license_status = "rejected"
        c.license_verified = False
        c.license_reject_reason = reject_reason
    db.commit()
    return {"message": "审核完成", "license_status": c.license_status}


@router.delete("/license")
def delete_license(company_id: int, db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """清除已上传的营业执照（重新提交）"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    c = db.query(Company).filter(Company.id == company_id).first()
    if c and c.business_license_path:
        try:
            if os.path.exists(c.business_license_path):
                os.remove(c.business_license_path)
        except OSError:
            pass
        c.business_license_path = ""
        c.license_status = "none"
        c.license_verified = False
        c.license_reject_reason = ""
        c.license_uploaded_at = None
        c.license_verified_at = None
        db.commit()
    return {"message": "license cleared"}


def _to_dict(c: Company) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "plan": c.plan,
        "max_agents": c.max_agents,
        "address": c.address or "",
        "phone": c.phone or "",
        "mobile": c.mobile or "",
        "mobile_verified": bool(getattr(c, "mobile_verified", False)),
        "contact_name": c.contact_name or "",
        "contact_email": c.contact_email or "",
        "industry": c.industry or "",
        "license_status": c.license_status or "none",
        "license_verified": bool(c.license_verified),
        "license_uploaded_at": c.license_uploaded_at.isoformat() if c.license_uploaded_at else None,
        "license_verified_at": c.license_verified_at.isoformat() if c.license_verified_at else None,
        "license_reject_reason": c.license_reject_reason or "",
        "has_license": bool(c.business_license_path),
        "created_at": c.created_at.isoformat(),
    }


@router.get("/industries")
def industries_list():
    """GB/T 4754-2017 行业分类下拉选项（20 门类 + 制造业 31 大类）"""
    return {"industries": get_all_options()}


# ============ 安全设置 ============

@router.put("/password")
def change_password(data: PasswordChange, request: Request,
                    db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """修改管理员密码（校验旧密码）"""
    require_admin(current.get("role", "admin"))
    user = db.query(User).filter(User.id == current["user_id"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not verify_password(data.old_password, user.password):
        log_action(current["company_id"], "password_change_failed",
                   "旧密码校验失败", user.id, user.email,
                   request.client.host if request.client else "")
        raise HTTPException(status_code=400, detail="旧密码不正确")

    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")

    user.password = hash_password(data.new_password)
    db.commit()
    log_action(current["company_id"], "password_change",
               "管理员密码已修改", user.id, user.email,
               request.client.host if request.client else "")
    return {"message": "密码修改成功"}


@router.get("/audit-logs")
def get_audit_logs(company_id: int, db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """企业操作日志（安全审计）"""
    require_admin(current.get("role", "admin"))
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权查看")
    return {"logs": list_audit_logs(db, company_id)}


# ============ 手机验证码 ============

def _validate_mobile(mobile: str) -> bool:
    """简单手机号校验（11 位中国大陆 / 7-15 位国际号）"""
    if not mobile:
        return False
    mobile = mobile.replace(" ", "").replace("-", "").replace("+", "")
    return mobile.isdigit() and 7 <= len(mobile) <= 15


@router.post("/sms/send")
def sms_send(data: SmsSendIn, db: Session = Depends(get_db),
             current: dict = Depends(get_current_user)):
    """发送手机验证码（演示版固定返回 123456，生产接阿里云/腾讯云 SMS）"""
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if not _validate_mobile(data.mobile):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    from datetime import timedelta
    code = SMS_MOCK_CODE  # 演示版固定验证码
    # 失效旧的同用途验证码
    db.query(SmsCode).filter(
        SmsCode.company_id == data.company_id,
        SmsCode.mobile == data.mobile,
        SmsCode.purpose == data.purpose,
        SmsCode.used.is_(False),
    ).update({"used": True})
    # 写入新验证码（10 分钟过期）
    s = SmsCode(
        company_id=data.company_id, mobile=data.mobile,
        code=code, purpose=data.purpose,
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db.add(s)
    db.commit()
    log_action(data.company_id, "sms_send",
               f"发送验证码到 {data.mobile[:6]}***（{data.purpose}）",
               current["user_id"], current["email"])
    return {"message": "验证码已发送",
            "mobile": data.mobile,
            "expires_in": 600,
            "mock_code": code}   # 演示版直接返回真实码，生产应去掉


@router.post("/sms/verify")
def sms_verify(data: SmsVerifyIn, db: Session = Depends(get_db),
               current: dict = Depends(get_current_user)):
    """校验手机验证码"""
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if not _validate_mobile(data.mobile):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    record = db.query(SmsCode).filter(
        SmsCode.company_id == data.company_id,
        SmsCode.mobile == data.mobile,
        SmsCode.purpose == data.purpose,
        SmsCode.used.is_(False),
    ).order_by(SmsCode.created_at.desc()).first()

    if not record:
        raise HTTPException(status_code=400, detail="请先获取验证码")
    if record.expires_at and datetime.utcnow() > record.expires_at:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if record.code != data.code.strip():
        raise HTTPException(status_code=400, detail="验证码不正确")

    record.used = True
    # 自动写入企业 mobile
    c = db.query(Company).filter(Company.id == data.company_id).first()
    if c:
        c.mobile = data.mobile
        c.mobile_verified = True
        db.commit()
    log_action(data.company_id, "sms_verify",
               f"手机 {data.mobile[:6]}*** 验证通过",
               current["user_id"], current["email"])
    return {"message": "验证成功", "mobile": data.mobile}


# ============ 工具：检测企业是否已认证 ============

def require_verified(license_verified: bool) -> None:
    """业务操作需要企业已认证（未认证返回 403）"""
    if not license_verified:
        raise HTTPException(
            status_code=403,
            detail="请先完成企业认证（营业执照审核通过）后再使用此功能")