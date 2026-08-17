"""
agents/sales.py —— 销售 Agent（第八章）

识别客户、产品、国家、需求，输出结构化销售分析。
Soul（人设/系统提示）由企业在后台编辑，注入到 prompt 头部。
"""
from ai.llm import get_llm
from hermes.registry.soul import read_soul


def run_sales(state: dict) -> dict:
    llm = get_llm()
    soul = read_soul(state["company_id"], "sales_agent")

    prompt = f"""
你是工业产品销售专家。
分析客户需求：

{state["message"]}

输出：
产品
数量
国家
购买意向
建议
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    state["result"] = result.content
    return state
