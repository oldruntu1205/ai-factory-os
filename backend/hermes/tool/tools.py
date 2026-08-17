"""
hermes/tool/tools.py —— Tool 定义与内置工具集（第6章）

Agent 不能直接操作企业系统，必须通过 Tool。
每个 Tool：name / description / permission / execute()

内置工具：
- crm.get_customer      读取客户
- crm.create_customer   创建线索
- email.send            发送邮件
- quotation.create      创建报价
- knowledge.search      RAG 检索
- erp.get_stock         查库存
- mes.get_production    查生产状态
"""
from abc import ABC, abstractmethod
from typing import Callable, Dict


class Tool(ABC):
    """Tool 基类：Agent 访问企业系统的唯一通道"""
    name: str = ""
    description: str = ""
    permissions: list = []          # 所需权限点（RBAC 校验）

    @abstractmethod
    def execute(self, params: dict, company_id: int) -> dict:
        ...


class FunctionTool(Tool):
    """函数式 Tool：用普通函数快速注册"""

    def __init__(self, name: str, description: str, func: Callable,
                 permissions: list = None):
        self.name = name
        self.description = description
        self._func = func
        self.permissions = permissions or []

    def execute(self, params: dict, company_id: int) -> dict:
        try:
            result = self._func(params=params, company_id=company_id)
            return {"ok": True, "tool": self.name, "data": result}
        except Exception as e:
            return {"ok": False, "tool": self.name, "error": str(e)}


# ============ 内置工具实现 ============

def _crm_get_customer(params, company_id):
    from database import SessionLocal
    from models import Customer
    db = SessionLocal()
    try:
        customers = db.query(Customer).filter(Customer.company_id == company_id)
        if params.get("customer_id"):
            customers = customers.filter(Customer.id == params["customer_id"])
        if params.get("name"):
            customers = customers.filter(Customer.name.like(f"%{params['name']}%"))
        return [{"id": c.id, "name": c.name, "country": c.country,
                 "email": c.email, "status": c.status, "requirement": c.requirement}
                for c in customers.limit(10).all()]
    finally:
        db.close()


def _crm_create_customer(params, company_id):
    from database import SessionLocal
    from models import Customer
    db = SessionLocal()
    try:
        cust = Customer(
            name=params.get("name", "未命名客户"),
            country=params.get("country", ""),
            email=params.get("email", ""),
            requirement=params.get("requirement", ""),
            source=params.get("source", "hermes_agent"),
            company_id=company_id,
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        return {"customer_id": cust.id, "name": cust.name}
    finally:
        db.close()


def _email_send(params, company_id):
    from channels.email.generator import create_email
    from channels.email.smtp import send_email
    receiver = params.get("to", "")
    if not receiver:
        return {"error": "缺少收件人"}
    content = params.get("content") or create_email(
        params.get("company", "Prospect"), params.get("product", "products"))
    result = send_email(receiver, content, params.get("subject", "Business Cooperation"))
    return result


def _quotation_create(params, company_id):
    from database import SessionLocal
    from models import Quotation
    import uuid
    db = SessionLocal()
    try:
        qty = int(params.get("quantity", 1) or 1)
        price = float(params.get("unit_price", 0) or 0)
        quote = Quotation(
            quotation_no=f"QT{company_id}{uuid.uuid4().hex[:8].upper()}",
            customer_name=params.get("customer_name", ""),
            product=params.get("product", ""),
            quantity=qty,
            unit_price=price,
            total_amount=qty * price,
            currency=params.get("currency", "USD"),
            delivery_days=int(params.get("delivery_days", 30) or 30),
            status="draft",
            company_id=company_id,
        )
        db.add(quote)
        db.commit()
        db.refresh(quote)
        return {"quotation_no": quote.quotation_no, "total_amount": quote.total_amount}
    finally:
        db.close()


def _knowledge_search(params, company_id):
    from knowledge.hybrid import hybrid_search
    question = params.get("query", "")
    if not question:
        return {"error": "缺少检索问题"}
    results = hybrid_search(question, company_id, limit=params.get("limit", 5))
    return results


def _erp_get_stock(params, company_id):
    # 演示数据（真实环境接入 SAP/金蝶 Connector）
    # [GEPA 代码进化 · 真实 LLM] 增强容错、参数校验与结构化返回
    import logging
    from connectors.erp.sap import SAPConnector

    logger = logging.getLogger(__name__)
    
    # 增强参数校验：params 本身可能为 None 或非 dict
    if not isinstance(params, dict):
        logger.error("params 参数非法，company_id=%s, params_type=%s", 
                     company_id, type(params).__name__)
        return {
            "product_id": "",
            "stock": 0,
            "source": "error",
            "note": "params 参数必须为 dict 类型",
            "success": False,
        }

    product_id = params.get("product_id", "")
    url = params.get("erp_url", "")
    token = params.get("token", "")

    # 参数校验：product_id 必填，且去除首尾空白
    if not product_id or not str(product_id).strip():
        logger.warning("product_id 为空或全空白，company_id=%s", company_id)
        return {
            "product_id": "",
            "stock": 0,
            "source": "error",
            "note": "product_id 不能为空",
            "success": False,
        }
    product_id = str(product_id).strip()

    # 未配置 URL 时返回演示数据
    if not url:
        logger.info("未配置 ERP URL，返回演示库存，company_id=%s, product_id=%s",
                    company_id, product_id)
        return {
            "product_id": product_id,
            "stock": 5000,
            "source": "demo",
            "note": "未配置 ERP，返回演示库存",
            "success": True,
        }

    # 配置了 URL 但缺少 token 时，记录警告但仍尝试连接
    if not token:
        logger.warning("ERP URL 已配置但 token 为空，company_id=%s", company_id)

    try:
        conn = SAPConnector(url, token)
        result = conn.get_stock(product_id)

        # 校验返回结果
        if result is None or not isinstance(result, dict):
            logger.error("ERP 返回格式异常，company_id=%s, product_id=%s, result=%s",
                         company_id, product_id, result)
            return {
                "product_id": product_id,
                "stock": 0,
                "source": "error",
                "note": "ERP 返回数据格式异常",
                "success": False,
            }

        # 确保 stock 字段存在且为数值，并处理字符串数字（如 "100"）
        stock = result.get("stock", 0)
        if isinstance(stock, str):
            try:
                stock = float(stock)
                logger.info("stock 字段为字符串，已转换为数值，company_id=%s, product_id=%s, stock=%s",
                            company_id, product_id, stock)
            except ValueError:
                logger.warning("stock 字段字符串无法转换为数值，company_id=%s, product_id=%s, stock=%s",
                               company_id, product_id, stock)
                stock = 0
        elif not isinstance(stock, (int, float)):
            logger.warning("stock 字段非数值，company_id=%s, product_id=%s, stock=%s",
                           company_id, product_id, stock)
            stock = 0

        # 确保 stock 不为负数（业务合理性校验）
        if stock < 0:
            logger.warning("stock 为负数，重置为 0，company_id=%s, product_id=%s, stock=%s",
                           company_id, product_id, stock)
            stock = 0

        logger.info("ERP 查询成功，company_id=%s, product_id=%s, stock=%s",
                    company_id, product_id, stock)
        return {
            "product_id": product_id,
            "stock": stock,
            "source": "sap",
            "note": "从 SAP 获取库存",
            "success": True,
        }

    except Exception as e:
        logger.error("ERP 查询异常，company_id=%s, product_id=%s, error=%s",
                     company_id, product_id, str(e))
        return {
            "product_id": product_id,
            "stock": 0,
            "source": "error",
            "note": f"ERP 查询失败: {str(e)}",
            "success": False,
        }


def _mes_get_production(params, company_id):
    import logging
    from connectors.mes.mes_api import MESConnector

    logger = logging.getLogger(__name__)
    logger.info("MES production query started | company_id=%s", company_id)

    # --- 参数校验与默认值 ---
    order_id = params.get("order_id", "")
    if not order_id or not isinstance(order_id, str):
        logger.warning("Invalid or missing order_id, falling back to demo data | order_id=%r", order_id)
        return {
            "order": "ORD10001",
            "progress": 72,
            "line": "A01",
            "delay": False,
            "source": "demo",
            "error": "invalid_order_id"
        }

    url = params.get("mes_url", "")
    token = params.get("token", "")

    # --- 无 URL 时返回 demo 数据（带明确标记） ---
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        logger.info("No valid MES URL provided, using demo data | url=%r", url)
        return {
            "order": order_id,
            "progress": 72,
            "line": "A01",
            "delay": False,
            "source": "demo"
        }

    # --- 尝试真实调用，带异常捕获 ---
    try:
        conn = MESConnector(url, token)
        result = conn.get_production_status(order_id)

        # 校验返回结构
        if not isinstance(result, dict):
            logger.error("MES returned non-dict result | type=%s", type(result).__name__)
            return {
                "order": order_id,
                "progress": 0,
                "line": "unknown",
                "delay": True,
                "source": "mes_error",
                "error": "invalid_response_type"
            }

        # 确保关键字段存在（容错填充）
        result.setdefault("order", order_id)
        result.setdefault("progress", 0)
        result.setdefault("line", "unknown")
        result.setdefault("delay", False)
        result.setdefault("source", "mes")

        logger.info("MES production query succeeded | order_id=%s | progress=%s", order_id, result.get("progress"))
        return result

    except Exception as e:
        logger.exception("MES production query failed | order_id=%s | error=%s", order_id, str(e))
        # 失败时返回降级数据，但保留错误信息
        return {
            "order": order_id,
            "progress": 0,
            "line": "unknown",
            "delay": True,
            "source": "mes_error",
            "error": str(e)
        }


def _dashboard_snapshot(params, company_id):
    import logging
    from database import SessionLocal
    from models import Quotation, Lead

    logger = logging.getLogger(__name__)
    db = None
    try:
        # 参数校验
        if not company_id:
            logger.warning("company_id is empty or None, returning empty snapshot")
            return {
                "total_amount": 0.0,
                "quotations": 0,
                "leads": 0,
                "grade_a_leads": 0,
                "error": "invalid_company_id"
            }

        db = SessionLocal()
        # 使用 try/except 包裹查询，避免单个查询失败导致整个函数崩溃
        try:
            quotes = db.query(Quotation).filter(Quotation.company_id == company_id).all()
        except Exception as e:
            logger.error(f"Failed to query quotations for company {company_id}: {e}")
            quotes = []

        try:
            leads = db.query(Lead).filter(Lead.company_id == company_id).all()
        except Exception as e:
            logger.error(f"Failed to query leads for company {company_id}: {e}")
            leads = []

        # 数据校验：过滤 None 值，避免 sum 崩溃
        valid_quotes = [q for q in quotes if q is not None and q.total_amount is not None and q.status in ("sent", "accepted")]
        total_amount = round(sum(q.total_amount for q in valid_quotes), 2)

        # 统计时也做 None 防护
        quotation_count = len([q for q in quotes if q is not None])
        lead_count = len([l for l in leads if l is not None])
        grade_a_count = sum(1 for l in leads if l is not None and l.grade == "A")

        logger.info(f"Dashboard snapshot for company {company_id}: quotes={quotation_count}, leads={lead_count}, amount={total_amount}")

        return {
            "total_amount": total_amount,
            "quotations": quotation_count,
            "leads": lead_count,
            "grade_a_leads": grade_a_count,
        }
    except Exception as e:
        logger.exception(f"Unexpected error in dashboard_snapshot for company {company_id}: {e}")
        # 返回安全的默认值，避免上层崩溃
        return {
            "total_amount": 0.0,
            "quotations": 0,
            "leads": 0,
            "grade_a_leads": 0,
            "error": str(e)
        }
    finally:
        if db is not None:
            db.close()


# ============ 工具注册表 ============

BUILTIN_TOOLS: Dict[str, FunctionTool] = {
    "crm.get_customer": FunctionTool(
        "crm.get_customer", "读取企业客户信息（支持按 id / 名称 / 国家过滤）",
        _crm_get_customer, permissions=["crm:read"]),
    "crm.create_customer": FunctionTool(
        "crm.create_customer", "创建客户线索",
        _crm_create_customer, permissions=["crm:write"]),
    "email.send": FunctionTool(
        "email.send", "发送销售邮件（可自动生成开发信）",
        _email_send, permissions=["email:send"]),
    "quotation.create": FunctionTool(
        "quotation.create", "创建报价单（含价格与交期）",
        _quotation_create, permissions=["quotation:write"]),
    "knowledge.search": FunctionTool(
        "knowledge.search", "检索企业知识库（RAG）",
        _knowledge_search, permissions=["knowledge:read"]),
    "erp.get_stock": FunctionTool(
        "erp.get_stock", "查询产品库存（ERP）",
        _erp_get_stock, permissions=["erp:read"]),
    "mes.get_production": FunctionTool(
        "mes.get_production", "查询订单生产状态（MES）",
        _mes_get_production, permissions=["mes:read"]),
    "dashboard.snapshot": FunctionTool(
        "dashboard.snapshot", "获取企业经营数据快照",
        _dashboard_snapshot, permissions=["dashboard:read"]),
}
