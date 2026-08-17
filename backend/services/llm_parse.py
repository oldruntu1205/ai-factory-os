"""
services/llm_parse.py —— LLM 结构化输出解析服务（分层重构）

把「LLM 自由文本 -> 结构化 JSON」的通用能力从 API 层抽离，
供 quotation / email sequence / sales discovery 等模块复用。
- 容错：非 JSON 输出自动降级为默认值，不再让 API 抛 500
"""
import json
import logging
import re

logger = logging.getLogger(__name__)


def parse_json_output(content: str, defaults: dict) -> dict:
    """从 LLM 输出中提取 JSON 对象；失败或类型异常时用 defaults 兜底

    - 自动定位第一个 { ... } 块
    - 字段类型强制转换（str/int/float），非法值回退默认
    """
    if not content:
        return dict(defaults)
    try:
        m = re.search(r"\{.*\}", content, re.S)
        raw = m.group() if m else content
        data = json.loads(raw)
        if not isinstance(data, dict):
            return dict(defaults)
        return {**defaults, **data}
    except Exception as e:
        logger.debug(f"LLM JSON 解析失败，使用默认值: {e}")
        return dict(defaults)


def safe_int(value, default: int) -> int:
    """安全转 int：None/非法值返回默认"""
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def safe_float(value, default: float) -> float:
    """安全转 float：None/非法值返回默认"""
    try:
        v = float(value)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def safe_str(value, default: str = "") -> str:
    """安全转 str（None -> 默认）"""
    return str(value) if value is not None else default
