"""
channels/email/generator.py —— AI 邮件生成（第五阶段 2.2 + V2.1 开发信）

根据目标客户与产品生成个性化英文开发信。
"""
from ai.llm import get_llm


def create_email(company: str, product: str, extra: str = "") -> str:
    llm = get_llm()
    prompt = f"""
你是一名工业品外贸销售专家。
目标客户：
{company}

产品：
{product}

{extra}

写一封英文开发信。
要求：
专业
简短
个性化
提高回复率
"""
    result = llm.invoke(prompt)
    return result.content
