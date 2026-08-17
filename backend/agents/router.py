"""
agents/router.py —— AI 意图识别（第六章）

判断客户需求属于 customer / sales / quotation 中的哪一类。
"""
from ai.llm import get_llm


def intent_router(state: dict) -> str:
    """返回意图标签：customer / sales / quotation（生产/质量等走 industrial）"""
    llm = get_llm()
    prompt = f"""
判断客户意图。
只能返回以下之一：
customer
sales
quotation

客户消息：
{state["message"]}

判断规则：
- 询问产品/服务信息、售后、使用方法 -> customer
- 有明确采购意向、数量、询价 -> sales
- 要求报价、价格、交期、付款方式 -> quotation
"""
    result = llm.invoke(prompt)
    intent = result.content.strip().lower()
    if intent not in ("customer", "sales", "quotation"):
        intent = "customer"
    return intent
