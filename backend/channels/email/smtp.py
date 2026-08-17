"""
channels/email/smtp.py —— 邮件发送模块（V2.1 第七章，已完善）

- 支持附件（报价 PDF）
- 支持 HTML / 纯文本
- 发送后写入 email_logs 表（含退信标记）
- 未配置 SMTP 时降级为记录日志（演示模式）
"""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate

from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM

logger = logging.getLogger(__name__)


def send_email(receiver: str, content: str, subject: str = "Business Cooperation",
               html: bool = False, attachments: list = None,
               company_id: int = None) -> dict:
    """发送邮件（支持附件）；未配置 SMTP 时记录日志并返回 simulated 状态"""
    attachments = attachments or []
    if not SMTP_USER:
        logger.info(f"[模拟发送] To={receiver} Subject={subject} "
                    f"attachments={len(attachments)}\n{content[:500]}")
        result = {"status": "simulated", "to": receiver, "subject": subject,
                  "attachments": len(attachments)}
        _log_email(company_id, receiver, subject, result)
        return result

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = receiver
    msg["Date"] = formatdate(localtime=True)

    part = MIMEText(content, "html" if html else "plain", "utf-8")
    msg.attach(part)

    for path, name in attachments:
        try:
            with open(path, "rb") as f:
                mime = MIMEBase("application", "octet-stream")
                mime.set_payload(f.read())
                encoders.encode_base64(mime)
                mime.add_header("Content-Disposition", "attachment",
                                filename=("utf-8", "", name or path.split("/")[-1]))
                msg.attach(mime)
        except Exception as e:
            logger.error(f"附件读取失败 {path}: {e}")

    try:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        result = {"status": "sent", "to": receiver, "subject": subject,
                  "attachments": len(attachments)}
        _log_email(company_id, receiver, subject, result)
        return result
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        result = {"status": "error", "error": str(e), "to": receiver}
        _log_email(company_id, receiver, subject, result)
        return result


def _log_email(company_id, receiver, subject, result):
    """发送记录写入 email_logs（退信处理依据）"""
    try:
        from database import SessionLocal
        from models import EmailLog
        db = SessionLocal()
        try:
            db.add(EmailLog(
                company_id=company_id or 0,
                recipient=receiver,
                subject=(subject or "")[:200],
                status=result.get("status", "unknown"),
                detail=str(result.get("error", ""))[:500],
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
