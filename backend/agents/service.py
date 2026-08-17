"""
agents/service.py —— Agent 统一入口（第十一章）

execute_agent 兼容两种模式：
1. LangGraph 工作流（意图路由 -> 客服/销售/报价）
2. 指定 agent 角色直调（供 AI 员工选择特定 Agent 使用）

所有 Agent 的 prompt 都自动注入企业级 Soul（人设/边界）。
"""
import json

from sqlalchemy.orm import Session

from agents.workflow import app
from agents.customer import customer_agent
from agents.production import production_agent
from agents.quality import quality_agent
from agents.process import process_agent
from agents.ceo import build_daily_report
from ai.llm import get_llm
from models import (Customer, AIAgent, Conversation, Quotation,
                    ProductionOrder, QualityReport)
from hermes.registry.soul import read_soul


def execute_agent(message: str, company_id: int, customer_id: int = None,
                  intent: str = None) -> str:
    """走 LangGraph 工作流（自动意图路由）"""
    state = {
        "message": message,
        "company_id": company_id,
        "customer_id": customer_id,
        "intent": intent or "",
        "result": "",
    }
    result = app.invoke(state)
    return result.get("result", "")


def execute_specialized(agent: AIAgent, message: str, company_id: int,
                        db: Session, customer_id: int = None, tools=None) -> str:
    """按 AI 员工角色直调对应 Agent（支持完整角色矩阵）。

    tools: 可选 ToolCenter，多步 Agent（生产/质量）会用来先查 MES/ERP 实时数据。
    """
    role = agent.role
    if role == "customer":
        return customer_agent(message, company_id)
    if role == "sales":
        return _sales_with_lead(message, company_id, db, customer_id)
    if role == "quotation":
        return _quotation_with_record(message, company_id, db, customer_id)
    if role == "production":
        orders = db.query(ProductionOrder).filter(
            ProductionOrder.company_id == company_id).limit(10).all()
        data = [{"order_no": o.order_no, "product": o.product, "quantity": o.quantity,
                 "progress": o.progress, "line": o.line, "load_rate": o.load_rate,
                 "delay": o.delay, "deadline_days": o.deadline_days} for o in orders]
        return production_agent(data, company_id, tools=tools)
    if role == "quality":
        reports = db.query(QualityReport).filter(
            QualityReport.company_id == company_id).limit(10).all()
        data = [{"line": r.line, "defect_rate": r.defect_rate,
                 "defect_type": r.defect_type} for r in reports]
        from agents.quality import quality_agent as qa
        return qa(data, company_id, tools=tools)
    if role == "process":
        # 工艺/图纸助手：结构化参数 + 混合检索（可带 db 做精确查）
        return process_agent(message, company_id, tools=tools, db=db)
    if role == "ceo":
        return build_daily_report(db, company_id)
    # 兜底
    return execute_agent(message, company_id, customer_id)


def _sales_with_lead(message: str, company_id: int, db: Session, customer_id: int = None) -> str:
    """销售 Agent + 自动创建线索（自动注入 Soul）"""
    llm = get_llm()
    soul = read_soul(company_id, "sales_agent")
    prompt = f"""
你是工业产品销售专家。
分析客户需求并输出 JSON：

{message}

JSON 格式：
{{"name": "客户名称", "country": "国家", "email": "邮箱(可选)",
  "product": "产品", "quantity": 数量, "intent": "高/中/低", "advice": "建议"}}
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    content = result.content
    try:
        parsed = _extract_json(content)
        if parsed and customer_id:
            # RBAC Step3：销售写 CRM 前校验租户权限（crm:write）——默认集通过，revoke 后不写库
            try:
                from hermes.runtime import get_runtime
                get_runtime().permission_system.assert_tool_permission(
                    company_id, "crm.create_customer", "crm:write")
            except PermissionError as e:
                return f"企业未授权 CRM 写入权限（crm:write），未创建/更新线索。{e}"
            except Exception:
                pass  # runtime 未初始化时不拦截
            # 更新或创建 CRM 客户（线索）
            cust = db.query(Customer).filter(Customer.id == customer_id).first()
            if not cust:
                cust = Customer(company_id=company_id)
                db.add(cust)
            cust.name = parsed.get("name") or cust.name or "未命名客户"
            cust.country = parsed.get("country") or cust.country or ""
            cust.email = parsed.get("email") or cust.email or ""
            cust.requirement = f"{parsed.get('product', '')} x {parsed.get('quantity', '')}"
            cust.source = "agent"
            db.commit()
        return content
    except Exception:
        return content


def _quotation_with_record(message: str, company_id: int, db: Session, customer_id: int = None) -> str:
    """报价 Agent + 记录报价单（自动注入 Soul）"""
    llm = get_llm()
    soul = read_soul(company_id, "quotation_agent")
    prompt = f"""
你是报价助手。
根据客户需求生成报价并输出 JSON：

{message}

JSON 格式：
{{"product": "产品", "quantity": 数量, "unit_price": 单价,
  "delivery_days": 交期天数, "payment_terms": "付款方式"}}
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    content = result.content
    try:
        parsed = _extract_json(content)
        if parsed:
            # RBAC Step3：报价写 Quotation 前校验租户权限（quotation:write）——默认集通过，revoke 后不写库
            try:
                from hermes.runtime import get_runtime
                get_runtime().permission_system.assert_tool_permission(
                    company_id, "quotation.create", "quotation:write")
            except PermissionError as e:
                return f"企业未授权报价写入权限（quotation:write），未生成报价单。{e}"
            except Exception:
                pass  # runtime 未初始化时不拦截
            import uuid
            cust = db.query(Customer).filter(Customer.id == customer_id).first() if customer_id else None
            quotation = Quotation(
                quotation_no=f"QT{company_id}{uuid.uuid4().hex[:8].upper()}",
                customer_id=customer_id,
                customer_name=cust.name if cust else "未命名客户",
                product=str(parsed.get("product", "")),
                quantity=int(parsed.get("quantity", 1) or 1),
                unit_price=float(parsed.get("unit_price", 0) or 0),
                total_amount=float(parsed.get("quantity", 1) or 1) * float(parsed.get("unit_price", 0) or 0),
                delivery_days=int(parsed.get("delivery_days", 30) or 30),
                payment_terms=str(parsed.get("payment_terms", "T/T 30% deposit")),
                status="draft",
                company_id=company_id,
            )
            db.add(quotation)
            db.commit()
            if cust:
                cust.status = "quoted"
                db.commit()
            content += f"\n\n[已生成报价单: {quotation.quotation_no}]"
        return content
    except Exception:
        return content


def _extract_json(content: str) -> dict:
    """从 LLM 输出中提取 JSON"""
    content = content.strip()
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        return json.loads(content[start:end + 1])
    return {}
