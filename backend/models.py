"""
models.py —— 全量数据模型（覆盖第二~V2.1 阶段全部业务表）

表清单：
  companies   企业（多租户根）
  users       用户（企业管理员）
  ai_agents   AI 员工（客服/销售/报价/生产/质量...）
  documents   上传的企业知识文档
  conversations 对话记录
  api_keys    企业 API 密钥
  customers   CRM 客户（线索）
  quotations  报价单
  leads       V2.1 AI 自主销售线索
"""
from sqlalchemy import (
    Column, Integer, String, ForeignKey, DateTime, Text, Float,
    Boolean, JSON, BigInteger, UniqueConstraint, CheckConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class Company(Base):
    """企业（多租户隔离边界）"""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    plan = Column(String(50), default="starter")          # starter/professional/enterprise
    max_agents = Column(Integer, default=1)
    max_conversations = Column(Integer, default=1000)

    # 套餐时间范围（升级时设置开始/结束时间）
    plan_started_at = Column(DateTime)                    # 套餐生效开始时间
    plan_expires_at = Column(DateTime)                    # 套餐到期时间（NULL = 永久/starter）

    # 企业基本信息（企业设置模块）
    address = Column(String(300), default="")             # 企业地址
    phone = Column(String(50), default="")                # 联系手机（保留兼容；推荐用 mobile）
    mobile = Column(String(50), default="")               # 联系手机
    mobile_verified = Column(Boolean, default=False)      # 手机号是否验证
    contact_name = Column(String(100), default="")         # 联系人
    contact_email = Column(String(100), default="")        # 联系邮箱
    industry = Column(String(50), default="")              # 所属行业（GB/T 4754-2017 门类代码 A-T）

    # 企业认证（营业执照）
    business_license_path = Column(String(500), default="")   # 营业执照照片路径
    license_verified = Column(Boolean, default=False)         # 是否通过平台审核

    # 开票信息（客户后台「套餐与账单 → 开票信息」维护，开票类别：服务费）
    invoice_title = Column(String(200), default="")           # 发票抬头（企业全称）
    invoice_tax_no = Column(String(50), default="")           # 纳税人识别号
    invoice_bank = Column(String(200), default="")            # 开户银行
    invoice_bank_account = Column(String(50), default="")     # 银行账号
    invoice_address = Column(String(300), default="")         # 注册地址
    invoice_phone = Column(String(50), default="")            # 注册电话
    invoice_type = Column(String(20), default="normal")       # normal=电子普通发票 special=电子专票

    # 外部渠道绑定（WhatsApp / 企业微信）
    whatsapp_phone = Column(String(50), default="")           # WhatsApp 业务号（客户来消息路由到本企业）
    wecom_user_map = Column(String(500), default="")          # 企微用户映射 JSON {"userid":"company_id"}
    license_status = Column(String(20), default="none")       # none/pending/approved/rejected
    license_reject_reason = Column(String(300), default="")   # 审核拒绝原因
    license_uploaded_at = Column(DateTime, nullable=True)
    license_verified_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="company")
    agents = relationship("AIAgent", back_populates="company", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="company", cascade="all, delete-orphan")


class User(Base):
    """用户（企业管理员）"""
    __tablename__ = "users"
    # 安全修复：角色 DB 层约束（防止绕过 API 白名单写入非法角色，如 platform_admin 越权）
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'staff', 'platform_admin')",
                        name="ck_users_role"),
    )

    id = Column(Integer, primary_key=True)
    email = Column(String(100), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)
    name = Column(String(100), default="管理员")
    role = Column(String(50), default="admin")            # admin / staff / platform_admin
    status = Column(String(20), default="active")         # active / disabled
    mobile = Column(String(50), default="")               # 个人手机（账户设置）
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="users")


class HermesRbacSetting(Base):
    """Hermes RBAC 租户设置（内存 RBAC 的持久化副本，重启不丢）

    仅记录企业显式配置过的权限点/Agent 白名单；无记录的企业回退默认策略。
    """
    __tablename__ = "hermes_rbac_settings"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, unique=True, index=True, nullable=False)
    allowed_agents = Column(JSON, default=list)   # Agent 白名单（空 = 默认放行语义）
    permissions = Column(JSON, default=list)       # 权限点集合（空 = 全拒绝，fail-closed）
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AIAgent(Base):
    """AI 员工"""
    __tablename__ = "ai_agents"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)            # Lisa / Amy
    role = Column(String(50), nullable=False)             # customer / sales / quotation / production / quality / purchasing / ceo
    role_label = Column(String(100), default="")
    description = Column(Text, default="")
    avatar = Column(String(300), default="")
    status = Column(String(20), default="active")         # active / disabled
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="agents")


class Document(Base):
    """企业知识文档"""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    filename = Column(String(255), nullable=False)
    path = Column(String(500), nullable=False)
    file_type = Column(String(20), default="")            # pdf/docx/xlsx
    size = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    status = Column(String(20), default="uploaded")       # uploaded/parsed/vectorized/failed
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="documents")


class DrawingParam(Base):
    """图纸/工艺结构化参数（工艺/图纸助手 Agent 的核心数据源）

    从 CAD(DXF)/PDF/Word/Excel 中抽取的精确参数（公差、材质、尺寸、标准），
    支持「按产品型号精确查」快路径，弥补稠密向量对规格数值检索差的短板。
    """
    __tablename__ = "drawing_params"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    document_id = Column(Integer, nullable=True, index=True)   # 关联源文档
    product_model = Column(String(120), default="", index=True)  # 产品型号（如 XK-205）
    category = Column(String(40), default="")    # tolerance / material / dimension / standard / other
    param_name = Column(String(120), default="")  # 参数名（如 外径公差、基体材质）
    value = Column(String(200), default="")       # 参数值（如 ±0.05、6061-T6）
    raw = Column(Text, default="")                # 抽取命中的原始文本片段
    source_file = Column(String(255), default="")  # 来源文件名
    location = Column(String(60), default="")     # 来源位置（页/实体，可选）
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company")


class Bm25Chunk(Base):
    """BM25 稀疏检索索引的持久化副本（工艺/图纸助手混合检索用）。

    内存 BM25 索引（knowledge/hybrid._bm25_store）在进程重启后会清空，
    这里把每个 chunk 落库，启动时 rebuild_bm25_index 重建，保证稀疏检索
    跨重启/跨进程可用，且删除文档时按 document_id 精准回收，避免无限增长。
    """
    __tablename__ = "bm25_chunks"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    document_id = Column(Integer, nullable=True, index=True)   # 关联源文档
    text = Column(Text, default="")                # chunk 文本（BM25 索引源）
    source = Column(String(255), default="")       # 来源文件名
    created_at = Column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    """对话记录（多租户隔离）"""
    __tablename__ = "conversations"
    # 安全/正确性修复：msg_id 用于外部渠道（WhatsApp/企微）消息幂等去重（至少一次投递防重复处理）
    __table_args__ = (
        UniqueConstraint("msg_id", name="uq_conversations_msg_id"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), index=True)
    agent_id = Column(Integer, ForeignKey("ai_agents.id"), nullable=True)
    user_message = Column(Text, default="")
    agent_answer = Column(Text, default="")
    channel = Column(String(20), default="web")           # web/whatsapp/wecom/email
    company_id = Column(Integer, index=True)
    # 外部渠道消息 ID（web 对话为 NULL；非 NULL 唯一，用于幂等去重）
    msg_id = Column(String(100), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ApiKey(Base):
    """企业 API 密钥"""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), default="")
    key = Column(String(64), unique=True, index=True)
    company_id = Column(Integer, index=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    """CRM 客户 / 销售线索（第四阶段）"""
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    name = Column(String(100))
    country = Column(String(50), default="")
    email = Column(String(100), default="")
    phone = Column(String(50), default="")
    whatsapp = Column(String(50), default="")   # 客户 WhatsApp
    requirement = Column(Text, default="")
    source = Column(String(50), default="agent")          # agent/whatsapp/email/manual
    status = Column(String(50), default="new")            # new/following/quoted/won/lost
    score = Column(Integer, default=0)
    company_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Quotation(Base):
    """报价单（第五阶段）"""
    __tablename__ = "quotations"

    id = Column(Integer, primary_key=True)
    quotation_no = Column(String(50), unique=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    customer_name = Column(String(100), default="")
    product = Column(String(200))
    quantity = Column(Integer, default=1)
    unit_price = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)
    currency = Column(String(10), default="USD")
    delivery_days = Column(Integer, default=30)
    payment_terms = Column(String(100), default="T/T 30% deposit")
    # 外贸报价字段（出口型制造业 AI 应用）
    hs_code = Column(String(20), default="")          # HS Code 归类（6 位）
    incoterms = Column(String(10), default="FOB")     # FOB/CIF/EXW/DAP...
    fx_rate = Column(Float, default=1.0)              # 本币→报价币种汇率
    freight_amount = Column(Float, default=0.0)       # 海运费（USD，CIF/DAP 场景计入）
    tariff_amount = Column(Float, default=0.0)        # 关税（USD，DDP 场景计入）
    tariff_pct = Column(Float, default=0.0)           # 适用关税率 %
    moq = Column(Integer, default=1)                  # 最小起订量
    packaging = Column(String(200), default="")       # 包装说明
    status = Column(String(50), default="draft")          # draft/sent/accepted/rejected
    pdf_path = Column(String(500), default="")
    company_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Lead(Base):
    """V2.1 AI 自主销售线索"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    company_name = Column(String(200))
    website = Column(String(300), default="")
    country = Column(String(50), default="")
    email = Column(String(100), default="")
    industry = Column(String(100), default="")
    score = Column(Integer, default=0)                    # 客户价值评分
    grade = Column(String(10), default="C")               # A/B/C
    status = Column(String(50), default="discovered")     # discovered/contacted/replied/qualified/won
    followup_day = Column(Integer, default=1)             # Day 1/3/7/14
    follow_up_step = Column(Integer, default=0)           # 邮件跟进进度 0/1/2/3
    follow_up_at = Column(DateTime)                        # 上次跟进时间
    source = Column(String(50), default="discovery")
    notes = Column(Text, default="")
    # ---- V2.1 自主销售升级字段 ----
    enriched = Column(JSON, default=dict)                 # 发现阶段 enriched: employee_count/has_import/products/social
    score_reasons = Column(Text, default="")              # LLM 评分理由
    track_id = Column(String(40), default="")             # 邮件追踪/退订唯一ID
    variant = Column(String(10), default="A")             # A/B 文案变体
    opened = Column(Boolean, default=False)               # 邮件打开追踪
    open_count = Column(Integer, default=0)               # 打开次数
    converted = Column(Boolean, default=False)            # 是否已转入 CRM
    last_error = Column(Text, default="")                 # 最近一次邮件错误
    company_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProductionOrder(Base):
    """生产订单（V2.0 MES 连接，演示用）"""
    __tablename__ = "production_orders"

    id = Column(Integer, primary_key=True)
    order_no = Column(String(50), unique=True, index=True)
    product = Column(String(200), default="")
    quantity = Column(Integer, default=0)
    deadline_days = Column(Integer, default=30)
    progress = Column(Integer, default=0)                 # 0-100
    line = Column(String(20), default="A01")
    load_rate = Column(Integer, default=0)                # 产线负载 %
    delay = Column(Boolean, default=False)
    company_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QualityReport(Base):
    """质量数据（V2.0 QMS 连接，演示用）"""
    __tablename__ = "quality_reports"

    id = Column(Integer, primary_key=True)
    report_date = Column(DateTime, default=datetime.utcnow)
    line = Column(String(20), default="")
    defect_rate = Column(Float, default=0.0)
    defect_type = Column(String(50), default="")          # crack/bubble/warp...
    total_count = Column(Integer, default=0)
    defect_count = Column(Integer, default=0)
    company_id = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ============ Hermes Agent Runtime（落地手册第二篇）============

class AgentMemory(Base):
    """Hermes 工作记忆（第9章）：Agent 任务上下文，按 agent+company 维度"""
    __tablename__ = "agent_memory"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String(100), index=True)
    context = Column(Text, default="{}")                  # JSON 任务上下文
    company_id = Column(Integer, index=True)
    updated_at = Column(Float, default=0)


class AgentTrace(Base):
    """Hermes 自进化·执行轨迹（Phase 0）：记录一次 Agent 任务的输入/步骤/输出/质量分。
    GEPA 进化引擎的数据来源——没有轨迹就没有"学习信号"。"""
    __tablename__ = "agent_traces"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    agent_name = Column(String(100), index=True)
    session_id = Column(String(100), index=True, nullable=True)
    signature = Column(String(120), index=True, nullable=True)  # 任务类型签名（技能提取分组用）
    task_input = Column(Text, default="")                 # 用户/系统输入
    steps_json = Column(JSON, default=list)               # 工具调用步骤列表
    final_output = Column(Text, default="")               # Agent 最终输出
    quality_score = Column(Float, nullable=True)          # 0-1，来自反馈/自动评估
    tool_count = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(Float, default=0)


class AgentSkill(Base):
    """Hermes 自进化·沉淀技能（Phase 1）：把重复任务的工作流提炼为可复用 SKILL。
    遵循 agentskills.io 渐进式披露：level0 概要 / level1 完整 / level2 参考。"""
    __tablename__ = "agent_skills"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    agent_name = Column(String(100), index=True)
    skill_key = Column(String(120), index=True)           # 任务签名（如 defect_rate_analysis）
    title = Column(String(200), default="")
    trigger = Column(Text, default="")                    # 触发条件描述
    workflow_md = Column(Text, default="")                # 完整工作流（SKILL.md 内容）
    level0 = Column(Text, default="")                     # 快速预览（约 3000 tokens 内）
    usage_count = Column(Integer, default=0)              # 复用次数
    perf_score = Column(Float, nullable=True)             # 平均质量分
    version = Column(Integer, default=1)
    soul_version = Column(String(40), default="")         # 技能沉淀时的 Soul 版本指纹（方案2：陈旧检测）
    # 方案4：Soul 大改后自动隔离旧版本技能，使其不再被匹配/注入（可人工恢复）
    quarantined = Column(Boolean, default=False)
    quarantined_at = Column(Float, nullable=True)
    quarantine_reason = Column(Text, default="")
    created_at = Column(Float, default=0)
    updated_at = Column(Float, default=0)


class ToolOverride(Base):
    """Hermes 自进化·Tool 描述企业级覆盖（Phase 2 落点）：GEPA 优化后的 Tool 描述写这里，
    优先于 hermes/tool/tools.py 中的内置硬编码描述。"""
    __tablename__ = "tool_overrides"
    __table_args__ = (UniqueConstraint("company_id", "tool_name", name="uq_tool_override"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    tool_name = Column(String(100), index=True)
    description = Column(Text, default="")
    created_at = Column(Float, default=0)
    updated_at = Column(Float, default=0)


class EvolutionCandidate(Base):
    """Hermes 自进化·进化候选（Phase 2/3 落点）：GEPA 生成的优化变体。

    经五道护栏校验后默认 status=pending，不自动生效；管理员审批通过(approve)
    后由 service.apply_candidate 写回 Soul 文件 / tool_overrides / agent_skills。
    target_type 枚举: soul | tool | skill
    target_key    : soul->agent_name; tool->tool_name; skill->skill_key
    status 状态机 : pending -> applied ; pending -> rejected
    """
    __tablename__ = "evolution_candidates"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    agent_name = Column(String(100), index=True)
    target_type = Column(String(20), index=True)   # soul | tool | skill
    target_key = Column(String(120), default="")    # 见上
    current_text = Column(Text, default="")         # 进化前文本
    candidate_text = Column(Text, default="")       # 进化后文本
    rationale = Column(Text, default="")            # 改动理由（由 GEPA 生成）
    baseline_score = Column(Float, nullable=True)   # 基线评估分
    eval_score = Column(Float, nullable=True)       # 候选评估分
    guardrails = Column(JSON, default=dict)         # 五道护栏校验结果
    status = Column(String(20), default="pending")  # pending|approved|rejected|applied
    soul_version = Column(String(40), default="")   # 候选评估时的 Soul 版本指纹（方案3：基线漂移检测）
    created_at = Column(Float, default=0)
    decided_at = Column(Float, nullable=True)
    decided_by = Column(Integer, nullable=True)     # 审批人 user_id


class Role(Base):
    """RBAC 角色（第8章）"""
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True)
    description = Column(String(300), default="")
    company_id = Column(Integer, default=0)               # 0=平台内置角色


class Permission(Base):
    """RBAC 权限点（第8章）：如 crm:read / email:send"""
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True)
    code = Column(String(100), unique=True)               # crm:read
    description = Column(String(200), default="")


class AgentPermission(Base):
    """Agent 与权限关联（第8章 agent_permissions）"""
    __tablename__ = "agent_permissions"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String(100), index=True)          # 平台级 Agent 名
    permission_code = Column(String(100))
    company_id = Column(Integer, index=True)              # 租户级授权


class ToolPermission(Base):
    """Tool 与权限关联（第8章 tool_permissions）"""
    __tablename__ = "tool_permissions"

    id = Column(Integer, primary_key=True)
    tool_name = Column(String(100), index=True)
    permission_code = Column(String(100))
    company_id = Column(Integer, index=True)


class SmsCode(Base):
    """手机验证码"""
    __tablename__ = "sms_codes"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    mobile = Column(String(50), index=True)
    code = Column(String(10))
    purpose = Column(String(50), default="verify_mobile")  # verify_mobile / login ...
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


# ============ Agent 行业商城（落地手册第24章）============

class AgentPackage(Base):
    """Agent 商城包：行业化的 AI 员工模板"""
    __tablename__ = "agent_packages"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, index=True)   # 如 glassware_sales_pack
    title = Column(String(200))                           # 展示名
    industry = Column(String(100), index=True)            # glassware / general
    version = Column(String(20), default="1.0")
    description = Column(Text, default="")
    price = Column(Float, default=0.0)                    # 价格（元/月）
    icon = Column(String(100), default="🧩")
    agents = Column(JSON, default=list)                   # [{name, role, label, description}]
    tools = Column(JSON, default=list)                    # 预授权工具
    status = Column(String(20), default="published")      # published/draft
    install_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class CompanyPackageInstall(Base):
    """企业已安装的商城包"""
    __tablename__ = "company_package_installs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    package_id = Column(Integer, index=True)
    package_name = Column(String(100), default="")
    status = Column(String(20), default="installed")      # installed/uninstalled
    created_at = Column(DateTime, default=datetime.utcnow)


class EmailLog(Base):
    """邮件发送日志（V2.1 邮件自动化：退信处理/跟进序列依据）"""
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    recipient = Column(String(100), index=True)
    subject = Column(String(200), default="")
    status = Column(String(20), default="sent")           # sent/simulated/error/bounced
    detail = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class ChannelConfig(Base):
    """企业级渠道配置（客户自助对接企微/WhatsApp/邮件）

    channel: wecom_robot / wecom_app / whatsapp / email
    config_json: {key: value} 密钥按原样存；GET 接口脱敏返回。
    """
    __tablename__ = "channel_configs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    channel = Column(String(30), index=True)
    config_json = Column(Text, default="{}")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============ 企业安全设置 ============

class AuditLog(Base):
    """操作日志（安全审计）"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    user_id = Column(Integer, default=0)
    user_email = Column(String(100), default="")
    action = Column(String(50))                            # login/password_change/...
    detail = Column(String(500), default="")
    ip = Column(String(50), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class MemberInvite(Base):
    """成员邀请"""
    __tablename__ = "member_invites"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, index=True)
    email = Column(String(100))
    role = Column(String(20), default="staff")             # admin/staff
    code = Column(String(64), unique=True, index=True)     # 邀请码
    status = Column(String(20), default="pending")         # pending/accepted/expired
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


class Bill(Base):
    """SaaS 账单（计费系统）

    支付状态 status：
      unpaid          待支付（生成后）
      pending_review  已上传转账凭证，待财务审核（24h 缓冲期内套餐已临时生效）
      paid            审核通过，套餐正式生效
      rejected        审核驳回（未到账/凭证不符），已取消临时生效
      expired         24h 缓冲期内未获审核通过，自动取消升级
    """
    __tablename__ = "bills"

    id = Column(Integer, primary_key=True)
    bill_no = Column(String(50), unique=True, index=True)
    company_id = Column(Integer, index=True)
    plan = Column(String(50))
    amount = Column(Float, default=0.0)
    currency = Column(String(10), default="CNY")
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    status = Column(String(20), default="unpaid")          # 见类注释
    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ===== 对公转账（凭证 + 审核 + 24h 缓冲）=====
    prev_plan = Column(String(50), default="starter")      # 升级前套餐（驳回/过期时回退）
    prev_snapshot = Column(JSON, default=dict)             # 升级前套餐完整快照（回退用）
    voucher_path = Column(String(500), default="")         # 转账凭证图片路径
    voucher_note = Column(String(300), default="")         # 客户备注（如转账时间/单号）
    submitted_at = Column(DateTime, nullable=True)         # 凭证提交时间（缓冲期起算）
    grace_expires_at = Column(DateTime, nullable=True)     # 缓冲截止 = submitted_at + 24h
    reviewed_by = Column(String(100), default="")          # 审核人
    reviewed_at = Column(DateTime, nullable=True)          # 审核时间
    review_note = Column(String(300), default="")          # 审核备注（通过/驳回说明）


class InvoiceRequest(Base):
    """开票申请（开票类别：服务费；状态：pending 待开 / issued 已开 / rejected 已驳回）"""
    __tablename__ = "invoice_requests"

    id = Column(Integer, primary_key=True)
    req_no = Column(String(50), unique=True, index=True)   # 申请单号 INV...
    company_id = Column(Integer, index=True)
    bill_no = Column(String(50), index=True)               # 关联账单
    amount = Column(Float, default=0.0)                    # 开票金额
    invoice_type = Column(String(20), default="normal")    # normal/special
    category = Column(String(50), default="服务费")         # 开票类别（本平台固定：服务费）
    title = Column(String(200), default="")                # 发票抬头快照
    tax_no = Column(String(50), default="")
    bank = Column(String(200), default="")
    bank_account = Column(String(50), default="")
    address = Column(String(300), default="")
    phone = Column(String(50), default="")
    status = Column(String(20), default="pending")         # pending/issued/rejected
    invoice_no = Column(String(50), default="")            # 开具后回填发票号码
    pdf_url = Column(String(500), default="")              # 电子发票 PDF 下载链接
    provider = Column(String(20), default="")              # 开具平台：mock/baiwang/nuonuo
    provider_req_no = Column(String(100), default="")      # 第三方平台流水号
    reject_reason = Column(String(300), default="")
    issued_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ============ 平台官网内容管理（CMS）============

class SiteContent(Base):
    """平台官网（AI Factory 介绍页）的可编辑内容，单行存储（id=1）

    分段：
      hero       首屏（标题/副标题/简介/按钮）
      about      关于 AI Factory（标题/正文/数据指标）
      features   功能介绍（图标/标题/描述列表）
      customers  客户 LOGO 展示（名称/LOGO 地址列表）
      contact    联系我们（公司名/地址/电话/邮箱/地图）
    """
    __tablename__ = "site_contents"

    id = Column(Integer, primary_key=True, autoincrement=False)
    hero = Column(JSON, default=dict)
    about = Column(JSON, default=dict)
    features = Column(JSON, default=list)
    customers = Column(JSON, default=list)
    contact = Column(JSON, default=dict)
    pricing = Column(JSON, default=dict)        # 套餐价格（标题/副标题/方案列表）
    social = Column(JSON, default=dict)         # 悬浮客服（热线电话/客服QQ/微信二维码）
    pay_account = Column(JSON, default=dict)    # 对公收款账户（付款弹窗展示，平台超管维护）
    invoice_config = Column(JSON, default=dict) # 发票平台配置（provider/销售方/API 凭据）
    translations = Column(JSON, default=dict)   # 多语言翻译覆盖 {"en": {...}, "de": {...}}，仅存与默认(zh)不同的字段
    updated_at = Column(DateTime, nullable=True)


# ============ 业务系统对接配置（平台超管维护，按企业隔离）============

class IntegrationConfig(Base):
    """企业对接自有业务系统（CRM / ERP / MES / QMS）的连接配置

    每个企业每种系统最多一条记录（company_id + system_type 唯一）。
    凭证字段（api_key / password）以明文存储，生产环境应加密落库。
    """
    __tablename__ = "integration_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, index=True)
    system_type = Column(String(20))             # crm / erp / mes / qms
    enabled = Column(Boolean, default=False)     # 是否启用该对接
    endpoint = Column(String(500), default="")   # 系统访问地址（API Base URL）
    auth_type = Column(String(20), default="none")  # none / basic / apikey / bearer
    api_key = Column(String(500), default="")    # API Key / Token（apikey / bearer 时使用）
    username = Column(String(200), default="")   # basic 鉴权用户名
    password = Column(String(500), default="")   # basic 鉴权密码
    extra = Column(JSON, default=dict)           # 其它扩展配置（如数据库名/租户ID）
    status = Column(String(20), default="unconfigured")  # unconfigured / ok / error
    last_error = Column(String(500), default="")
    last_tested_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # 同企业同系统唯一
        UniqueConstraint("company_id", "system_type", name="uq_company_system"),
    )


class PlatformLLMConfig(Base):
    """平台级 LLM 网关配置（单行 id=1，供超管后台可视化切换大模型）

    持久化源；运行时由 ai.llm 的 override 缓存热加载，无需重启即可生效。
    config 结构:
    {
      "gateway": "deepseek",
      "providers": {
        "deepseek":  {"api_key": "", "base_url": "", "model": ""},
        "fireworks": {...}, "openai": {...}, "zhipu": {...},
        "dashscope": {...}, "ollama": {...}
      },
      "updated_at": "ISO..."
    }
    """
    __tablename__ = "platform_llm_config"

    id = Column(Integer, primary_key=True, default=1)   # 固定单行
    config = Column(JSON, default=dict)
    updated_at = Column(DateTime, nullable=True)


class AgentManifestRecord(Base):
    """自定义注册的 Hermes Agent manifest（持久化，重启不丢；内置 Agent 不入此表）"""
    __tablename__ = "agent_manifests"
    # 安全修复（跨租户同名覆盖）：name 全局唯一 → (company_id, name) 复合唯一，
    # 允许不同企业各自注册同名自定义 Agent；同企业内同名仍唯一（upsert 语义）。
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_agent_manifests_company_name"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(100), index=True)
    version = Column(String(20), default="1.0")
    industry = Column(String(50), default="general")
    label = Column(String(100), default="")
    description = Column(String(500), default="")
    tools = Column(JSON, default=list)           # 允许调用的 Tool 白名单
    system_prompt = Column(Text, default="")     # 系统提示词（prompt 驱动编排）
    permissions = Column(JSON, default=list)
    company_id = Column(Integer, default=0)      # 注册企业（0=平台级）
    status = Column(String(20), default="registered")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)
