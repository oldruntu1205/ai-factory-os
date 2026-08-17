"""
main.py —— 制造业 AI 员工平台主程序（第五/十一/十三章）

覆盖阶段：第二阶段 SaaS 基础 + 第三阶段 RAG + 第四阶段 Multi-Agent
           + 第五阶段商业化 + V2.0 工业 AI + V2.1 自主销售
           + Hermes Agent Runtime（落地手册第二篇，AI 员工操作系统内核）
"""
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from datetime import datetime, timedelta
from config import BASE_DIR
from database import engine, SessionLocal, _lightweight_migrate
from models import (Base, AIAgent, Company, User, AgentPackage, SiteContent,
                    ProductionOrder, QualityReport)
from auth.security import hash_password
import config


def _run_alembic_upgrade():
    """应用 Alembic 迁移（幂等：已到 head 则跳过）。
    首次部署时 create_all 已建全量表，alembic 在此之上打基线。"""
    try:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config(os.path.join(BASE_DIR, "alembic.ini"))
        # 用项目的 DATABASE_URL 覆盖 alembic.ini（与 env.py 一致）
        alembic_cfg.set_main_option("sqlalchemy.url", config.DATABASE_URL)
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic 迁移已同步到 head")
    except Exception as e:
        logger.warning("Alembic 迁移跳过: %s", e)


def _acquire_init_lock():
    """PostgreSQL advisory lock：保证多 worker 时仅首个进程执行 DB 初始化，
    避免并发建表 / alembic / 种子写入相互冲突。非 Postgres 环境返回 None。"""
    try:
        if engine.dialect.name == "postgresql":
            raw = engine.raw_connection()
            conn = getattr(raw, "connection", raw)
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_lock(912073145)")  # 固定锁 ID
            return raw
    except Exception as e:
        logger.warning(f"获取初始化锁失败，将无锁执行: {e}")
    return None


def _release_init_lock(raw):
    if raw is None:
        return
    try:
        conn = getattr(raw, "connection", raw)
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_unlock(912073145)")
    except Exception as e:
        logger.warning(f"释放初始化锁失败: {e}")
    finally:
        try:
            raw.close()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库 + 演示种子数据 + Hermes Runtime + Agent 商城 + AI 员工 Soul"""
    _init_lock = _acquire_init_lock()
    try:
        _lightweight_migrate()
        Base.metadata.create_all(bind=engine)
        _run_alembic_upgrade()
        # 演示种子默认开启（SEED_DEMO=0 关闭，生产环境不注入演示数据）
        # 商城包是平台内容（非演示数据），始终幂等写入，保证新行业包可达
        _seed_marketplace()
        _seed_cms()          # 官网默认内容（CMS）
        _init_hermes()
        _bootstrap_default_souls()
        # 重建 BM25 内存索引（从 bm25_chunks 持久化表），保证稀疏检索跨重启可用
        try:
            from knowledge.hybrid import rebuild_bm25_index
            rebuild_bm25_index()
        except Exception as e:
            logger.warning("BM25 启动重建失败: %s", e)
    finally:
        _release_init_lock(_init_lock)
    # 启动周期自进化调度器（后台守护线程；仅产出 pending 候选，需人工 approve）
    try:
        from hermes.evolution.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        logger.warning("自进化调度器启动失败（不影响主服务）: %s", e)
    # 从库加载平台 LLM 网关配置到运行时（超管后台可视化设置，热切换）
    try:
        from api.platform import load_config_from_db
        load_config_from_db()
    except Exception as e:
        logger.warning(f"平台 LLM 配置预加载跳过: {e}")
    logger.info("数据库、Hermes Runtime%s 初始化完成",
                "、演示种子数据" if config.SEED_DEMO else "")
    yield
    # 优雅停机：停止周期自进化调度器
    try:
        from hermes.evolution.scheduler import stop_scheduler
        stop_scheduler()
    except Exception as e:
        logger.warning("自进化调度器停止异常: %s", e)


def _bootstrap_default_souls():
    """首次启动把内存 DEFAULT_SOULS 写入 hermes/registry/souls/default/"""
    from hermes.registry.soul import bootstrap_default_souls
    bootstrap_default_souls()


def _seed_marketplace():
    """写入 Agent 商城种子包（第24章 行业 Agent 商城），按 name 幂等增量写入"""
    db = SessionLocal()
    try:
        existing = {p[0] for p in db.query(AgentPackage.name).all()}
        packages = [
            AgentPackage(
                name="industry_sales_pack", title="AI销售包",
                industry="C30", version="1.0", price=1299,
                icon="📈",
                description="针对制造业外贸企业的完整 AI 销售团队：自动识别客户需求、创建线索、生成开发信、跟进成交。",
                agents=[
                    {"name": "Lisa", "role": "sales", "label": "AI销售",
                     "description": "客户需求分析、线索创建、开发信生成"},
                    {"name": "Amy", "role": "customer", "label": "AI客服",
                     "description": "结合企业知识库解答客户问题"},
                ],
                tools=["crm.get_customer", "crm.create_customer", "email.send",
                       "knowledge.search", "dashboard.snapshot"],
            ),
            AgentPackage(
                name="industry_quote_pack", title="AI报价包",
                industry="C30", version="1.0", price=1499,
                icon="📄",
                description="自动解析询盘需求，匹配产品价格规则，生成专业 PDF 报价单。",
                agents=[
                    {"name": "Quinn", "role": "quotation", "label": "AI报价",
                     "description": "需求解析、价格匹配、PDF 报价单生成"},
                ],
                tools=["quotation.create", "crm.get_customer", "knowledge.search",
                       "erp.get_stock"],
            ),
            AgentPackage(
                name="industry_quality_pack", title="AI质量包",
                industry="C30", version="1.0", price=1999,
                icon="🔬",
                description="读取检测报告与不良率数据，识别质量异常（裂纹/气泡），给出工艺整改建议。",
                agents=[
                    {"name": "QC", "role": "quality", "label": "AI质量工程师",
                     "description": "不良率分析、根因定位、整改建议"},
                    {"name": "Prod", "role": "production", "label": "AI生产计划",
                     "description": "MES 生产数据分析、延期预警"},
                ],
                tools=["mes.get_production", "dashboard.snapshot"],
            ),
            AgentPackage(
                name="industry_drawing_pack", title="工艺/图纸助手包",
                industry="general", version="1.0", price=899,
                icon="📐",
                description="解析 CAD/PDF 图纸，查询型号公差/材质标准，提示防误操作要点。",
                agents=[
                    {"name": "花花", "role": "process", "label": "工艺/图纸助手",
                     "description": "解析 CAD/PDF 图纸，回答型号公差、材质标准与防误操作要点"},
                ],
                tools=["knowledge.search"],
            ),
        ]
        # 幂等：仅插入 name 不存在的包，避免重复 / 唯一键冲突
        to_add = [p for p in packages if p.name not in existing]
        if to_add:
            db.add_all(to_add)
            db.commit()
            logger.info("Agent 商城种子数据新增 %d 个行业包", len(to_add))
        else:
            logger.info("Agent 商城种子数据已存在，跳过")
    finally:
        db.close()


def _seed_cms():
    """写入官网（AI Factory 介绍页）默认内容，仅首次（无记录）时写入"""
    db = SessionLocal()
    try:
        if db.query(SiteContent).filter(SiteContent.id == 1).first():
            return
        default = SiteContent(
            id=1,
            hero={
                "title": "AI Factory · 制造业 AI 员工平台",
                "subtitle": "让每一家工厂都拥有自己的 AI 员工团队",
                "description": (
                    "AI Factory 是面向制造业的 AI 员工 SaaS 平台，基于 Hermes Agent Runtime"
                    "（AI 员工操作系统内核），一键为企业部署销售、客服、报价、生产、质量、"
                    "经营等数字化 AI 员工，让制造型企业以极低成本拥有 7×24 小时在线的智能团队。"
                ),
                "cta_text": "免费体验",
                "cta_url": "/login",
                "slides": [],
            },
            about={
                "title": "关于 AI Factory",
                "description": (
                    "我们专注于用 AI 重塑制造业的人力结构。通过 Hermes Agent Runtime，"
                    "我们将企业沉淀的知识、流程与工具封装成一个个可独立工作的 AI 员工，"
                    "它们接入 CRM、ERP、MES、QMS 与知识库，自动完成销售开发、客户答疑、"
                    "智能报价、生产排程、质量分析与经营决策。\n\n"
                    "从玻璃仪器到精密制造，AI Factory 已服务众多制造业客户，"
                    "帮助企业在不增加人头的前提下，把获客效率与交付质量提升一个数量级。"
                ),
                "stats": [
                    {"label": "服务制造企业", "value": "500+"},
                    {"label": "AI 员工种类", "value": "6+"},
                    {"label": "客户覆盖国家", "value": "30+"},
                    {"label": "累计 AI 对话", "value": "100 万+"},
                ],
            },
            features=[
                {"icon": "🤖", "title": "AI 销售员工",
                 "description": "自动识别客户需求、创建线索、生成开发信、跟进成交，7×24 小时自主获客。"},
                {"icon": "💬", "title": "AI 客服员工",
                 "description": "接入企业知识库，用母语即时解答客户咨询，永不掉线、秒级响应。"},
                {"icon": "📄", "title": "AI 报价员工",
                 "description": "解析询盘需求，匹配价格规则，一键生成专业 PDF 报价单。"},
                {"icon": "🏭", "title": "AI 生产计划",
                 "description": "分析 MES 生产数据，识别延期与瓶颈，主动预警、智能排程。"},
                {"icon": "🔬", "title": "AI 质量工程师",
                 "description": "读取检测报告与不良率，定位根因并给出可落地的整改建议。"},
                {"icon": "🧠", "title": "CEO 企业大脑",
                 "description": "自动汇总经营日报，输出销售/生产/质量/库存的 AI 决策建议。"},
            ],
            customers=[
                {"name": "太阳玻璃仪器", "logo": ""},
                {"name": "精密制造集团", "logo": ""},
                {"name": "恒达机械", "logo": ""},
                {"name": "蓝海电子", "logo": ""},
                {"name": "鼎峰模具", "logo": ""},
                {"name": "瑞丰新材料", "logo": ""},
            ],
            contact={
                "company_name": "AI Factory 科技（演示）有限公司",
                "address": "上海市浦东新区张江高科技园区博云路 2 号 AI 大厦 8 层",
                "phone": "400-888-0000",
                "email": "contact@aifactory.com",
                "map_url": "",
            },
            social={
                "hotline": "400-888-0000",
                "qq": "800123456",
                "wechat_qr": "",
            },
            pricing={
                "title": "套餐价格",
                "subtitle": "按需选择，灵活扩展，让每一分投入都产生 AI 价值。所有套餐均含 7×24 在线 AI 员工与平台技术保障。",
                "plans": [
                    {
                        "name": "入门版",
                        "tag": "",
                        "price": "299",
                        "period": "/月",
                        "description": "适合刚起步、希望用 AI 替代重复性人工的小微制造企业。",
                        "features": [
                            "1 个 AI 员工（客服 / 销售任选）",
                            "企业知识库（5GB）",
                            "基础数据看板",
                            "邮件 / 网页端接入",
                            "工单式技术支持",
                        ],
                        "cta_text": "免费试用",
                        "cta_url": "/login",
                        "featured": False,
                    },
                    {
                        "name": "专业版",
                        "tag": "推荐",
                        "price": "1299",
                        "period": "/月",
                        "description": "适合成长型制造企业，多岗位 AI 协同提效。",
                        "features": [
                            "5 个 AI 员工（全岗位可选）",
                            "企业知识库（50GB）",
                            "智能报价 / 生产排程 / 质量分析",
                            "CRM / ERP / MES 对接",
                            "专属客户成功经理",
                            "7×24 优先技术支持",
                        ],
                        "cta_text": "立即升级",
                        "cta_url": "/login",
                        "featured": True,
                    },
                    {
                        "name": "企业版",
                        "tag": "定制",
                        "price": "价格面议",
                        "period": "",
                        "description": "适合多工厂、多法人集团，私有化与深度定制。",
                        "features": [
                            "不限 AI 员工数量",
                            "私有化 / 专有云部署",
                            "行业大模型微调",
                            "Hermes Runtime 企业级权限",
                            "SLA 99.9% 与专属架构师",
                            "SDK / OpenAPI 深度集成",
                        ],
                        "cta_text": "联系顾问",
                        "cta_url": "/login",
                        "featured": False,
                    },
                ],
            },
            updated_at=datetime.utcnow(),
        )
        db.add(default)
        db.commit()
        logger.info("官网默认内容（CMS）写入完成")
    finally:
        db.close()


def _seed_demo_data():
    """写入演示企业 + AI 员工（便于快速体验，可删除）"""
    db = SessionLocal()
    try:
        # 平台超级管理员（不属于任何企业，独立种子）
        if not db.query(User).filter(User.email == "platform@demo.com").first():
            db.add(User(
                email="platform@demo.com",
                password=hash_password("Platform@123"),
                name="平台管理员",
                role="platform_admin",
                company_id=None,  # 平台超管不属于任何租户企业
            ))
            db.commit()
            logger.info("平台超管种子写入 platform@demo.com / Platform@123")

        if db.query(Company).filter(Company.name == "太阳玻璃仪器").first():
            return
        from datetime import datetime, timedelta
        # 演示企业：30 天前购买 Enterprise 年度套餐（剩余 335 天）
        company = Company(
            name="太阳玻璃仪器", plan="enterprise", max_agents=50,
            plan_started_at=datetime.utcnow() - timedelta(days=30),
            plan_expires_at=datetime.utcnow() + timedelta(days=335),
        )
        db.add(company)
        db.commit()
        db.refresh(company)

        user = User(
            email="admin@demo.com",
            password=hash_password("123456"),
            name="演示管理员",
            company_id=company.id,
        )
        db.add(user)

        agents = [
            AIAgent(name="Lisa", role="sales", role_label="AI销售",
                    description="自动识别客户需求、创建线索、推动成交", company_id=company.id),
            AIAgent(name="Amy", role="customer", role_label="AI客服",
                    description="结合企业知识库解答客户问题", company_id=company.id),
            AIAgent(name="Quinn", role="quotation", role_label="AI报价",
                    description="根据需求生成专业报价单", company_id=company.id),
            AIAgent(name="Prod", role="production", role_label="AI生产计划",
                    description="分析 MES 生产数据，识别延期与瓶颈", company_id=company.id),
            AIAgent(name="QC", role="quality", role_label="AI质量工程师",
                    description="分析不良率与客诉，给出整改建议", company_id=company.id),
            AIAgent(name="Brain", role="ceo", role_label="CEO企业大脑",
                    description="自动生成企业经营日报", company_id=company.id),
        ]
        db.add_all(agents)
        db.commit()
        logger.info("演示数据写入完成（admin@demo.com / 123456）")
    finally:
        db.close()


def _seed_business_data():
    """写入演示企业的生产订单与质量报告（玻璃仪器行业主题），便于数据中心页与驾驶舱展示真实内容"""
    db = SessionLocal()
    try:
        company = db.query(Company).filter(Company.name == "太阳玻璃仪器").first()
        if not company:
            return

        # 生产订单（MES 数据源）
        if db.query(ProductionOrder).filter(ProductionOrder.company_id == company.id).count() == 0:
            pos = [
                ProductionOrder(order_no="WO-2026-001", product="烧杯 250ml", quantity=1000, deadline_days=15, progress=100, line="A01", load_rate=82, delay=False, company_id=company.id),
                ProductionOrder(order_no="WO-2026-002", product="量筒 100ml", quantity=800, deadline_days=20, progress=65, line="A02", load_rate=88, delay=False, company_id=company.id),
                ProductionOrder(order_no="WO-2026-003", product="容量瓶 500ml", quantity=500, deadline_days=10, progress=40, line="A01", load_rate=90, delay=True, company_id=company.id),
                ProductionOrder(order_no="WO-2026-004", product="试管 16x150", quantity=2000, deadline_days=25, progress=20, line="B01", load_rate=70, delay=False, company_id=company.id),
                ProductionOrder(order_no="WO-2026-005", product="滴管 玻璃", quantity=1500, deadline_days=18, progress=0, line="B02", load_rate=60, delay=False, company_id=company.id),
                ProductionOrder(order_no="WO-2026-006", product="培养皿 90mm", quantity=600, deadline_days=12, progress=100, line="A02", load_rate=78, delay=False, company_id=company.id),
            ]
            db.add_all(pos)

        # 质量报告（QMS 数据源）
        if db.query(QualityReport).filter(QualityReport.company_id == company.id).count() == 0:
            base = datetime.utcnow()
            qrs = [
                QualityReport(report_date=base - timedelta(days=6), line="A01", defect_rate=1.2, defect_type="bubble", total_count=1000, defect_count=12, company_id=company.id),
                QualityReport(report_date=base - timedelta(days=5), line="A02", defect_rate=0.8, defect_type="crack", total_count=800, defect_count=6, company_id=company.id),
                QualityReport(report_date=base - timedelta(days=4), line="A01", defect_rate=2.5, defect_type="warp", total_count=500, defect_count=13, company_id=company.id),
                QualityReport(report_date=base - timedelta(days=3), line="B01", defect_rate=0.5, defect_type="none", total_count=2000, defect_count=10, company_id=company.id),
                QualityReport(report_date=base - timedelta(days=2), line="A02", defect_rate=3.2, defect_type="bubble", total_count=600, defect_count=19, company_id=company.id),
                QualityReport(report_date=base - timedelta(days=1), line="B02", defect_rate=0.3, defect_type="none", total_count=1500, defect_count=5, company_id=company.id),
            ]
            db.add_all(qrs)

        db.commit()
        logger.info("演示生产/质量数据写入完成")
    finally:
        db.close()


def _init_hermes():
    """装配 Hermes Runtime 并注册内置 Agent（落地手册第二篇）"""
    from hermes.runtime import init_hermes, get_runtime
    from hermes.registry.models import AgentManifest

    runtime = init_hermes()

    # 注册内置 AI 员工 Agent（Hermes 动态加载）
    builtin_agents = [
        AgentManifest(name="customer_agent", version="1.0", industry="general",
                      label="AI客服", description="RAG 企业知识问答客服",
                      tools=["knowledge.search", "crm.get_customer"]),
        AgentManifest(name="sales_agent", version="1.0", industry="general",
                      label="AI销售", description="客户分析、线索创建、开发信",
                      tools=["crm.get_customer", "crm.create_customer",
                             "email.send", "dashboard.snapshot"]),
        AgentManifest(name="quotation_agent", version="1.0", industry="general",
                      label="AI报价", description="生成报价单",
                      tools=["quotation.create", "crm.get_customer",
                             "knowledge.search", "erp.get_stock"]),
        AgentManifest(name="production_agent", version="1.0", industry="general",
                      label="AI生产计划", description="MES 生产数据分析",
                      tools=["mes.get_production", "erp.get_stock", "dashboard.snapshot"]),
        AgentManifest(name="quality_agent", version="1.0", industry="general",
                      label="AI质量工程师", description="质量数据分析",
                      tools=["mes.get_production", "erp.get_stock", "dashboard.snapshot"]),
        AgentManifest(name="ceo_agent", version="1.0", industry="general",
                      label="CEO企业大脑", description="经营日报与决策支持",
                      tools=["crm.get_customer", "dashboard.snapshot",
                             "quotation.create"]),
        AgentManifest(name="process_agent", version="1.0", industry="general",
                      label="工艺/图纸助手", description="CAD/PDF 图纸参数抽取 + 混合检索，查询公差/材质标准，防范误操作",
                      tools=["drawing.search", "knowledge.search",
                             "mes.get_process", "erp.get_material_std"]),
    ]
    registry = runtime.agent_registry
    for manifest in builtin_agents:
        # 挂接角色实现
        from agents.service import execute_specialized
        from models import AIAgent
        from database import SessionLocal

        role_map = {"customer_agent": "customer", "sales_agent": "sales",
                    "quotation_agent": "quotation", "production_agent": "production",
                    "quality_agent": "quality", "ceo_agent": "ceo",
                    "process_agent": "process"}

        def make_handler(role_name):
            def handler(message: str, runtime_ctx: dict):
                db = SessionLocal()
                try:
                    agent = db.query(AIAgent).filter(
                        AIAgent.role == role_name,
                        AIAgent.company_id == runtime_ctx["company_id"]).first()
                    if not agent:
                        return "企业尚未创建对应 AI 员工，请先在 AI员工 页面创建"
                    return execute_specialized(
                        agent, message, runtime_ctx["company_id"], db,
                        runtime_ctx.get("customer_id"),
                        tools=runtime_ctx.get("tools"))
                finally:
                    db.close()
            return handler

        manifest.handler = make_handler(role_map[manifest.name])
        registry.register(manifest)

    # 权限系统初始化（默认开放企业工具权限）
    runtime.permission_system.init_tenant(1)

    logger.info("Hermes Runtime: 内置 6 个 AI 员工 Agent 注册完成")


# 主应用
app = FastAPI(
    title="制造业 AI 员工平台 V2.1",
    description="企业 SaaS + RAG 知识库 + Multi-Agent 智能员工 + 商业化 MVP + 工业 AI + 自主销售",
    version="2.1.0",
    lifespan=lifespan,
)

# CORS（允许前端跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 全局请求日志 + 统一异常处理 middleware =====
import time as _time
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.middleware("http")
async def request_logging_middleware(request, call_next):
    """记录每个请求的耗时/状态码（结构化 JSON），统一兜底异常响应"""
    start = _time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = round((_time.perf_counter() - start) * 1000, 1)
        if request.url.path.startswith(("/health", "/docs", "/openapi.json")):
            return response
        logger.info("req method=%s path=%s status=%d ms=%.1f",
                    request.method, request.url.path, response.status_code, duration_ms)
        return response
    except Exception as e:
        logger.exception("unhandled_exc method=%s path=%s err=%s",
                         request.method, request.url.path, e)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务器内部错误，请稍后重试"},
        )


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    """参数校验失败统一返回 422 + 可读信息"""
    errors = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        msg = err.get("msg", "")
        errors.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    """HTTP 异常统一 JSON（含 404/403/401 等）"""
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail})

# 路由挂载
from auth.router import router as auth_router
from api.agent import router as agent_router
from api.agent_chat import router as agent_chat_router
from api.knowledge import router as knowledge_router
from api.crm import router as crm_router
from api.quotation import router as quotation_router
from api.channels import router as channels_router
from api.leads import router as leads_router
from api.dashboard import router as dashboard_router
from api.system import router as system_router
from api.hermes import router as hermes_router
from api.evolution import router as evolution_router
from api.marketplace import router as marketplace_router
from api.company import router as company_router
from api.members import router as members_router
from api.billing import router as billing_router
from api.platform import router as platform_router
from api.account import router as account_router
from api.cms import router as cms_router
from api.integrations import router as integrations_router
from channels.whatsapp.webhook import router as whatsapp_router
from channels.wecom.webhook import router as wecom_router

app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(agent_chat_router)
app.include_router(knowledge_router)
app.include_router(crm_router)
app.include_router(quotation_router)
app.include_router(channels_router)
app.include_router(leads_router)
app.include_router(dashboard_router)
app.include_router(system_router)
app.include_router(hermes_router)
app.include_router(evolution_router)
app.include_router(marketplace_router)
app.include_router(company_router)
app.include_router(members_router)
app.include_router(billing_router)
app.include_router(platform_router)
app.include_router(account_router)
app.include_router(cms_router)
app.include_router(integrations_router)
from api.systems_data import router as systems_data_router
app.include_router(systems_data_router)
app.include_router(whatsapp_router)
app.include_router(wecom_router)


@app.get("/")
def root():
    return {
        "app": "制造业 AI 员工平台 V2.1",
        "version": "2.1.0",
        "docs": "/docs",
        "architecture": "Hermes Agent Runtime（AI 员工操作系统内核）",
        "modules": [
            "auth: 注册/登录/JWT",
            "agent: AI员工管理",
            "agent/chat: Multi-Agent 对话",
            "knowledge: RAG 企业知识库",
            "crm: 客户管理",
            "quotation: AI 报价单",
            "channels: WhatsApp/Email 渠道",
            "leads: AI 自主销售线索",
            "dashboard: 经营驾驶舱",
            "hermes: Agent Runtime（注册中心/Tool/记忆/权限/队列）",
            "marketplace: Agent 行业商城（安装/卸载行业包）",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# 上传文件静态服务（供前端下载报价 PDF 等）
os.makedirs("output", exist_ok=True)
app.mount("/output", StaticFiles(directory="output"), name="output")

# 官网图片静态服务（客户 LOGO / 首屏背景等，CMS 上传）
os.makedirs("uploads/site", exist_ok=True)
app.mount("/static", StaticFiles(directory="uploads"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
