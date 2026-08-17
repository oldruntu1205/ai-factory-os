"""
channels/wecom/app.py —— 企业微信自建应用（客户消息 -> AI 员工自动回复）

支持企业级配置（company_id 读取 channel_configs.wecom_app），
未配置回退全局 env，仍未配置则 mock 降级。
"""
import time
import requests

from channels.wecom.config import (WECOM_BASE, WECOM_CORP_ID, WECOM_AGENT_ID,
                                   WECOM_SECRET, is_app_configured)
from channels.config_utils import resolve

_token_cache = {"token": None, "expires": 0}


def _app_creds(company_id=None) -> tuple:
    """返回 (corp_id, agent_id, secret, configured)"""
    corp = resolve(company_id, "wecom_app", "wecom_corp_id", WECOM_CORP_ID)
    agent = resolve(company_id, "wecom_app", "wecom_agent_id", WECOM_AGENT_ID)
    secret = resolve(company_id, "wecom_app", "wecom_secret", WECOM_SECRET)
    return corp, agent, secret, bool(corp and agent and secret)


def get_access_token(company_id: int = None) -> str:
    """获取应用 access_token（7000s 缓存）"""
    global _token_cache
    corp, agent, secret, configured = _app_creds(company_id)
    if not configured:
        return ""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires"] > now + 120:
        return _token_cache["token"]
    resp = requests.get(
        f"{WECOM_BASE}/gettoken",
        params={"corpid": corp, "corpsecret": secret},
        timeout=10,
    ).json()
    token = resp.get("access_token", "")
    _token_cache = {"token": token, "expires": now + resp.get("expires_in", 7200)}
    return token


def send_app_text(user_id: str, content: str, company_id: int = None) -> dict:
    """通过自建应用给企业成员发文本消息"""
    corp, agent, secret, configured = _app_creds(company_id)
    if not configured:
        return {"ok": True, "mock": True, "message": "演示模式：未配置企微应用，已模拟发送"}
    token = get_access_token(company_id)
    if not token:
        return {"ok": False, "error": "获取 access_token 失败"}
    payload = {
        "touser": user_id,
        "msgtype": "text",
        "agentid": int(agent),
        "text": {"content": content},
    }
    r = requests.post(f"{WECOM_BASE}/message/send?access_token={token}",
                      json=payload, timeout=10).json()
    return {"ok": r.get("errcode", -1) == 0, "response": r}
