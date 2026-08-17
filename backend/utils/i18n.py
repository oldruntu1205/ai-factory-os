"""
utils/i18n.py —— 多语种工具（出口型制造业 AI 应用 第2项：多语种全链路）

- LANGUAGE_NAMES: 语言代码 → 语言名（供 LLM prompt 使用）
- detect_language: 按字符集/关键词快速检测文本语言（CJK/西里尔/阿拉伯/拉丁）
"""
import re

LANGUAGE_NAMES = {
    "zh": "中文", "en": "English", "de": "Deutsch", "fr": "Français",
    "es": "Español", "it": "Italiano", "pt": "Português", "nl": "Nederlands",
    "pl": "Polski", "ru": "Русский", "uk": "Українська", "ar": "العربية",
    "he": "עברית", "tr": "Türkçe", "ja": "日本語", "ko": "한국어",
    "vi": "Tiếng Việt", "th": "ไทย", "id": "Bahasa Indonesia", "ms": "Bahasa Melayu",
}

# 常用词语言提示（拉丁语系弱检测辅助）
_EN_WORDS = re.compile(r"\b(the|and|for|with|your|please|best|hello|hi|thanks)\b", re.I)
_DE_WORDS = re.compile(r"\b(und|die|der|das|mit|bitte|hallo|danke|sie)\b", re.I)
_FR_WORDS = re.compile(r"\b(et|le|la|les|avec|bonjour|merci|vous|nous)\b", re.I)
_ES_WORDS = re.compile(r"\b(y|el|la|los|con|hola|gracias|usted|por)\b", re.I)


def detect_language(text: str = "") -> str:
    """按字符集/关键词快速检测语言，返回语言代码（默认 en）"""
    t = (text or "").strip()
    if not t:
        return "en"
    # CJK
    cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
    hiragana = len(re.findall(r"[\u3040-\u309f]", t))
    hangul = len(re.findall(r"[\uac00-\ud7af]", t))
    if cjk > 0 and hiragana == 0 and hangul == 0:
        return "zh"
    if hiragana > 0:
        return "ja"
    if hangul > 0:
        return "ko"
    # 西里尔
    if re.search(r"[\u0400-\u04ff]", t):
        return "ru"
    # 阿拉伯/希伯来
    if re.search(r"[\u0600-\u06ff]", t):
        return "ar"
    if re.search(r"[\u0590-\u05ff]", t):
        return "he"
    # 泰/越/印尼（部分特殊字符）
    if re.search(r"[\u0e00-\u0e7f]", t):
        return "th"
    # 拉丁语系关键词
    if _ES_WORDS.search(t) and not _EN_WORDS.search(t):
        return "es"
    if _DE_WORDS.search(t) and not _EN_WORDS.search(t):
        return "de"
    if _FR_WORDS.search(t) and not _EN_WORDS.search(t):
        return "fr"
    return "en"
