"""
channels/wecom/robot.py —— 企业微信群机器人（推送 AI 日报 / 报价单）

企业微信群机器人通过 Webhook 发送 markdown / 文本，无需授权 token。
支持企业级配置（company_id 读取 channel_configs.wecom_robot），
未配置时回退全局 env 或返回 mock 成功。
"""
import requests

from channels.wecom.config import WECOM_ROBOT_URL, WECOM_ROBOT_KEY
from channels.config_utils import resolve


def _robot_url(company_id=None) -> tuple:
    """返回 (url, configured) —— 企业配置优先，回退全局 env"""
    key = resolve(company_id, "wecom_robot", "wecom_robot_key", WECOM_ROBOT_KEY)
    if key:
        return (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}", True)
    return (WECOM_ROBOT_URL, False)


def send_robot_markdown(content: str, mentioned: list = None, company_id: int = None) -> dict:
    """向企微群推送 markdown 消息"""
    url, configured = _robot_url(company_id)
    if not configured:
        return {"ok": True, "mock": True, "message": "演示模式：未配置企微群机器人，已模拟发送"}
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content, "mentioned_list": mentioned or []},
    }
    r = requests.post(url, json=payload, timeout=10)
    return {"ok": r.status_code == 200, "response": r.json()}


def send_robot_text(content: str, mentioned: list = None, company_id: int = None) -> dict:
    """向企微群推送纯文本消息"""
    url, configured = _robot_url(company_id)
    if not configured:
        return {"ok": True, "mock": True, "message": "演示模式：未配置企微群机器人，已模拟发送"}
    payload = {
        "msgtype": "text",
        "text": {"content": content, "mentioned_list": mentioned or []},
    }
    r = requests.post(url, json=payload, timeout=10)
    return {"ok": r.status_code == 200, "response": r.json()}


def send_daily_report(report: str, company_id: int = None) -> dict:
    """把 CEO 经营日报推送到企微群（截断保护）"""
    content = report[:3800]
    return send_robot_markdown(f"### 📊 CEO 经营日报\n\n{content}", company_id=company_id)
