"""
utils/crypto.py —— 对称加密工具（Fernet AES-128-CBC）

用途：敏感配置（集成凭据/渠道密钥）加密落库，密钥派生自 SECRET_KEY。
- encrypt_secret: 加密 → "enc:<token>"（带前缀便于识别与旧明文兼容）
- decrypt_secret: 解密；无 enc: 前缀按旧明文返回（迁移期兼容）
"""
import base64
import hashlib

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    from config import SECRET_KEY
    key = base64.urlsafe_b64encode(
        hashlib.sha256(SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    """加密敏感值；空值直接返回空串"""
    if not plain:
        return ""
    return "enc:" + _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(stored: str) -> str:
    """解密敏感值；无 enc: 前缀视为旧明文（兼容既有数据）"""
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        return stored
    try:
        return _fernet().decrypt(stored[4:].encode()).decode()
    except Exception:
        return ""  # 密钥变更等异常场景返回空，避免崩溃
