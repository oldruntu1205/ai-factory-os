"""
database.py —— SQLAlchemy 引擎与会话管理（第二阶段）
"""
import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from config import DATABASE_URL

logger = logging.getLogger(__name__)

_connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    """FastAPI 依赖：每个请求一个数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 演示版轻量迁移：自动给已有 SQLite 表增加新列（生产环境推荐 Alembic）
_COMPANY_MIGRATIONS = [
    ("address", "VARCHAR(300) DEFAULT ''"),
    ("phone", "VARCHAR(50) DEFAULT ''"),
    ("contact_name", "VARCHAR(100) DEFAULT ''"),
    ("contact_email", "VARCHAR(100) DEFAULT ''"),
    ("business_license_path", "VARCHAR(500) DEFAULT ''"),
    ("license_verified", "BOOLEAN DEFAULT 0"),
    ("license_status", "VARCHAR(20) DEFAULT 'none'"),
    ("license_reject_reason", "VARCHAR(300) DEFAULT ''"),
    ("license_uploaded_at", "DATETIME"),
    ("license_verified_at", "DATETIME"),
    ("whatsapp_phone", "VARCHAR(50) DEFAULT ''"),
    ("wecom_user_map", "VARCHAR(500) DEFAULT ''"),
    ("industry", "VARCHAR(50) DEFAULT ''"),
    ("plan_started_at", "DATETIME"),
    ("plan_expires_at", "DATETIME"),
]

_USER_MIGRATIONS = [
    ("status", "VARCHAR(20) DEFAULT 'active'"),
    ("mobile", "VARCHAR(50) DEFAULT ''"),
]

_COMPANY_MIGRATIONS_V2 = [
    ("mobile", "VARCHAR(50) DEFAULT ''"),
    ("mobile_verified", "BOOLEAN DEFAULT 0"),
]

_LEAD_MIGRATIONS = [
    ("follow_up_step", "INTEGER DEFAULT 0"),
    ("follow_up_at", "DATETIME"),
    ("enriched", "JSON DEFAULT '{}'"),
    ("score_reasons", "TEXT DEFAULT ''"),
    ("track_id", "VARCHAR(40) DEFAULT ''"),
    ("variant", "VARCHAR(10) DEFAULT 'A'"),
    ("opened", "BOOLEAN DEFAULT 0"),
    ("open_count", "INTEGER DEFAULT 0"),
    ("converted", "BOOLEAN DEFAULT 0"),
    ("last_error", "TEXT DEFAULT ''"),
]

_SITE_CONTENT_MIGRATIONS = [
    ("pricing", "JSON DEFAULT '{}'"),
    ("social", "JSON DEFAULT '{}'"),
    ("translations", "JSON DEFAULT '{}'"),
]

_CUSTOMER_MIGRATIONS = [
    ("whatsapp", "VARCHAR(50) DEFAULT ''"),
]


def _pg_type(sqltype: str) -> str:
    """把 SQLite 风格的类型串转成 PostgreSQL 兼容（用于 ADD COLUMN IF NOT EXISTS）。"""
    return (sqltype
            .replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
            .replace("DATETIME", "TIMESTAMP")
            .replace("FLOAT", "DOUBLE PRECISION"))


def _lightweight_migrate():
    """对已有数据库自动补加新列。

    - SQLite 演示库：逐表用 inspector 比对后 ALTER（SQLite 不支持 IF NOT EXISTS）。
    - PostgreSQL 生产库：用幂等的 ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
      启动自愈（本项目无 Alembic 列迁移，避免新列缺失导致查询崩溃）。
    """
    is_sqlite = DATABASE_URL.startswith("sqlite")
    is_postgres = DATABASE_URL.startswith("postgresql")
    if not (is_sqlite or is_postgres):
        return
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        with engine.begin() as conn:
            # ---------- SQLite：逐表 inspector 比对后 ALTER ----------
            if is_sqlite:
                # companies 表
                if "companies" in tables:
                    existing = {c["name"] for c in inspector.get_columns("companies")}
                    for col, sqltype in _COMPANY_MIGRATIONS + _COMPANY_MIGRATIONS_V2:
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE companies ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: companies.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 companies.{col}: {e}")
                # users 表
                if "users" in tables:
                    existing = {c["name"] for c in inspector.get_columns("users")}
                    for col, sqltype in _USER_MIGRATIONS:
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE users ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: users.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 users.{col}: {e}")
                # leads 表
                if "leads" in tables:
                    existing = {c["name"] for c in inspector.get_columns("leads")}
                    for col, sqltype in _LEAD_MIGRATIONS:
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE leads ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: leads.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 leads.{col}: {e}")
                # customers 表
                if "customers" in tables:
                    existing = {c["name"] for c in inspector.get_columns("customers")}
                    for col, sqltype in _CUSTOMER_MIGRATIONS:
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE customers ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: customers.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 customers.{col}: {e}")
                # site_contents 表（官网 CMS）
                if "site_contents" in tables:
                    existing = {c["name"] for c in inspector.get_columns("site_contents")}
                    for col, sqltype in _SITE_CONTENT_MIGRATIONS:
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE site_contents ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: site_contents.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 site_contents.{col}: {e}")
                # 自进化相关表新增列（方案2/3：soul_version 指纹）
                for tbl, col, sqltype in [
                    ("agent_skills", "soul_version", "VARCHAR(40) DEFAULT ''"),
                    ("evolution_candidates", "soul_version", "VARCHAR(40) DEFAULT ''"),
                ]:
                    if tbl in tables:
                        existing = {c["name"] for c in inspector.get_columns(tbl)}
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE {tbl} ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: {tbl}.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 {tbl}.{col}: {e}")
                # 方案4：agent_skills 隔离列（soul 大改自动 quarantine 旧版本技能）
                for tbl, col, sqltype in [
                    ("agent_skills", "quarantined", "BOOLEAN DEFAULT 0"),
                    ("agent_skills", "quarantined_at", "FLOAT"),
                    ("agent_skills", "quarantine_reason", "TEXT DEFAULT ''"),
                ]:
                    if tbl in tables:
                        existing = {c["name"] for c in inspector.get_columns(tbl)}
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE {tbl} ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: {tbl}.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 {tbl}.{col}: {e}")
                # 渠道消息幂等：conversations.msg_id（WhatsApp/企微去重）
                if "conversations" in tables:
                    existing = {c["name"] for c in inspector.get_columns("conversations")}
                    if "msg_id" not in existing:
                        try:
                            conn.execute(text(
                                "ALTER TABLE conversations ADD COLUMN msg_id VARCHAR(100)"))
                            conn.execute(text(
                                "CREATE INDEX IF NOT EXISTS idx_conversations_msg_id ON conversations(msg_id)"))
                            logger.info("迁移: conversations.msg_id 新增")
                        except Exception as e:
                            logger.debug(f"迁移跳过 conversations.msg_id: {e}")
                # 外贸报价字段：quotations 新增列
                _QUOTE_MIGRATIONS = [
                    ("hs_code", "VARCHAR(20) DEFAULT ''"),
                    ("incoterms", "VARCHAR(10) DEFAULT 'FOB'"),
                    ("fx_rate", "FLOAT DEFAULT 1.0"),
                    ("freight_amount", "FLOAT DEFAULT 0.0"),
                    ("tariff_amount", "FLOAT DEFAULT 0.0"),
                    ("tariff_pct", "FLOAT DEFAULT 0.0"),
                    ("moq", "INTEGER DEFAULT 1"),
                    ("packaging", "VARCHAR(200) DEFAULT ''"),
                ]
                if "quotations" in tables:
                    existing = {c["name"] for c in inspector.get_columns("quotations")}
                    for col, sqltype in _QUOTE_MIGRATIONS:
                        if col not in existing:
                            try:
                                conn.execute(text(
                                    f"ALTER TABLE quotations ADD COLUMN {col} {sqltype}"))
                                logger.info(f"迁移: quotations.{col} 新增")
                            except Exception as e:
                                logger.debug(f"迁移跳过 quotations.{col}: {e}")
            # ---------- PostgreSQL：幂等 ADD COLUMN IF NOT EXISTS ----------
            if is_postgres:
                _PG_ALL_COLS = []
                for col, sqltype in _COMPANY_MIGRATIONS + _COMPANY_MIGRATIONS_V2:
                    _PG_ALL_COLS.append(("companies", col, _pg_type(sqltype)))
                for col, sqltype in _USER_MIGRATIONS:
                    _PG_ALL_COLS.append(("users", col, _pg_type(sqltype)))
                for col, sqltype in _LEAD_MIGRATIONS:
                    _PG_ALL_COLS.append(("leads", col, _pg_type(sqltype)))
                for col, sqltype in _CUSTOMER_MIGRATIONS:
                    _PG_ALL_COLS.append(("customers", col, _pg_type(sqltype)))
                for col, sqltype in _SITE_CONTENT_MIGRATIONS:
                    _PG_ALL_COLS.append(("site_contents", col, _pg_type(sqltype)))
                # 自进化相关表（方案2/3/4）
                _PG_ALL_COLS += [
                    ("agent_skills", "soul_version", "VARCHAR(40) DEFAULT ''"),
                    ("evolution_candidates", "soul_version", "VARCHAR(40) DEFAULT ''"),
                    ("agent_skills", "quarantined", "BOOLEAN DEFAULT FALSE"),
                    ("agent_skills", "quarantined_at", "DOUBLE PRECISION"),
                    ("agent_skills", "quarantine_reason", "TEXT DEFAULT ''"),
                    # 渠道消息幂等：conversations.msg_id
                    ("conversations", "msg_id", "VARCHAR(100)"),
                    # 外贸报价字段
                    ("quotations", "hs_code", "VARCHAR(20) DEFAULT ''"),
                    ("quotations", "incoterms", "VARCHAR(10) DEFAULT 'FOB'"),
                    ("quotations", "fx_rate", "DOUBLE PRECISION DEFAULT 1.0"),
                    ("quotations", "freight_amount", "DOUBLE PRECISION DEFAULT 0.0"),
                    ("quotations", "tariff_amount", "DOUBLE PRECISION DEFAULT 0.0"),
                    ("quotations", "tariff_pct", "DOUBLE PRECISION DEFAULT 0.0"),
                    ("quotations", "moq", "INTEGER DEFAULT 1"),
                    ("quotations", "packaging", "VARCHAR(200) DEFAULT ''"),
                ]
                for tbl, col, sqltype in _PG_ALL_COLS:
                    if tbl in tables:
                        try:
                            conn.execute(text(
                                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {sqltype}"))
                            logger.info(f"PG 迁移: {tbl}.{col} 新增")
                        except Exception as e:
                            logger.debug(f"PG 迁移跳过 {tbl}.{col}: {e}")
            # 高频查询索引（幂等，两种库通用）
            _INDEX_MIGRATIONS = [
                ("CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id)"),
                ("CREATE INDEX IF NOT EXISTS idx_conversations_company ON conversations(company_id)"),
                ("CREATE INDEX IF NOT EXISTS idx_customers_company ON customers(company_id)"),
                ("CREATE INDEX IF NOT EXISTS idx_quotations_company ON quotations(company_id)"),
                ("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)"),
                ("CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(company_id)"),
            ]
            for stmt in _INDEX_MIGRATIONS:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.debug(f"索引迁移跳过: {e}")
    except Exception as e:
        logger.warning(f"轻量迁移失败: {e}")
