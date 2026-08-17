"""
channels/wecom/config.py —— 企业微信配置（渠道层）

对接能力（按落地手册第 7 章外部渠道）：
1. 群机器人 webhook：AI 日报 / 报价单推送到企微群
2. 自建应用：接收客户消息回调 -> AI 员工自动回复（应用消息）
3. 回调验签：msg_signature + echostr 握手（明文模式）

生产环境在 .env 配置：
WECOM_CORP_ID=wwxxxxxxxx     企业 ID（企微管理后台 -> 我的企业）
WECOM_AGENT_ID=1000002       自建应用 AgentId
WECOM_SECRET=xxxx            自建应用 Secret
WECOM_ROBOT_KEY=xxxx         群机器人 Webhook key（可选）
未配置时全部优雅降级为演示模式（mock 发送成功）。
"""
import os
import hashlib
import hmac
import base64
import struct
import time
import logging

logger = logging.getLogger(__name__)

# ---------- 自建应用 ----------
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID", "")
WECOM_SECRET = os.getenv("WECOM_SECRET", "")
WECOM_TOKEN = os.getenv("WECOM_TOKEN", "")       # 回调 Token
WECOM_AES_KEY = os.getenv("WECOM_AES_KEY", "")   # 回调 EncodingAESKey（明文模式可空）
WECOM_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

# ---------- 群机器人 ----------
WECOM_ROBOT_KEY = os.getenv("WECOM_ROBOT_KEY", "")
WECOM_ROBOT_URL = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                   + (f"?key={WECOM_ROBOT_KEY}" if WECOM_ROBOT_KEY else ""))

# ---------- 智能机器人（长连接 AI Bot，凭证与自建应用不同）----------
WECOM_BOT_ID = os.getenv("WECOM_BOT_ID", "")
WECOM_BOT_SECRET = os.getenv("WECOM_BOT_SECRET", "")


def is_app_configured() -> bool:
    """自建应用是否配置完整"""
    return bool(WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET)


def is_robot_configured() -> bool:
    return bool(WECOM_ROBOT_KEY)


def sign_callback(timestamp: str = "", nonce: str = "", echostr: str = "") -> str:
    """企业微信回调签名（sha1，字典序拼接 token+timestamp+nonce+echostr）"""
    items = sorted([WECOM_TOKEN, timestamp, nonce, echostr])
    s = "".join(items)
    return hashlib.sha1(s.encode()).hexdigest()


def verify_callback(signature: str, timestamp: str, nonce: str, echostr: str = "") -> bool:
    """校验回调签名（恒定时间比较防时序攻击）"""
    if not WECOM_TOKEN:
        return False
    calc = sign_callback(timestamp, nonce, echostr)
    return hmac.compare_digest(calc, signature)


def verify_post_signature(signature: str, timestamp: str, nonce: str, body: str) -> bool:
    """校验 POST 回调签名：sha1(sorted(token, timestamp, nonce, body))（明文模式）"""
    if not WECOM_TOKEN:
        return False
    items = sorted([WECOM_TOKEN, timestamp, nonce, body])
    s = "".join(items)
    calc = hashlib.sha1(s.encode()).hexdigest()
    return hmac.compare_digest(calc, signature)


def decrypt_wecom_msg(encrypt_b64: str, encoding_aes_key: str = None) -> tuple:
    """企微回调密文解密（安全模式）：返回 (msg, receiveid)。

    企微规范：EncodingAESKey base64 解码得 32 字节 key；
    AES-256-CBC 解密（key 前 16 字节为 IV）；PKCS7 去填充；
    明文 = 16 字节随机 + 4 字节网络序消息长度 + 消息 + receiveid。
    未配置 EncodingAESKey 时抛 ValueError（调用方应仅在该模式启用时调用）。
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding

    aes_key = encoding_aes_key or WECOM_AES_KEY
    if not aes_key:
        raise ValueError("未配置 WECOM_AES_KEY，无法解密企微密文回调")
    key = base64.b64decode(aes_key + "=")  # 43 位 key 补 = 还原 32 字节
    iv = key[:16]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    raw = dec.update(base64.b64decode(encrypt_b64)) + dec.finalize()
    # PKCS7 去填充
    pad = raw[-1]
    raw = raw[:-pad] if 1 <= pad <= 32 else raw
    if len(raw) < 20:
        raise ValueError("解密结果过短")
    msg_len = struct.unpack(">I", raw[16:20])[0]
    msg = raw[20:20 + msg_len].decode("utf-8", errors="ignore")
    receiveid = raw[20 + msg_len:].decode("utf-8", errors="ignore")
    return msg, receiveid
