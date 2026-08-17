"""
constants/status.py —— 业务状态枚举（唯一数据源）

把散落在 API 层的字符串状态集中管理：
- 避免拼写错误产生脏数据
- API 入参白名单校验统一引用
- 未来迁移 Enum 或加多语言展示时只改这里
"""

# AI 员工状态
AGENT_STATUS = {"active", "disabled"}

# 客户状态（CRM 漏斗）
CUSTOMER_STATUS = {"new", "following", "quoted", "won", "lost"}

# 线索状态（AI 自主销售）
LEAD_STATUS = {"discovered", "contacted", "replied", "qualified", "won", "lost"}

# 报价单状态
QUOTATION_STATUS = {"draft", "sent", "accepted", "rejected"}

# 文档状态（知识库）
DOCUMENT_STATUS = {"parsed", "vectorized", "failed"}

# 账单状态（计费）
BILL_STATUS = {"unpaid", "paid", "cancelled"}

# 成员状态
MEMBER_STATUS = {"active", "disabled"}

# 成员邀请状态
INVITE_STATUS = {"pending", "accepted", "expired"}

# 邮件日志状态
EMAIL_STATUS = {"sent", "simulated", "error", "bounced"}


def assert_valid(value: str, allowed: set, field: str = "status") -> None:
    """校验状态值，非法时抛出 ValueError（API 层转 400）"""
    if value not in allowed:
        raise ValueError(f"{field} 无效，可选: {sorted(allowed)}")
