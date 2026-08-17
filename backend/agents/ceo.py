"""
agents/ceo.py —— CEO Agent 企业大脑（V2.0 第九章）

自动生成企业经营日报：销售/生产/质量/库存/AI 建议。
"""
from sqlalchemy.orm import Session

from ai.llm import get_llm
from models import (Customer, Quotation, ProductionOrder,
                    QualityReport, Conversation)
from hermes.registry.soul import read_soul


def build_daily_report(db: Session, company_id: int) -> str:
    """基于企业实时数据生成经营日报"""
    inquiries = db.query(Customer).filter(Customer.company_id == company_id).count()
    quotations = db.query(Quotation).filter(Quotation.company_id == company_id).count()
    won = db.query(Quotation).filter(
        Quotation.company_id == company_id,
        Quotation.status == "accepted").count()
    orders = db.query(ProductionOrder).filter(ProductionOrder.company_id == company_id).count()
    delay_orders = db.query(ProductionOrder).filter(
        ProductionOrder.company_id == company_id,
        ProductionOrder.delay.is_(True)).count()
    quality_rows = db.query(QualityReport).filter(
        QualityReport.company_id == company_id).all()
    conversations = db.query(Conversation).filter(
        Conversation.company_id == company_id).count()

    avg_defect = round(sum(r.defect_rate for r in quality_rows) / len(quality_rows), 2) if quality_rows else 0

    summary = {
        "新增询盘": inquiries,
        "报价单": quotations,
        "成交订单": won,
        "生产订单": orders,
        "生产风险": delay_orders,
        "平均不良率%": avg_defect,
        "AI对话量": conversations,
    }

    llm = get_llm()
    soul = read_soul(company_id, "ceo_agent")
    prompt = f"""
你是企业 CEO 的经营分析助手。
根据今日企业数据生成经营日报：

{summary}

输出：
销售情况
生产情况
质量情况
风险提示
明日建议
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    return result.content
