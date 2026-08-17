"""
constants/plans.py —— SaaS 套餐定义（唯一数据源）

billing.py / system.py / 前端套餐卡片共用此定义，
避免多份配置不同步（历史问题：billing 与 system 各维护一份）。
"""

# 套餐定义（两层计费模型）
#
# 底座套餐 = 「平台席位包」：只决定平台容量上限与平台级能力，
#   不直接"送"具体 AI 员工；AI 员工席位由技能包在席位内填充。
# 技能包（marketplace）在席位内安装行业 AI 员工 + 工具，单独按月计费。
# 两者是「容器（容量）」与「内容（能力）」关系，互不冲突。
PLANS = {
    "starter": {
        "name": "Starter", "price": 1999, "currency": "CNY", "period": "月",
        "max_agents": 1, "max_conversations": 1000,
        "features": ["1 个 AI 员工席位", "1000 次对话/月", "企业知识库（基础）", "标准邮件支持"],
    },
    "professional": {
        "name": "Professional", "price": 4999, "currency": "CNY", "period": "月",
        "max_agents": 5, "max_conversations": 10000,
        "features": ["5 个 AI 员工席位", "1 万次对话/月", "多渠道接入（WhatsApp/企微）", "CRM + AI 报价", "优先支持"],
    },
    "enterprise": {
        "name": "Enterprise", "price": 200000, "currency": "CNY", "period": "年",
        "max_agents": 50, "max_conversations": 1000000,
        "features": ["50 个 AI 员工席位", "百万级对话", "私有化部署", "ERP 连接 + 定制 Agent", "专属客户成功"],
    },
}

# 套餐购买周期（天）——升级时用于计算到期时间
PLAN_DAYS = {"starter": 365, "professional": 365, "enterprise": 365}


def plan_list() -> list:
    """轻量套餐列表（system.py 兼容格式）"""
    return [{"name": p["name"], "price": f"{p['price']}元/{p['period']}",
             "features": p["features"]} for p in PLANS.values()]
