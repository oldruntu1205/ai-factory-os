"""
knowledge/parser.py —— 统一文档解析入口（第五~八章）

支持：PDF / Word(docx) / Excel(xlsx,xls) / TXT / MD / CSV
"""
import os


# 解析行数上限：超大 BOM/报表转文本后会撑爆分片数与 token 成本
MAX_PARSE_ROWS = int(os.getenv("MAX_PARSE_ROWS", "2000"))


def parse_pdf(path: str) -> str:
    import fitz  # PyMuPDF
    doc = fitz.open(path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text


def parse_docx(path: str) -> str:
    import docx2txt
    return docx2txt.process(path)


def parse_doc(path: str) -> str:
    """老版 .doc 尽力解析（docx2txt 不支持的场景返回空）"""
    try:
        import docx2txt
        return docx2txt.process(path)
    except Exception:
        try:
            import subprocess
            result = subprocess.run(
                ["antiword", path], capture_output=True, text=True)
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""


def parse_excel(path: str) -> str:
    import pandas as pd
    dfs = pd.read_excel(path, sheet_name=None)
    parts = []
    for sheet_name, df in dfs.items():
        df = df.head(MAX_PARSE_ROWS)
        parts.append(f"【工作表: {sheet_name}】（前 {len(df)} 行）\n{df.to_string()}")
    return "\n\n".join(parts)


def parse_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def parse_csv(path: str) -> str:
    import pandas as pd
    df = pd.read_csv(path).head(MAX_PARSE_ROWS)
    return f"（前 {len(df)} 行）\n{df.to_string()}"


def parse_md(path: str) -> str:
    return parse_txt(path)


def parse_dxf(path: str) -> str:
    """解析 DXF（AutoCAD 文本交换格式），抽取文本实体与块属性。"""
    from knowledge.cad_parser import parse_dxf as _dxf
    return _dxf(path)


def parse_dwg(path: str) -> str:
    """解析 DWG（先转 DXF/PDF；详见 knowledge.cad_parser）。"""
    from knowledge.cad_parser import parse_cad
    return parse_cad(path)


_PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".doc": parse_doc,
    ".xlsx": parse_excel,
    ".xls": parse_excel,
    ".txt": parse_txt,
    ".md": parse_md,
    ".csv": parse_csv,
    ".dxf": parse_dxf,
    ".dwg": parse_dwg,
}


def parse_file(path: str) -> str:
    """统一解析入口：按扩展名分发"""
    ext = os.path.splitext(path)[1].lower()
    parser = _PARSERS.get(ext)
    if not parser:
        return ""
    try:
        return parser(path)
    except Exception as e:
        raise RuntimeError(f"解析文件失败 [{ext}]: {e}")
