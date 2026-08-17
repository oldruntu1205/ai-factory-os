"""
channels/config_utils.py —— 渠道配置读取（企业级优先 → 全局 env 回退）

客户在「渠道中心」保存自己的企微/WhatsApp/邮件配置到 channel_configs 表。
发送/回调时按 company_id 优先读取企业配置，未配置则回退服务器 .env 全局配置。
"""
import json
import logging

logger = logging.getLogger(__name__)

# 各渠道支持的配置键
CHANNEL_KEYS = {
    "wecom_robot": ["wecom_robot_key"],
    "wecom_app": ["wecom_corp_id", "wecom_agent_id", "wecom_secret", "wecom_token"],
    "wecom_bot": ["wecom_bot_id", "wecom_bot_secret"],
    "whatsapp": ["whatsapp_token", "whatsapp_phone_id",
                 "whatsapp_verify_token", "whatsapp_app_secret"],
    "email": ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from"],
}

# 敏感字段（GET 时脱敏）
_SECRET_KEYS = {"wecom_secret", "wecom_token", "wecom_bot_secret", "whatsapp_token",
                "whatsapp_app_secret", "smtp_password", "wecom_robot_key"}


def get_company_config(company_id: int, channel: str) -> dict:
    """读取企业级渠道配置（未存则空 dict）"""
    if not company_id:
        return {}
    try:
        from database import SessionLocal
        from models import ChannelConfig
        db = SessionLocal()
        try:
            row = db.query(ChannelConfig).filter(
                ChannelConfig.company_id == company_id,
                ChannelConfig.channel == channel).first()
            if not row or not row.config_json:
                return {}
            return json.loads(row.config_json)
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"读取企业渠道配置失败 {channel}: {e}")
        return {}


def save_company_config(company_id: int, channel: str, config: dict) -> dict:
    """保存/覆盖企业级渠道配置（只保留该渠道允许的键）"""
    allowed = CHANNEL_KEYS.get(channel, [])
    clean = {k: (str(v).strip() if v is not None else "")
             for k, v in config.items() if k in allowed}
    try:
        from database import SessionLocal
        from models import ChannelConfig
        db = SessionLocal()
        try:
            row = db.query(ChannelConfig).filter(
                ChannelConfig.company_id == company_id,
                ChannelConfig.channel == channel).first()
            if row:
                row.config_json = json.dumps(clean, ensure_ascii=False)
            else:
                db.add(ChannelConfig(company_id=company_id, channel=channel,
                                     config_json=json.dumps(clean, ensure_ascii=False)))
            db.commit()
            return {"saved": True, "channel": channel, "keys": list(clean.keys())}
        finally:
            db.close()
    except Exception as e:
        logger.error(f"保存企业渠道配置失败: {e}")
        return {"saved": False, "error": str(e)}


def mask_secrets(config: dict) -> dict:
    """脱敏：密钥类字段只显示后 4 位（前端回显友好）"""
    out = {}
    for k, v in config.items():
        s = str(v)
        if k in _SECRET_KEYS and s:
            out[k] = "*" * 8 + s[-4:]
        else:
            out[k] = s
    return out


def is_configured_company(company_id: int, channel: str, global_check=None) -> bool:
    """企业级是否已配置（含全局 env 回退）"""
    cfg = get_company_config(company_id, channel)
    if any(v for v in cfg.values()):
        return True
    if global_check:
        try:
            return bool(global_check())
        except Exception:
            return False
    return False


def resolve(company_id: int, channel: str, key: str, env_default: str = "") -> str:
    """取配置值：企业配置优先，缺省回退 env 默认"""
    cfg = get_company_config(company_id, channel)
    if key in cfg and cfg[key]:
        return cfg[key]
    return env_default
