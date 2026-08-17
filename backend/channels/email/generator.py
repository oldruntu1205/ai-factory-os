"""
channels/email/generator.py —— AI 邮件生成（第五阶段 2.2 + V2.1 开发信）

根据目标客户与产品生成多语种开发信（多语种全链路：按客户国家路由语言）。
"""
from ai.llm import get_llm
from utils.i18n import LANGUAGE_NAMES


def create_email(company: str, product: str, extra: str = "",
                 language: str = "en") -> str:
    """生成开发信（language: 语言代码，如 en/de/es/ar...）"""
    llm = get_llm()
    lang_name = LANGUAGE_NAMES.get((language or "en").lower(), "English")
    prompt = f"""
你是一名工业品外贸销售专家。
目标客户：
{company}

产品：
{product}

{extra}

写一封{lang_name}开发信。
要求：
专业
简短
个性化
提高回复率
用{lang_name}撰写正文（邮件主题可保留英文关键词）。
"""
    result = llm.invoke(prompt)
    return result.content
