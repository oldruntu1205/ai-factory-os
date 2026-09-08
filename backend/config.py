"""
config.py —— 全局配置（第二阶段：SaaS 多租户架构）

所有配置均可通过环境变量覆盖（见 .env.example）。
默认使用 PostgreSQL；若未配置 PostgreSQL 或设置 USE_SQLITE=1，
自动降级为本地 SQLite，方便快速启动开发调试。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# 加载本地 .env（若存在）
load_dotenv(BASE_DIR / ".env")

# ---------- 数据库 ----------
# 生产环境: postgresql://user:pass@localhost:5432/industrial_ai
# 本地开发: 留空则使用 SQLite
# 优先级：显式设置 DATABASE_URL（生产 PostgreSQL / 外部库）> USE_SQLITE=1 > 兜底 SQLite
_env_db = (os.getenv("DATABASE_URL") or "").strip()
if _env_db:
    # 容器内通过环境变量注入 DATABASE_URL 时，强制走该库（忽略 .env 里的 USE_SQLITE）
    DATABASE_URL = _env_db
elif os.getenv("USE_SQLITE") == "1":
    _sqlite_path = Path(os.getenv("SQLITE_PATH", str(BASE_DIR / "industrial_ai.db")))
    DATABASE_URL = f"sqlite:///{_sqlite_path.as_posix()}"
else:
    # 兜底：既没设 DATABASE_URL 也没开 USE_SQLITE → 本地 SQLite（开发/演示）
    _sqlite_path = Path(os.getenv("SQLITE_PATH", str(BASE_DIR / "industrial_ai.db")))
    DATABASE_URL = f"sqlite:///{_sqlite_path.as_posix()}"

# ---------- JWT ----------
# 安全修复：SECRET_KEY 不再硬编码默认值。未配置时自动生成并持久化到 backend/.secret_key
# （重启不失效）；生产可用环境变量 SECRET_KEY 显式注入。
import secrets as _secrets
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    _key_file = BASE_DIR / ".secret_key"
    try:
        if _key_file.exists():
            SECRET_KEY = _key_file.read_text().strip()
        if not SECRET_KEY:
            SECRET_KEY = _secrets.token_hex(32)
            _key_file.write_text(SECRET_KEY)
    except Exception as e:
        # P2 安全提示：无法持久化密钥时告警（否则重启换 key，已加密的集成/渠道凭据将不可解）
        import warnings
        warnings.warn(f"无法持久化 SECRET_KEY 到 {_key_file}（{e}），"
                      "重启后已加密凭据将不可解密，请检查目录可写或配置 SECRET_KEY 环境变量")
        SECRET_KEY = SECRET_KEY or _secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("ACCESS_TOKEN_EXPIRE_DAYS", "7"))

# ---------- 上传目录 ----------
UPLOAD_DIR = os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))

# ---------- LLM 网关（第12章）----------
# gateway 可选: deepseek(默认) / fireworks / openai / zhipu / dashscope / ollama
LLM_GATEWAY = os.getenv("LLM_GATEWAY", "deepseek")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
# Fireworks AI 专用配置
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY", "")
FIREWORKS_MODEL = os.getenv("FIREWORKS_MODEL",
                           "accounts/fireworks/models/llama-v3p1-70b-instruct")
# 未配置 API Key 时是否降级 MockLLM（生产建议 LLM_FALLBACK=0，防止输出伪造报价/排产数据）
LLM_FALLBACK = os.getenv("LLM_FALLBACK", "1").lower() in ("1", "true", "yes", "on")

# ---------- Qdrant 向量库 ----------
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
# 现为每租户向量集合名的前缀：实际集合名为 {QDRANT_COLLECTION}_{company_id}
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "company_docs")
# 未配置 Qdrant 时自动降级为内存向量库（重启丢失，仅供演示）
QDRANT_FALLBACK_MEMORY = True

# ---------- Embedding ----------
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "512"))
# fastembed 首次运行会下载模型；失败时降级为本地哈希向量
EMBEDDING_FALLBACK = True

# ---------- 外部渠道（第五阶段）----------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
# 安全修复：移除硬编码默认 'verify_token'（未配置时 webhook 握手 fail-closed）
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")   # Meta App Secret（webhook 签名校验）

# ---------- 企业微信渠道 ----------
WECOM_ROBOT_KEY = os.getenv("WECOM_ROBOT_KEY", "")
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID", "")
WECOM_SECRET = os.getenv("WECOM_SECRET", "")
WECOM_TOKEN = os.getenv("WECOM_TOKEN", "")
WECOM_AES_KEY = os.getenv("WECOM_AES_KEY", "")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "sales@company.com")

# ---------- Redis / Celery / RabbitMQ（第7章 消息队列）----------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# 队列模式: auto(自动探测) / rabbitmq(企业版) / redis / memory
QUEUE_MODE = os.getenv("QUEUE_MODE", "auto")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

# ---------- SMS 验证码（安全修复：随机码、接口不回显；演示模式写服务端日志）----------
# SMS_MOCK_MODE=1（默认）验证码写日志便于本地联调；生产置 0 并接入真实短信网关
SMS_MOCK_MODE = os.getenv("SMS_MOCK_MODE", "1").lower() in ("1", "true", "yes", "on")

# ---------- 自进化定时调度器（Phase 5 周期自进化）----------
# 是否启用后台周期自进化守护线程（0 关闭；关闭后仍可用系统 crontab 跑 scripts/auto_evolve_cron.py）
EVOLUTION_SCHEDULER_ENABLED = os.getenv("EVOLUTION_SCHEDULER_ENABLED", "1").lower() in ("1", "true", "yes", "on")
# 调度间隔（秒），默认 24 小时
EVOLUTION_SCHEDULER_INTERVAL = int(os.getenv("EVOLUTION_SCHEDULER_INTERVAL", "86400"))
# 应用启动后延迟多久首次运行（秒），默认 5 分钟，避免启动即触发重型 LLM 调用
EVOLUTION_SCHEDULER_START_DELAY = int(os.getenv("EVOLUTION_SCHEDULER_START_DELAY", "300"))
# auto_evolve_cycle 轨迹回溯窗口（天）
EVOLUTION_SCHEDULER_LOOKBACK_DAYS = int(os.getenv("EVOLUTION_SCHEDULER_LOOKBACK_DAYS", "30"))
# 周期循环进化目标类型（默认 soul + skill；tool/tool_code 仍走手动 /approve 流程，绝不自动改运行代码）
_tt = os.getenv("EVOLUTION_SCHEDULER_TARGET_TYPES", "soul,skill")
EVOLUTION_SCHEDULER_TARGET_TYPES = tuple(
    t.strip() for t in _tt.split(",") if t.strip()) or ("soul", "skill")

# ---------- 演示种子数据 ----------
# 生产环境建议 SEED_DEMO=0，避免自动注入演示企业/AI 员工/商城包
SEED_DEMO = os.getenv("SEED_DEMO", "1").lower() in ("1", "true", "yes", "on")

# ---------- 计费生产门禁（安全修复：防演示数据被真实环境误用）----------
# BILLING_ENFORCE_LIVE=1（正式收费时置 1）：收款账户未配置（回退演示账号）时拒绝
# upgrade/pay；发票平台为 mock 时拒绝一键开票（防客户打款到演示账号 / 开出 DEMO 假票）。
# 默认 0 保持本地/试运营演示流程畅通。
BILLING_ENFORCE_LIVE = os.getenv("BILLING_ENFORCE_LIVE", "0").lower() in ("1", "true", "yes", "on")

# ---------- 邮件追踪/退订外链基础地址 ----------
# 跟踪像素与退订页需要的公网可访问地址；部署时设为真实域名
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
