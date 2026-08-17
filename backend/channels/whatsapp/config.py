"""
channels/whatsapp/config.py —— WhatsApp 配置（第五阶段 1.4）
"""
from config import WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_VERIFY_TOKEN

# Meta Cloud API
GRAPH_API_URL = "https://graph.facebook.com/v19.0"


def is_configured() -> bool:
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)
