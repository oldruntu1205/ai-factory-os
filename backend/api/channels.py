"""
api/channels.py —— 外部渠道 API（第五阶段 + V2.1 自动化，已完善）

- GET  /channels/config          读取企业级渠道配置（密钥脱敏）
- PUT  /channels/config          保存企业级渠道配置（客户自助对接企微/WhatsApp/邮件）
- POST /channels/whatsapp/send    发送 WhatsApp 消息
- POST /channels/email/send       AI 生成开发信并发送（支持报价 PDF 附件）
- POST /channels/email/generate   仅生成开发信（预览）
- POST /channels/email/sequence   生成 3 封跟进序列（预览）
- POST /channels/email/followup   发送线索第 N 步跟进邮件（自动带报价 PDF）
- POST /channels/email/bounce     标记退信（模拟退信处理）
- GET  /channels/email/logs       邮件发送日志
- POST /channels/wecom/robot      推送到企业微信群（AI 日报/报价）
- POST /channels/wecom/app        企业微信应用消息发送
- GET  /channels/status           各渠道配置状态
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.deps import get_current_user, require_company
from api.company import require_verified
from channels.whatsapp.webhook import send_whatsapp_message
from channels.email.generator import create_email
from channels.email.smtp import send_email
from channels.email.sequence import build_sequence, send_follow_up, run_due_follow_ups
from channels.wecom.robot import send_robot_markdown, send_robot_text
from channels.wecom.app import send_app_text
from channels.wecom.config import is_app_configured, is_robot_configured, WECOM_BOT_ID
from channels.wecom.bot_client import test_bot
from channels.config_utils import (get_company_config, save_company_config,
                                   mask_secrets, CHANNEL_KEYS)

router = APIRouter(prefix="/channels", tags=["外部渠道"])


class ConfigIn(BaseModel):
    company_id: int
    channel: str          # wecom_robot / wecom_app / whatsapp / email
    config: dict


class WhatsAppSend(BaseModel):
    to: str
    message: str
    company_id: int = 1


class EmailSend(BaseModel):
    receiver: str
    company: str
    product: str
    extra: str = ""


class EmailGenerate(BaseModel):
    company: str
    product: str
    extra: str = ""
    language: str = "zh"           # zh 中文 / en 英文（外贸格式）


class FollowUpIn(BaseModel):
    lead_id: int
    company_id: int
    step: int = 1
    with_quote_pdf: bool = True


class WecomRobotIn(BaseModel):
    content: str
    msgtype: str = "markdown"   # markdown / text
    company_id: int = None


class WecomAppIn(BaseModel):
    user_id: str
    content: str


class WecomBotTestIn(BaseModel):
    bot_id: str
    secret: str


@router.post("/whatsapp/send")
def whatsapp_send(data: WhatsAppSend, current: dict = Depends(get_current_user)):
    require_verified(current.get("license_verified", False))
    result = send_whatsapp_message(data.to, data.message)
    return result


@router.post("/email/generate")
def email_generate(data: EmailGenerate, current: dict = Depends(get_current_user)):
    content = create_email(data.company, data.product, data.extra)
    return {"content": content}


@router.post("/email/send")
def email_send(data: EmailSend, current: dict = Depends(get_current_user)):
    require_verified(current.get("license_verified", False))
    content = create_email(data.company, data.product, data.extra)
    subject = "Business Cooperation - Premium Industrial Products"
    result = send_email(data.receiver, content, subject,
                        company_id=data.company_id if hasattr(data, "company_id") else None)
    return {**result, "content": content}


@router.post("/email/sequence")
def email_sequence(data: EmailGenerate, current: dict = Depends(get_current_user)):
    """生成 3 封跟进序列（预览，language: zh 中文 / en 英文外贸格式）"""
    seq = build_sequence(data.company, data.product, data.extra,
                         language=data.language)
    return {"sequence": seq, "language": data.language}


@router.post("/email/followup")
def email_followup(data: FollowUpIn, current: dict = Depends(get_current_user)):
    """发送线索第 N 步跟进邮件（自动带最新报价 PDF）。

    安全修复：company_id 必须为当前登录企业（此前不校验归属，
    任意用户可借他企 SMTP 配置向其客户发信并推进其线索状态机——跨租户动作注入）。
    """
    require_verified(current.get("license_verified", False))
    require_company(data.company_id, current)
    return send_follow_up(data.lead_id, data.company_id, step=data.step)


@router.post("/email/bounce")
def email_bounce(data: dict, current: dict = Depends(get_current_user)):
    """标记退信（模拟：真实环境由 SMTP 回执/MX 检测触发）

    修复：仅能标记**当前企业**的邮件日志（此前无 company_id 过滤可跨租户改状态）。
    """
    from database import SessionLocal
    from models import EmailLog
    recipient = (data or {}).get("recipient", "")
    if not recipient:
        return {"error": "recipient 必填"}
    db = SessionLocal()
    try:
        logs = db.query(EmailLog).filter(
            EmailLog.recipient == recipient,
            EmailLog.company_id == current["company_id"]).all()
        for log in logs:
            log.status = "bounced"
        db.commit()
        return {"message": f"已标记 {len(logs)} 条退信", "count": len(logs)}
    finally:
        db.close()


@router.get("/email/logs")
def email_logs(company_id: int, current: dict = Depends(get_current_user)):
    """邮件发送日志（含退信状态）"""
    from database import SessionLocal
    from models import EmailLog
    if company_id != current["company_id"]:
        return {"error": "无权查看"}
    db = SessionLocal()
    try:
        logs = db.query(EmailLog).filter(EmailLog.company_id == company_id)\
            .order_by(EmailLog.id.desc()).limit(50).all()
        return {"logs": [{
            "id": l.id, "recipient": l.recipient, "subject": l.subject,
            "status": l.status, "created_at": l.created_at.isoformat(),
        } for l in logs]}
    finally:
        db.close()


@router.post("/wecom/robot")
def wecom_robot(data: WecomRobotIn, current: dict = Depends(get_current_user)):
    """向企业微信群推送（AI 日报/报价 markdown），优先用企业配置

    安全修复：company_id 若不传则用当前企业；若显式传他企 → 403
    （此前任意用户可借任意企业已配置的企微机器人 webhook 推送内容——跨租户借力外发）。
    """
    require_verified(current.get("license_verified", False))
    cid = data.company_id or current["company_id"]
    require_company(cid, current)
    if data.msgtype == "text":
        return send_robot_text(data.content, company_id=cid)
    return send_robot_markdown(data.content, company_id=cid)


@router.post("/wecom/app")
def wecom_app(data: WecomAppIn, current: dict = Depends(get_current_user)):
    """企业微信自建应用发消息（优先用企业配置）"""
    require_verified(current.get("license_verified", False))
    return send_app_text(data.user_id, data.content, company_id=current["company_id"])


@router.post("/wecom/bot/test")
async def wecom_bot_test(data: WecomBotTestIn, current: dict = Depends(get_current_user)):
    """智能机器人连通性测试（建立一次性连接 + 订阅鉴权）"""
    return await test_bot(data.bot_id, data.secret)


@router.get("/config")
def channel_config(company_id: int, current: dict = Depends(get_current_user)):
    """读取企业级渠道配置（密钥脱敏显示）"""
    if company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    result = {}
    for channel in CHANNEL_KEYS:
        result[channel] = mask_secrets(get_company_config(company_id, channel))
    # 附带各渠道是否已配置（含全局 env 回退）
    import config as G
    result["_meta"] = {
        "wecom_robot": {"configured": is_robot_configured() or bool(get_company_config(company_id, "wecom_robot"))},
        "wecom_app": {"configured": is_app_configured() or bool(get_company_config(company_id, "wecom_app"))},
        "wecom_bot": {"configured": bool(WECOM_BOT_ID) or bool(get_company_config(company_id, "wecom_bot"))},
        "whatsapp": {"configured": bool(G.WHATSAPP_TOKEN) or bool(get_company_config(company_id, "whatsapp"))},
        "email": {"configured": bool(G.SMTP_USER) or bool(get_company_config(company_id, "email"))},
    }
    return result


@router.put("/config")
def channel_config_save(data: ConfigIn, current: dict = Depends(get_current_user)):
    """保存企业级渠道配置（客户自助对接）"""
    if data.company_id != current["company_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    if data.channel not in CHANNEL_KEYS:
        raise HTTPException(status_code=400, detail=f"渠道无效，可选: {list(CHANNEL_KEYS)}")
    result = save_company_config(data.company_id, data.channel, data.config)
    if not result.get("saved"):
        raise HTTPException(status_code=500, detail=result.get("error", "保存失败"))
    # 保存后返回脱敏状态
    return {**result,
            "config": mask_secrets(get_company_config(data.company_id, data.channel))}


@router.get("/status")
def channel_status(current: dict = Depends(get_current_user)):
    """各渠道配置状态（前端展示用，含企业级配置）"""
    import config as G
    cid = current["company_id"]
    wc = get_company_config(cid, "whatsapp")
    wr = get_company_config(cid, "wecom_robot")
    wa = get_company_config(cid, "wecom_app")
    wb = get_company_config(cid, "wecom_bot")
    em = get_company_config(cid, "email")
    return {
        "whatsapp": {"configured": bool(G.WHATSAPP_TOKEN) or bool(wc),
                     "phone_id": wc.get("whatsapp_phone_id") or G.WHATSAPP_PHONE_ID},
        "wecom": {"robot_configured": is_robot_configured() or bool(wr),
                  "app_configured": is_app_configured() or bool(wa),
                  "bot_configured": bool(WECOM_BOT_ID) or bool(wb)},
        "email": {"configured": bool(G.SMTP_USER) or bool(em)},
    }
