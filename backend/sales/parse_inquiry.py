"""
sales/parse_inquiry.py —— 询盘智能解析（出口型制造业 AI 应用 第3项：真实询盘导入与解析）

把真实客户询盘文本解析为结构化字段（产品/数量/国家/预算/意向），
供 /leads/import 导入后评分与自动首封。
"""
import logging

from ai.llm import get_llm
from services.llm_parse import parse_json_output, safe_int, safe_str

logger = logging.getLogger(__name__)


def parse_inquiry(text: str) -> dict:
    """LLM 解析询盘文本 -> {product, quantity, country, budget, intent, notes}"""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        llm = get_llm()
        prompt = f"""你是外贸业务分析助手。从客户询盘中提取关键信息，只输出 JSON：

询盘原文：
{text[:1500]}

JSON 格式：
{{"product": "产品（中文名）", "quantity": 数量(数字，未知给 null),
  "country": "目的国代码(如 DE/US/AE，未知给 '')", "budget": "预算描述(如有)",
  "intent": "high/medium/low（采购意向强度）", "notes": "关键要点(如认证要求/交期/包装)"}}
"""
        result = llm.invoke(prompt)
        parsed = parse_json_output(result.content, {
            "product": "", "quantity": None, "country": "",
            "budget": "", "intent": "medium", "notes": "",
        })
        return {
            "product": safe_str(parsed.get("product")),
            "quantity": safe_int(parsed.get("quantity"), None) if parsed.get("quantity") else None,
            "country": safe_str(parsed.get("country")).upper(),
            "budget": safe_str(parsed.get("budget")),
            "intent": safe_str(parsed.get("intent"), "medium").lower(),
            "notes": safe_str(parsed.get("notes")),
        }
    except Exception as e:
        logger.warning("询盘解析失败: %s", e)
        return {}
