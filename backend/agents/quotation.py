"""
agents/quotation.py —— 报价 Agent（第九章）

根据客户需求生成产品/数量/价格/交期/付款方式。
Soul（人设/系统提示）由企业在后台编辑，注入到 prompt 头部。
"""
from ai.llm import get_llm
from hermes.registry.soul import read_soul


def run_quote(state: dict) -> dict:
    llm = get_llm()
    soul = read_soul(state["company_id"], "quotation_agent")

    prompt = f"""
你是报价助手。
根据客户需求：

{state["message"]}

生成：
产品
数量
单价
总价
交期
付款方式
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    state["result"] = result.content
    return state
