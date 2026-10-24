"""
hermes/mcp/schemas.py —— Tool 的 JSON Schema 声明（MCP tools/list 用）

为什么要单独一份：
Tool 的 execute() 收的是自由 dict，MCP 客户端需要明确的 inputSchema 才能
让 LLM 正确填参。这里按工具名声明一次，缺声明时回退通用 object schema。

新增 Tool 时同步在此登记，否则客户端看到的是宽松 schema（能用，但提示不够准）。
"""

_GENERIC = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}

SCHEMAS = {
    "crm.get_customer": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "integer", "description": "客户 ID（可选）"},
            "name": {"type": "string", "description": "客户名称模糊匹配（可选）"},
        },
        "additionalProperties": False,
    },
    "crm.create_customer": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "客户名称"},
            "country": {"type": "string", "description": "国家/地区"},
            "email": {"type": "string", "description": "邮箱"},
            "requirement": {"type": "string", "description": "需求描述"},
            "source": {"type": "string", "description": "来源（默认 hermes_agent）"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "email.send": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人邮箱"},
            "subject": {"type": "string", "description": "邮件主题"},
            "content": {"type": "string", "description": "正文（留空则自动生成开发信）"},
            "company": {"type": "string", "description": "客户公司名（自动生成时用）"},
            "product": {"type": "string", "description": "产品（自动生成时用）"},
        },
        "required": ["to"],
        "additionalProperties": False,
    },
    "quotation.create": {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "客户名称"},
            "product": {"type": "string", "description": "产品"},
            "quantity": {"type": "number", "description": "数量"},
            "unit_price": {"type": "number", "description": "单价"},
            "currency": {"type": "string", "description": "币种，默认 USD"},
            "delivery_days": {"type": "integer", "description": "交期天数，默认 30"},
        },
        "required": ["customer_name", "product"],
        "additionalProperties": False,
    },
    "knowledge.search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题"},
            "limit": {"type": "integer", "description": "返回条数，默认 5"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "erp.get_stock": {
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "产品编号"},
            "erp_url": {"type": "string", "description": "ERP 接口地址（可选）"},
            "token": {"type": "string", "description": "ERP 令牌（可选，审计时自动脱敏）"},
        },
        "required": ["product_id"],
        "additionalProperties": False,
    },
    "mes.get_production": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "生产订单号"},
            "mes_url": {"type": "string", "description": "MES 接口地址（可选）"},
            "token": {"type": "string", "description": "MES 令牌（可选，审计时自动脱敏）"},
        },
        "required": ["order_id"],
        "additionalProperties": False,
    },
    "dashboard.snapshot": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# 明确标记为"写"的工具（MCP 默认只读，写需显式开启 MCP_ALLOW_WRITE）
WRITE_TOOLS = {"crm.create_customer", "email.send", "quotation.create"}


def schema_for(tool_name: str) -> dict:
    return SCHEMAS.get(tool_name, _GENERIC)


def is_write_tool(tool_name: str, permissions=None) -> bool:
    """写工具判定：显式名单优先，其次看权限点是否含 write/send。"""
    if tool_name in WRITE_TOOLS:
        return True
    for perm in (permissions or []):
        tail = str(perm).split(":")[-1].lower()
        if tail in ("write", "send", "delete", "update", "admin"):
            return True
    return False
