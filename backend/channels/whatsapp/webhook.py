"""
channels/whatsapp/webhook.py —— WhatsApp Webhook 接口（第五阶段 1.5，已完善）

- GET  /whatsapp/webhook   Meta 校验握手
- POST /whatsapp/webhook   接收消息 -> 校验签名 -> 入库 -> AI Agent -> 回复客户
完善项：
  1. X-Hub-Signature-256 签名校验（配置 WHATSAPP_APP_SECRET 后启用）
  2. 消息收发入库（conversations 表，按 phone 维度记录）
  3. 按手机号会话路由 AI 员工（_resolve_company 支持查询绑定）
"""
import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request, HTTPException
import requests

from agents.service import execute_agent
from channels.whatsapp.config import GRAPH_API_URL, WHATSAPP_VERIFY_TOKEN, \
    WHATSAPP_PHONE_ID, WHATSAPP_TOKEN, is_configured
from config import WHATSAPP_APP_SECRET

router = APIRouter(prefix="/whatsapp", tags=["WhatsApp"])
logger = logging.getLogger(__name__)


@router.get("/webhook")
async def verify(request: Request):
    """Meta Cloud API 的 webhook 验证握手（安全：未配置 verify_token 时 fail-closed）"""
    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=503,
                            detail="未配置 WHATSAPP_VERIFY_TOKEN，拒绝握手（请在生产环境配置）")
    params = request.query_params
    if params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
        return int(params.get("hub.challenge", "0"))
    raise HTTPException(status_code=403, detail="verify token mismatch")


async def _check_signature(request: Request, raw: bytes):
    """Meta webhook 签名校验：X-Hub-Signature-256 = sha256(app_secret, raw_body)

    安全策略：未配置 WHATSAPP_APP_SECRET 时拒绝处理（fail-closed），
    防止伪造消息触发 AI 与发送。配置后强制校验 HMAC。
    """
    if not WHATSAPP_APP_SECRET:
        raise HTTPException(
            status_code=503,
            detail="未配置 WHATSAPP_APP_SECRET，无法验证 webhook 签名，已拒绝处理（请在生产环境配置）")
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="缺少签名")
    calc = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, signature):
        raise HTTPException(status_code=401, detail="签名校验失败")


@router.post("/webhook")
async def receive(request: Request):
    """接收 WhatsApp 消息并让 AI Agent 回复（幂等去重 + 入库 + 路由）"""
    if not is_configured():
        raise HTTPException(status_code=503, detail="WhatsApp 未配置，请先设置 token 与 phone_id")

    raw = await request.body()
    await _check_signature(request, raw)
    data = json.loads(raw or b"{}")

    # Meta 通知结构解析
    try:
        entry = data["entry"][0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return {"status": "ok", "handled": False}

        msg = messages[0]
        phone = msg.get("from", "")
        text = msg.get("text", {}).get("body", "")
        msg_id = msg.get("id", "")
        # 通过手机号查客户归属企业（未绑定则拒绝，不再回退演示企业 1）
        company_id = _resolve_company(phone)
    except (KeyError, IndexError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="无效的 WhatsApp 消息结构")

    if not company_id:
        logger.warning("WhatsApp 消息来自未绑定企业的手机号 %s，拒绝处理", phone)
        return {"status": "ok", "handled": False, "reason": "unbound_phone"}

    # 幂等去重：Meta 为至少一次投递，同一 msg_id 重复到达直接跳过（防重复 AI/回复/入库）
    if msg_id and _message_seen(company_id, msg_id):
        return {"status": "ok", "handled": True, "duplicate": True}

    try:
        # 同步 LLM 调用放到线程池，避免阻塞事件循环
        answer = await asyncio.to_thread(execute_agent, text, company_id)
        _save_conversation(company_id, phone, text, answer, msg_id)
        send_whatsapp_message(phone, answer, company_id=company_id)
        return {"status": "ok", "handled": True, "answer": answer}
    except Exception as e:
        # 记录错误并返回 200（避免 Meta 重试造成重复处理）
        logger.error("WhatsApp 消息处理失败: %s", e)
        return {"status": "ok", "handled": False, "error": "internal"}


def _message_seen(company_id: int, msg_id: str) -> bool:
    """幂等检查：msg_id 是否已入库（webhook 至少一次投递去重）"""
    try:
        from database import SessionLocal
        from models import Conversation
        db = SessionLocal()
        try:
            return db.query(Conversation.id).filter(
                Conversation.msg_id == msg_id,
                Conversation.company_id == company_id).first() is not None
        finally:
            db.close()
    except Exception:
        return False


def _save_conversation(company_id: int, phone: str, question: str, answer: str, msg_id: str = ""):
    """把 WhatsApp 往来消息写入 conversations 表（channel 维度，记录 msg_id 供幂等）"""
    try:
        from database import SessionLocal
        from models import Conversation
        db = SessionLocal()
        try:
            db.add(Conversation(
                company_id=company_id,
                session_id=f"whatsapp:{phone}",
                user_message=question,
                agent_answer=answer,
                channel="whatsapp",
                msg_id=msg_id or None,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # 入库失败不影响回复


def _resolve_company(phone: str):
    """手机号 -> 企业映射（优先查 companies 绑定；未绑定返回 None，拒绝处理防跨租户）"""
    try:
        from database import SessionLocal
        from models import Company
        db = SessionLocal()
        try:
            c = db.query(Company).filter(Company.whatsapp_phone == phone).first()
            if c:
                return c.id
        finally:
            db.close()
    except Exception:
        pass
    return None


def send_whatsapp_message(to_phone: str, text: str, company_id: int = None) -> dict:
    """通过 Meta Cloud API 发送消息（支持企业级配置）"""
    from channels.config_utils import resolve
    token = resolve(company_id, "whatsapp", "whatsapp_token", WHATSAPP_TOKEN)
    phone_id = resolve(company_id, "whatsapp", "whatsapp_phone_id", WHATSAPP_PHONE_ID)
    if not (token and phone_id):
        return {"ok": True, "mock": True,
                "message": "演示模式：未配置 WhatsApp（token/phone_id），已模拟发送"}
    url = f"{GRAPH_API_URL}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        try:
            return response.json()
        except Exception:
            return {"status": "error", "code": getattr(response, "status_code", 0),
                    "message": "WhatsApp API 返回非 JSON"}
    except Exception as e:
        return {"status": "error", "message": f"WhatsApp API 连接失败: {e}"}
