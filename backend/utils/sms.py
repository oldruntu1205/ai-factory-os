"""
utils/sms.py —— 手机验证码生成与发送

安全修复（2026-08）：
- 验证码改为服务端随机 6 位，接口永不回显（此前固定 123456 且明文返回，验证码机制形同虚设）
- 演示模式（SMS_MOCK_MODE=1）验证码写入服务端日志，便于本地联调；生产模式必须接入真实短信网关
"""
import logging
import secrets

from config import SMS_MOCK_MODE

logger = logging.getLogger(__name__)


def generate_code() -> str:
    """生成随机 6 位数字验证码"""
    return f"{secrets.randbelow(1000000):06d}"


def send_sms(mobile: str, code: str, purpose: str = "") -> dict:
    """发送短信验证码：演示模式写日志；生产接入真实短信网关（阿里云/腾讯云 SMS）"""
    if SMS_MOCK_MODE:
        logger.info("[SMS 演示模式] purpose=%s 验证码 %s -> %s（接口不回显；生产由短信网关发送）",
                    purpose, code, mobile)
        return {"status": "mock"}
    # TODO: 接入真实短信网关（阿里云/腾讯云 SMS）；未接入时验证码仅落库不发送
    logger.warning("[SMS] 未接入真实短信网关，验证码（purpose=%s）仅落库不实际发送", purpose)
    return {"status": "unsent"}
