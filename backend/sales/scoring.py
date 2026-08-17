"""
sales/scoring.py —— 客户评分模型（V2.1 第五章，升级版）

评分策略：
1. 优先 LLM 语义评分（综合行业匹配 / 规模 / 进口史 / 互动 / 邮箱可送达），输出分值 + 等级 + 理由
2. LLM 不可用时回退规则评分（calculate_score）

等级：90-100 A级，70-90 B级，<70 C级（低价值）
"""
import json
import re
import logging

from models import Lead
from ai.llm import get_llm

logger = logging.getLogger(__name__)


def calculate_score(data: dict) -> int:
    """规则评分：行业匹配(30) + 采购能力/进口史(30) + 企业规模(20) + 联系质量(20)"""
    score = 0
    if data.get("industry_match"):
        score += 30
    if data.get("has_import"):
        score += 30
    elif data.get("employee_count", 0) > 100:
        score += 15
    if data.get("employee_count", 0) > 50:
        score += 20
    elif data.get("employee_count", 0) > 10:
        score += 10
    if data.get("contact_found"):
        score += 20
    return min(score, 100)


def grade_of(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 70:
        return "B"
    return "C"


def _enrich_data(lead: Lead) -> dict:
    """把 Lead 基础字段 + enriched 合并成评分输入"""
    enriched = lead.enriched or {}
    return {
        "industry": lead.industry or "",
        "country": lead.country or "",
        "employee_count": enriched.get("employee_count", 50 if lead.website else 10),
        "has_import": enriched.get("has_import",
                                   lead.country not in ("", "CN", "中国")),
        "products": enriched.get("products", []),
        "contact_found": bool(lead.email),
        "website": bool(lead.website),
    }


def _rule_reason(data: dict) -> str:
    parts = []
    if data.get("industry_match"):
        parts.append("行业高度匹配")
    if data.get("has_import"):
        parts.append("有进口/采购史")
    elif data.get("employee_count", 0) > 100:
        parts.append("规模较大(>100人)")
    if data.get("employee_count", 0) > 50:
        parts.append("企业规模达标")
    if data.get("contact_found"):
        parts.append("已获取联系邮箱")
    else:
        parts.append("暂缺邮箱需人工补全")
    return "；".join(parts) if parts else "信息不足"


def score_lead_llm(lead: Lead, data: dict):
    """
    LLM 语义评分。成功返回 (reasons:str, score:int)；失败返回 ("", None)。
    输出 JSON：{"score": <0-100>, "grade": "A/B/C", "reasons": "..."}
    """
    try:
        llm = get_llm()
        prompt = f"""你是 B2B 工业外贸的客户价值评估专家。
请基于以下线索评估该客户成为我方（制造业 AI 员工平台 / 工业用品出口商）付费客户的概率与价值。

客户信息：
- 公司：{lead.company_name}
- 国家/地区：{data['country']}
- 行业：{data['industry']}
- 员工规模：{data['employee_count']} 人
- 是否有进口/采购史：{'是' if data['has_import'] else '未知/否'}
- 主营产品线索：{', '.join(data['products']) if data['products'] else '未知'}
- 是否已有联系邮箱：{'是' if data['contact_found'] else '否'}

评估维度（权重参考）：行业匹配度、采购能力与进口史、企业规模、联系方式可触达性。
请输出严格 JSON：
{{"score": <0-100 整数>, "grade": "A或B或C", "reasons": "<50字内的中文评分理由，点列式>"}}"""
        result = llm.invoke(prompt)
        m = re.search(r"\{.*\}", result.content, re.S)
        if not m:
            return "", None
        obj = json.loads(m.group())
        score = int(obj.get("score", 0))
        score = max(0, min(100, score))
        reasons = obj.get("reasons", "")
        # LLM 常把 reasons 输出为 JSON 数组；score_reasons 是 Text 列，
        # 传 list 会导致 SQLite 绑定失败（ProgrammingError），统一转字符串。
        if isinstance(reasons, list):
            reasons = "；".join(str(r) for r in reasons if r)
        elif not isinstance(reasons, str):
            reasons = str(reasons)
        return reasons, score
    except Exception as e:
        logger.debug(f"LLM 评分失败，回退规则: {e}")
        return "", None


def score_lead(lead: Lead) -> Lead:
    """更新线索评分/等级/理由：优先 LLM，失败回退规则"""
    data = _enrich_data(lead)
    data["industry_match"] = bool(lead.industry)
    reasons, llm_score = score_lead_llm(lead, data)
    if llm_score is not None:
        lead.score = llm_score
        lead.grade = grade_of(llm_score)
        lead.score_reasons = reasons or "（LLM 语义评分）"
    else:
        lead.score = calculate_score(data)
        lead.grade = grade_of(lead.score)
        lead.score_reasons = "（规则评分）" + _rule_reason(data)
    return lead
