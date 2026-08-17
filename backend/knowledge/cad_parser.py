"""
knowledge/cad_parser.py —— CAD/图纸解析与参数抽取（工艺/图纸助手核心）

设计目标：
- DXF（AutoCAD 文本交换格式）：用 ezdxf 读取 TEXT/MTEXT/ATTDEF/INSERT 属性，
  拼成可检索文本，并抽取结构化参数。
- DWG：ezdxf 不能直接读；若装了 ODA File Converter（ezdxf.addons.odafc）则先转 DXF，
  否则返回友好提示，由前端引导用户「另存为 DXF/PDF」。
- PDF/DOC/XLSX：文本已由 parser.parse_file 抽出，本模块只负责在文本上做参数抽取，
  因此 Excel BOM 表、PDF 标题栏同样能被识别为材质/公差。

抽取能力（规则 + 正则，轻量无外部依赖，可离线演示）：
- 公差：±0.05、+0.02/-0.01、IT7、GB/T 1804-m、H7/g6 等
- 材质：6061-T6、Q235、45#、SUS304、20CrMnTi、ASTM A36、ADC12 等
- 尺寸：Φ20、M8、长度 120mm、R5 等
- 标准：GB/T、ISO、ASTM、DIN、JIS 等
- 产品型号：常见型号写法（如 XK-205、型号: ABC123）

所有抽取结果都带 raw 片段，便于前端「引用来源」展示与审计。
"""
import os
import re
import logging

logger = logging.getLogger(__name__)


# ---------- 产品型号（用于结构化精确查的 key） ----------
_MODEL_PATTERNS = [
    re.compile(r"型号[:\s：]*([A-Za-z0-9][A-Za-z0-9\-_/.]{1,30})", re.I),
    re.compile(r"图号[:\s：]*([A-Za-z0-9][A-Za-z0-9\-_/.]{1,30})", re.I),
    re.compile(r"零件号[:\s：]*([A-Za-z0-9][A-Za-z0-9\-_/.]{1,30})", re.I),
    re.compile(r"model[:\s：]*([A-Za-z0-9][A-Za-z0-9\-_/.]{1,30})", re.I),
    re.compile(r"\b([A-Z]{1,4}-?\d{2,6}[A-Z]?)\b"),   # XK-205 / ABC123
]

# 这些前缀开头的代号是「标准/认证号」而非产品型号，必须从型号抽取中排除，
# 否则 ISO9001 / GB1804 等会被误当型号，进而把就近公差/材质错绑到它们上。
_STANDARD_PREFIXES = ("GB", "GB/T", "GB/Z", "ISO", "DIN", "JIS", "EN",
                      "BS", "ASTM", "AS", "NF", "GOST", "QC", "JB", "HG")


def _is_valid_model(val: str) -> bool:
    """排除标准/认证号误当型号。

    标准代号形如 GB/T 1804、ISO9001、DIN EN 等：前缀后紧跟数字或 '/' 或 '-' 即视为标准号。
    """
    v = (val or "").upper()
    if not v:
        return False
    for p in _STANDARD_PREFIXES:
        if v == p:
            return False
        if v.startswith(p) and len(v) > len(p):
            nxt = v[len(p)]
            if nxt.isdigit() or nxt in "/-":
                return False
    return True

# ---------- 公差 ----------
_TOLERANCE_PATTERNS = [
    re.compile(r"[±]\s*\d+(?:\.\d+)?\s*(?:mm|°|′|″)?", re.I),
    re.compile(r"[+\-]\s*\d+(?:\.\d+)?\s*/\s*[+\-]\s*\d+(?:\.\d+)?"),
    re.compile(r"\bIT\s*[0-9]{1,2}\b", re.I),
    re.compile(r"\b(?:GB|T|GB\/T)\s*\d+[^\n]{0,20}?(?:f|m|h|g|e|c|v|H|G|E|C|V)\b", re.I),
    re.compile(r"\b[HhGgEeFf]\s*[0-9]?\s*/\s*[hgfesp]\s*[0-9]?\b"),  # H7/g6
    re.compile(r"\b(?:公差)\s*[:：]?\s*([^；;\n]{1,40})"),
]

# ---------- 材质 ----------
_MATERIAL_PATTERNS = [
    re.compile(r"\b(?:6061|6063|7075|6082|5052)-?[Tt]?[0-9]?\b"),
    re.compile(r"\bQ(?:235|345|195|275|420|460|550|690)\b"),
    re.compile(r"\b(?:20|45|40Cr|20CrMnTi|GCr15|16Mn|65Mn|T8|T10)\b"),
    re.compile(r"\bSUS(?:201|304|316|316L|430|420)\b"),
    re.compile(r"\bADC1?2\b"),
    re.compile(r"\bASTM\s*[A-Z]?\s*\d+[^\n]{0,15}?", re.I),
    re.compile(r"\b(?:HT|QT|ZL|ZAl|LY|LD)\d{2,4}[A-Z]?\b"),
    re.compile(r"\b(?:材质|材料|基体)\s*[:：]?\s*([^；;\n]{1,40})"),
]

# ---------- 尺寸 ----------
_DIMENSION_PATTERNS = [
    re.compile(r"[ΦØ⌀]\s*\d+(?:\.\d+)?\s*(?:mm|cm)?", re.I),
    re.compile(r"\bM\d+(?:\.?\d+)?\b(?:\s*[xX×]\s*\d+)?"),  # M8 / M8x1.25
    re.compile(r"\bR\s*\d+(?:\.\d+)?\b"),                     # R5
    re.compile(r"(?:长度|宽度|厚度|直径|总长|孔径|中心距)\s*[:：]?\s*\d+(?:\.\d+)?\s*(?:mm|cm|°)"),
]

# ---------- 标准 ----------
_STANDARD_PATTERNS = [
    re.compile(r"\b(?:GB|GB\/T|GB\/Z|ISO|ASTM|DIN|JIS|EN|BS)\s*\d+[^\n]{0,25}?", re.I),
]

_CATEGORY_PATTERNS = {
    "tolerance": _TOLERANCE_PATTERNS,
    "material": _MATERIAL_PATTERNS,
    "dimension": _DIMENSION_PATTERNS,
    "standard": _STANDARD_PATTERNS,
}


def extract_product_models(text: str) -> list:
    """抽取文本中出现的所有产品型号（去重，保序）；排除标准/认证号误召回。"""
    found = []
    for pat in _MODEL_PATTERNS:
        for m in pat.finditer(text or ""):
            val = m.group(1).strip()
            if val and _is_valid_model(val) and val not in found:
                found.append(val)
    return found


def extract_drawing_params(text: str, source_file: str = "", location: str = "") -> list:
    """从图纸/文档文本中抽取结构化参数列表。

    返回 [ {category, param_name, value, raw, product_model?, source_file, location} ]
    每条命中尽量反推其所属产品型号（就近向上找最近的型号）。
    """
    if not text:
        return []

    # 预先定位所有型号出现位置（用于就近归属）
    model_spans = []
    for pat in _MODEL_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1).strip()
            if _is_valid_model(name):
                model_spans.append((m.start(), name))
    model_spans.sort()

    def nearest_model(pos: int) -> str:
        # 取 pos 之前最近出现的型号
        cur = ""
        for s, name in model_spans:
            if s <= pos:
                cur = name
            else:
                break
        return cur

    params = []
    seen = set()
    for category, patterns in _CATEGORY_PATTERNS.items():
        for pat in patterns:
            for m in pat.finditer(text):
                raw = m.group(0).strip()
                if len(raw) < 2:
                    continue
                # 「key: value」结构：value 取冒号后部分，并去掉前缀标签噪声
                value = raw
                if m.groups() and m.group(1):
                    value = m.group(1).strip()
                norm_value = value.split(":", 1)[-1].strip() if ":" in value else value
                norm_value = norm_value.split("：", 1)[-1].strip()
                if not norm_value:
                    norm_value = value
                # 去重（同分类同归一值只保留一次，避免「6061-T6」与「材质: 6061-T6」重复）
                key = (category, norm_value)
                if key in seen:
                    continue
                seen.add(key)
                # 全局默认公差（如「未注公差按 IT7」）不强绑某个型号
                model = nearest_model(m.start())
                # 向前看更宽的窗口（40 字），覆盖「所有未注公差按 IT7」等较长前缀
                ctx_before = text[max(0, m.start() - 40):m.start()]
                if "未注" in ctx_before or "未注" in raw or "未注" in value:
                    model = ""
                params.append({
                    "category": category,
                    "param_name": _category_label(category),
                    "value": norm_value,
                    "raw": raw,
                    "product_model": model,
                    "source_file": source_file,
                    "location": location,
                })
    return params


def _category_label(category: str) -> str:
    return {
        "tolerance": "公差/等级",
        "material": "材质",
        "dimension": "尺寸",
        "standard": "执行标准",
    }.get(category, category)


# ---------- DXF 解析（ezdxf） ----------

def parse_dxf(path: str) -> str:
    """读取 DXF 文本实体与块属性，拼成可检索文本。"""
    import ezdxf
    doc = ezdxf.readfile(path)
    parts = []
    msp = doc.modelspace()

    for entity in msp:
        dxftype = entity.dxftype()
        if dxftype in ("TEXT", "MTEXT", "ATTRIB"):
            txt = getattr(entity, "text", "") or getattr(entity, "plain_text", "")
            if txt:
                parts.append(txt)
        elif dxftype == "INSERT":
            # 块参照：抽取其属性
            try:
                for attrib in entity.attribs:
                    if attrib.text:
                        parts.append(attrib.text)
            except Exception:
                pass
        elif dxftype == "TABLE":
            # 标题栏/明细表：逐 cell 取文本
            try:
                for row in entity:
                    for cell in row:
                        t = cell.text
                        if t:
                            parts.append(t)
            except Exception:
                pass

    # 块定义里的属性定义也纳入（标题栏常定义成块）
    try:
        for block in doc.blocks:
            for entity in block:
                if entity.dxftype() in ("ATTDEF", "TEXT", "MTEXT"):
                    txt = getattr(entity, "text", "") or getattr(entity, "plain_text", "")
                    if txt:
                        parts.append(txt)
    except Exception:
        pass

    return "\n".join(p for p in parts if p and p.strip())


def parse_cad(path: str) -> str:
    """统一 CAD 入口：DXF 直接解析；DWG 尝试 ODA 转换，否则报错提示。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dxf":
        return parse_dxf(path)
    if ext == ".dwg":
        return _parse_dwg(path)
    raise RuntimeError(f"不支持的 CAD 格式: {ext}")


def _parse_dwg(path: str) -> str:
    """DWG 需先转 DXF。优先用 ezdxf.addons.odafc（需安装 ODA File Converter）。"""
    try:
        from ezdxf.addons import odafc
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
        tmp.close()
        try:
            odafc.convert(path, tmp.name)
            text = parse_dxf(tmp.name)
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
        return text
    except Exception as e:
        raise RuntimeError(
            "DWG 为 AutoCAD 私有格式，需先用 AutoCAD『另存为 DXF/PDF』，"
            "或安装 ODA File Converter 后重试。解析失败原因: " + str(e)
        )


def parse_and_extract(path: str, source_file: str = "") -> tuple:
    """CAD 专属：返回 (抽取文本, 结构化参数列表)。"""
    text = parse_cad(path)
    params = extract_drawing_params(text, source_file=source_file or os.path.basename(path))
    return text, params
