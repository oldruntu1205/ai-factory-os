"""
auth/jwt.py —— JWT 生成与校验（第八章）
"""
from datetime import datetime, timedelta

from jose import jwt, JWTError

from config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS


def create_token(user_id: int, company_id: int = None, role: str = "admin") -> str:
    """签发访问令牌"""
    payload = {
        "sub": str(user_id),
        "company_id": company_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """校验并解析令牌，失败抛出 JWTError"""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
