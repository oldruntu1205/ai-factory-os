"""
ai/llm.py —— LLM Gateway（模型路由）

统一封装大模型调用，支持模型路由：
- DeepSeek（默认，deepseek-chat / deepseek-reasoner）
- Fireworks AI / OpenAI / 智谱 / 通义 / Ollama 等任意 OpenAI 兼容接口
未配置 API Key 时自动降级为内置 MockLLM，保证全链路可演示。
"""
import logging
import os
import base64
import threading

from config import (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_FALLBACK,
                    LLM_GATEWAY, FIREWORKS_API_KEY, FIREWORKS_MODEL)

logger = logging.getLogger(__name__)

_llm = None
_use_mock = False

# 性能/成本控制：LLM 调用全局并发信号量（防费用暴涨与 API 429）
_llm_semaphore = threading.Semaphore(int(os.getenv("LLM_MAX_CONCURRENCY", "4")))


class _RateLimitedLLM:
    """LLM 并发限流包装：所有 invoke 走全局信号量"""

    def __init__(self, inner):
        self._inner = inner

    def invoke(self, *args, **kwargs):
        with _llm_semaphore:
            return self._inner.invoke(*args, **kwargs)

# 运行时覆盖配置（平台超管后台可视化设置，优先于环境变量）
# 结构: {"gateway": str, "keys": {gw: key}, "base_urls": {gw: url}, "models": {gw: model}}
_config_override = None


def set_config_override(cfg: dict | None):
    """设置/清除运行时 LLM 配置覆盖，并重置已构建的 LLM 实例（热切换）"""
    global _config_override, _llm, _use_mock
    _config_override = cfg
    _llm = None
    _use_mock = False


def get_config_override():
    return _config_override

# 模型路由表：gateway -> (base_url, model)
GATEWAY_ROUTES = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "fireworks": ("https://api.fireworks.ai/inference/v1",
                  "accounts/fireworks/models/llama-v3p1-70b-instruct"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "ollama": ("http://localhost:11434/v1", "llama3.1"),
}

# DeepSeek 模型
DEEPSEEK_CHAT = "deepseek-chat"           # V3，通用对话
DEEPSEEK_REASONER = "deepseek-reasoner"   # R1，深度推理（Agent 分析任务推荐）


class MockLLM:
    """离线降级：基于规则返回的演示 LLM"""

    def invoke(self, prompt: str):
        # 极简意图识别，保证演示链路完整
        if "customer" in prompt and ("销售" in prompt or "sales" in prompt):
            content = "sales"
        elif "customer" in prompt:
            content = "customer"
        else:
            content = _demo_answer(prompt)
        return SimpleMessage(content)


class SimpleMessage:
    def __init__(self, content: str):
        self.content = content


def _resolve_route() -> tuple:
    """按 gateway 配置解析 (base_url, model)，优先使用后台运行时覆盖配置。

    安全/正确性修复：全局 env（LLM_BASE_URL/LLM_MODEL）仅回退到**默认网关**，
    切换网关时使用 GATEWAY_ROUTES 内置默认，避免把 deepseek 的 base_url/model 发到别家。
    """
    cfg = _config_override
    gateway = (cfg or {}).get("gateway") or (LLM_GATEWAY or "deepseek")
    gateway = gateway.lower()

    keys = (cfg or {}).get("keys") or {}
    base_urls = (cfg or {}).get("base_urls") or {}
    models = (cfg or {}).get("models") or {}
    is_default_gateway = gateway == (LLM_GATEWAY or "deepseek").lower()

    # Fireworks 专用配置优先
    if gateway == "fireworks":
        if keys.get("fireworks") or FIREWORKS_API_KEY:
            base = base_urls.get("fireworks") or "https://api.fireworks.ai/inference/v1"
            model = models.get("fireworks") or FIREWORKS_MODEL
            return (base, model)

    if gateway in GATEWAY_ROUTES:
        base_url, model = GATEWAY_ROUTES[gateway]
        return (base_urls.get(gateway) or (LLM_BASE_URL if is_default_gateway else "") or base_url,
                models.get(gateway) or (LLM_MODEL if is_default_gateway else "") or model)

    return (LLM_BASE_URL, LLM_MODEL)


def _get_llm():
    global _llm, _use_mock
    if _llm is not None:
        return _llm

    cfg = _config_override
    gateway = (cfg or {}).get("gateway") or (LLM_GATEWAY or "deepseek")
    gateway = gateway.lower()
    keys = (cfg or {}).get("keys") or {}
    is_default_gateway = gateway == (LLM_GATEWAY or "deepseek").lower()

    if gateway == "fireworks":
        api_key = keys.get("fireworks") or FIREWORKS_API_KEY
    else:
        # 修复：全局 LLM_API_KEY 仅回退默认网关，切网关用 override 的按网关 key
        api_key = keys.get(gateway) or (LLM_API_KEY if is_default_gateway else "")

    if not api_key:
        if not LLM_FALLBACK:
            raise RuntimeError("未配置 LLM API Key 且禁止降级")
        _use_mock = True
        _llm = MockLLM()
        return _llm

    base_url, model = _resolve_route()
    try:
        from langchain_openai import ChatOpenAI
        # 可靠性加固：超时 + 重试（此前默认 600s 无重试，请求可挂死线程）
        # 性能：并发信号量限流（LLM_MAX_CONCURRENCY）
        _llm = _RateLimitedLLM(ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.3,
            max_tokens=2048,
            timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        ))
        logger.info(f"LLM Gateway: [{gateway}] model={model} base={base_url}")
    except Exception:
        if not LLM_FALLBACK:
            raise
        _use_mock = True
        _llm = MockLLM()
    return _llm


def get_llm():
    return _get_llm()


def is_mock() -> bool:
    return _use_mock


# 视觉（多模态）模型判定：目前仅 OpenAI 网关的 gpt-4o 系列稳定支持图片输入
_VISION_HINTS = ("4o", "vision", "gpt-4-turbo", "gpt-4v")


def is_vision_capable() -> bool:
    """当前配置的网关/模型是否支持图片多模态输入（如手绘图纸照片）。"""
    cfg = _config_override
    gateway = (cfg or {}).get("gateway") or (LLM_GATEWAY or "deepseek")
    gateway = gateway.lower()
    if gateway != "openai":
        return False
    models = (cfg or {}).get("models") or {}
    model = models.get("openai") or LLM_MODEL or GATEWAY_ROUTES["openai"][1]
    model = (model or "").lower()
    return any(h in model for h in _VISION_HINTS)


def vision_invoke(prompt_text: str, image_paths: list) -> str:
    """多模态调用：把文本与本地图片一起发给视觉模型，返回文本回答。

    仅当 is_vision_capable() 为 True 时调用（否则图片无法识别）。
    """
    cfg = _config_override
    gateway = (cfg or {}).get("gateway") or (LLM_GATEWAY or "deepseek")
    gateway = gateway.lower()
    keys = (cfg or {}).get("keys") or {}
    # P2 修复：全局 LLM_API_KEY 仅回退默认网关（与 _get_llm 一致，防 key 误投第三方）
    is_default_gateway = gateway == (LLM_GATEWAY or "deepseek").lower()
    api_key = keys.get(gateway) or (LLM_API_KEY if is_default_gateway else "")
    if not api_key:
        return "⚠️ 当前未配置支持图片识别的模型 API Key，无法解析图片。请上传 DXF/PDF/文本类图纸，或在「平台设置-模型设置」中配置 OpenAI（gpt-4o）后重试。"

    base_url, model = _resolve_route()
    try:
        from langchain_openai import ChatOpenAI
        llm = _RateLimitedLLM(ChatOpenAI(
            model=model, api_key=api_key, base_url=base_url,
            temperature=0.3, max_tokens=2048,
            timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        ))
    except Exception:
        if not LLM_FALLBACK:
            raise
        return "⚠️ 视觉模型初始化失败，请检查模型配置。"

    content = [{"type": "text", "text": prompt_text}]
    _MIME = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    }
    for p in image_paths or []:
        ext = os.path.splitext(p)[1].lower()
        mime = _MIME.get(ext, "image/png")
        try:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        except Exception as e:
            content.append({"type": "text",
                             "text": f"（图片 {os.path.basename(p)} 读取失败：{e}）"})
    try:
        res = llm.invoke(content)
        return res.content
    except Exception as e:
        return f"⚠️ 视觉模型调用失败：{e}\n请尝试上传更清晰的图片，或改传 DXF/PDF/文本图纸。"


def _demo_answer(prompt: str) -> str:
    """Mock 模式下根据提示词返回贴近场景的演示答案"""
    # 销售分析
    if "工业产品销售专家" in prompt or "销售专家" in prompt:
        return (
            "产品: 玻璃烧杯 500ml\n"
            "数量: 1000\n"
            "国家: 待确认\n"
            "购买意向: 高\n"
            "建议: 发送报价"
        )
    # 报价
    if "报价助手" in prompt or "生成报价" in prompt:
        return (
            '{"product": "500ml硼硅酸盐玻璃烧杯", "quantity": 1000, '
            '"unit_price": 1.20, "delivery_days": 30, '
            '"payment_terms": "T/T 30% deposit", "currency": "USD"}'
        )
    # 报价单 JSON（api/quotation.py 使用）
    if "客户需求生成报价" in prompt or "quotation" in prompt.lower() or "根据客户需求生成报价" in prompt:
        return (
            '{"product": "500ml硼硅酸盐玻璃烧杯", "quantity": 1000, '
            '"unit_price": 1.20, "delivery_days": 30, '
            '"payment_terms": "T/T 30% deposit", "currency": "USD"}'
        )
    # 生产计划
    if "生产计划专家" in prompt:
        return (
            "分析结论:\n"
            "1. 是否延期: 否\n"
            "2. 生产瓶颈: 产线A01 负载 70%，仍有余量\n"
            "3. 优化建议: 保持当前排产，可提前 4 天完工"
        )
    # 质量
    if "质量" in prompt and "专家" in prompt:
        return (
            "异常发现: A03 产线裂纹率上升\n"
            "可能原因:\n"
            "1. 模具温度波动\n"
            "2. 原材料批次变更\n"
            "3. 冷却速度过快\n"
            "建议: 检查烧结参数并抽检原材料批次"
        )
    # 工艺/图纸助手（防范误操作）
    if "工艺/图纸助手" in prompt or "防范工人误操作" in prompt:
        return (
            "【精确值（来自图纸参数抽取）】\n"
            "- 外径公差: ±0.05（来源: 已上传图纸 / 图框）\n"
            "- 基体材质: 6061-T6（来源: 已上传图纸 / 标题栏）\n"
            "- 执行标准: GB/T 1804-m\n\n"
            "【防误操作提示】\n"
            "1. 6061-T6 为铝合金，加工禁止干切（易粘刀/积屑瘤），须用专用切削液。\n"
            "2. 外径公差仅 ±0.05，装夹禁止过度夹紧导致变形，建议软爪/胀套。\n"
            "3. 未注公差按 IT7，去毛刺不得损伤配合面。\n"
            "4. 如与原图标注不一致，请先核对原图再开工。"
        )
    # 经营日报
    if "经营日报" in prompt or "经营" in prompt:
        return (
            "今日经营摘要:\n"
            "- 销售: 新增询盘 56，订单 +18%\n"
            "- 生产: 3 个风险项\n"
            "- 库存: 玻璃管库存下降\n"
            "- 建议: 提前采购玻璃管原材料"
        )
    # 市场分析
    if "B2B工业销售专家" in prompt or "目标客户" in prompt:
        return (
            '{"industry_match": true, "has_import": true, '
            '"employee_count": 120, "contact_found": true, '
            '"score": 92, "grade": "A", "strategy": "发送定制开发信"}'
        )
    # 开发信
    if "英文开发信" in prompt:
        return (
            "Subject: Premium Laboratory Glassware Manufacturer\n\n"
            "Dear Sir/Madam,\n\n"
            "We are a leading manufacturer of borosilicate laboratory glassware "
            "with 20 years of export experience to Europe and the USA.\n\n"
            "Our products are ISO9001 and CE certified with competitive pricing.\n\n"
            "Could we send you our latest catalog and quotation?\n\n"
            "Best regards,\nSales Team"
        )
    # 默认：客服 RAG 回答
    return (
        "根据企业知识库资料，关于您的问题：\n"
        "我们建议您查阅产品目录第 2 章，该部分详细说明了产品规格与认证信息。\n"
        "如需进一步了解，欢迎继续咨询。"
    )
