"""
channels/wecom/webhook.py —— 企业微信回调接口

- GET  /wecom/callback?msg_signature=..&timestamp=..&nonce=..&echostr=..
      配置回调 URL 时企业微信发来的验证握手（回显 echostr 原文）
- POST /wecom/callback  接收客户消息（明文模式）-> AI 员工 -> 应用消息回复
- POST /wecom/robot/send 向企微群推送（AI 日报/报价，仅登录用户 + 按企业配置）
"""
import asyncio
import json
import logging
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import PlainTextResponse

from agents.service import execute_agent
from auth.deps import get_current_user
from channels.wecom.config import (verify_callback, verify_post_signature,
                                   WECOM_TOKEN, WECOM_ROBOT_KEY,
                                   is_app_configured, is_robot_configured)
from channels.wecom.robot import send_robot_markdown, send_robot_text
from channels.config_utils import resolve

router = APIRouter(prefix="/wecom", tags=["企业微信"])
logger = logging.getLogger(__name__)

# robot/send 加固：仅允许 text/markdown，内容限长
_ALLOWED_ROBOT_TYPES = {"text", "markdown"}
_MAX_ROBOT_CONTENT = 4000


@router.get("/callback")
async def verify(request: Request):
    """企业微信回调 URL 验证握手（回显 echostr）"""
    q = request.query_params
    signature = q.get("msg_signature", "")
    timestamp = q.get("timestamp", "")
    nonce = q.get("nonce", "")
    echostr = q.get("echostr", "")

    if not WECOM_TOKEN:
        # 安全策略：未配置回调 Token 时拒绝握手（fail-closed），防止公网暴露被绕过
        raise HTTPException(status_code=503,
                            detail="未配置 WECOM_TOKEN，无法验证回调签名（请在生产环境配置）")

    if not verify_callback(signature, timestamp, nonce, echostr):
        raise HTTPException(status_code=403, detail="签名校验失败")
    # 安全修复：企微要求响应体必须是 echostr 原文（JSON 包装会导致 URL 验证握手失败）
    return PlainTextResponse(echostr)


@router.post("/callback")
async def receive(request: Request):
    """接收企业微信客户消息（明文模式 XML）-> AI 员工自动回复"""
    if not is_app_configured():
        raise HTTPException(status_code=503, detail="企微应用未配置（WECOM_CORP_ID/AGENT_ID/SECRET）")

    # 安全策略：POST 回调同样强制验签（防伪造客户消息/借用应用消息发信）
    q = request.query_params
    signature = q.get("msg_signature", "")
    timestamp = q.get("timestamp", "")
    nonce = q.get("nonce", "")
    body = await request.body()
    body_text = body.decode("utf-8", errors="ignore")
    if not verify_post_signature(signature, timestamp, nonce, body_text):
        raise HTTPException(status_code=403, detail="签名校验失败")

    # 解析 XML（企业微信明文模式返回 XML；JSON 模式兼容处理）
    try:
        if body_text.strip().startswith("<"):
            root = ET.fromstring(body_text)
            content = root.findtext("Content", "") or ""
            from_user = root.findtext("FromUserName", "")
            msg_type = root.findtext("MsgType", "text")
        else:
            data = json.loads(body_text)
            content = data.get("Content") or data.get("content") or ""
            from_user = data.get("FromUserName") or data.get("from") or ""
            msg_type = data.get("MsgType", "text")
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析企微消息")

    if msg_type not in ("text", "event"):
        return {"status": "ok", "handled": False}
    # event 空消息不调 LLM
    if msg_type == "event" or not content.strip():
        return {"status": "ok", "handled": False}

    # 按企微用户绑定表解析企业（未绑定拒绝，不再硬编码企业 1）
    company_id = _resolve_company(from_user)
    if not company_id:
        logger.warning("企微消息来自未绑定企业的用户 %s，拒绝处理", from_user)
        return {"status": "ok", "handled": False, "reason": "unbound_user"}

    try:
        # 同步 LLM 调用放到线程池，避免阻塞事件循环
        answer = await asyncio.to_thread(execute_agent, content, company_id)
        # 通过应用消息回复客户（使用企业级配置）
        from channels.wecom.app import send_app_text
        send_app_text(from_user, answer, company_id=company_id)
        return {"status": "ok", "handled": True, "answer": answer}
    except Exception as e:
        logger.error("企微消息处理失败: %s", e)
        return {"status": "ok", "handled": False, "error": "internal"}


@router.post("/robot/send")
async def robot_send(data: dict, current: dict = Depends(get_current_user)):
    """向企微群推送消息（AI 日报 / 报价单）——仅登录用户；按企业配置发送"""
    content = (data or {}).get("content", "")
    msgtype = (data or {}).get("msgtype", "markdown")
    if not content:
        raise HTTPException(status_code=400, detail="content 必填")
    if msgtype not in _ALLOWED_ROBOT_TYPES:
        raise HTTPException(status_code=400, detail="msgtype 仅支持 text/markdown")
    if len(content) > _MAX_ROBOT_CONTENT:
        raise HTTPException(status_code=400, detail=f"内容不能超过 {_MAX_ROBOT_CONTENT} 字符")

    # 按当前企业解析机器人 key（未配置拒绝，不用全局 key 越权发送）
    key = resolve(current.get("company_id"), "wecom_robot", "wecom_robot_key", WECOM_ROBOT_KEY)
    if not key:
        raise HTTPException(status_code=400, detail="当前企业未配置企微机器人，无法推送")

    if msgtype == "text":
        result = send_robot_text(content)
    else:
        result = send_robot_markdown(content)
    return result


def _resolve_company(user_id: str):
    """企微用户 -> 企业映射（查 companies.wecom_user_map 绑定；未绑定返回 None）"""
    try:
        from database import SessionLocal
        from models import Company
        db = SessionLocal()
        try:
            rows = db.query(Company.id, Company.wecom_user_map).filter(
                Company.wecom_user_map.isnot(None)).all()
            for cid, mapping in rows:
                if not mapping:
                    continue
                if isinstance(mapping, dict) and user_id in mapping:
                    return cid
                if isinstance(mapping, list) and user_id in mapping:
                    return cid
        finally:
            db.close()
    except Exception:
        pass
    return None
