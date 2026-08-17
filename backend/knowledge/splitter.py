"""
knowledge/splitter.py —— 文档切片（第九章）

产品目录可能有 200 页，需要拆成小段再向量化。
chunk_size / chunk_overlap 支持环境变量配置：
- CHUNK_SIZE 默认 800（此前 500 会切碎 Excel/BOM 表格的长行，导致公差/材质/型号跨切片分离）
- CHUNK_OVERLAP 默认 100
"""
import os

from langchain_text_splitters import RecursiveCharacterTextSplitter

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))


def split_text(text: str, chunk_size: int = None, chunk_overlap: int = None) -> list:
    if not text or not text.strip():
        return []
    size = chunk_size or CHUNK_SIZE
    overlap = chunk_overlap if chunk_overlap is not None else CHUNK_OVERLAP
    # 防御：overlap 不得 >= chunk_size（否则 splitter 行为异常）
    if overlap >= size:
        overlap = max(0, size // 5)
    # 表格/清单文档按行结构优先切分（\n 在分隔符中优先级高于标点/空格），
    # 尽量保持 BOM 表、图纸参数块的行完整性。
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", " ", ""],
    )
    return splitter.split_text(text)
