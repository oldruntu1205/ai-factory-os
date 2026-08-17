"""
knowledge/embedding.py —— Embedding 向量化（第十章）

优先使用 fastembed 中文模型 BAAI/bge-small-zh-v1.5；
首次使用会下载模型；下载失败或未安装时降级为本地哈希向量，
保证系统在无外网/无 GPU 环境下依然可演示全链路。

一致性提示：
- 降级哈希向量与真实模型向量处于不同语义空间、精度显著偏低，切换时打 warning。
- create_embedding 记录实际输出维度（_active_dim），供 vector.ensure_collection
  建集合 / save_vectors 校验维度一致，避免「集合 512 维、写入 1024 维」这类静默崩溃。
"""
import hashlib
import logging
import math
import re

from config import EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_FALLBACK

logger = logging.getLogger(__name__)

_model = None
_use_fallback = False
_active_dim = None


def _load_model():
    global _model, _use_fallback
    try:
        from fastembed import TextEmbedding
        _model = TextEmbedding(EMBEDDING_MODEL)
        _use_fallback = False
        logger.info("Embedding 模型加载成功: %s", EMBEDDING_MODEL)
    except Exception as e:
        _use_fallback = True
        logger.warning("Embedding 模型加载失败，降级为本地哈希向量（语义精度显著降低）: %s", e)


def _hash_vector(text: str, dim: int = EMBEDDING_DIM) -> list:
    """降级方案：基于多哈希的特征向量（仅用于离线演示）。

    分词同时支持「连续英文/数字」与「CJK 单字」，避免中文无空格导致
    整段塌缩为一个 token、所有中文片段向量高度雷同（中文降级失效）。
    """
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9]+|[一-鿿]", (text or "").lower())
    for token in tokens[:256]:
        for seed in range(1, 4):
            h = hashlib.md5(f"{seed}:{token}".encode()).hexdigest()
            idx = int(h[:8], 16) % dim
            vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def create_embedding(texts: list) -> list:
    """批量文本 -> 向量列表"""
    global _use_fallback, _active_dim
    if not texts:
        return []
    if _model is None:
        _load_model()

    if not _use_fallback and _model is not None:
        try:
            vecs = list(_model.embed(texts))
            _active_dim = len(vecs[0])
            return vecs
        except Exception:
            # 修复：单次推理失败仅本次降级（不永久粘性），下次继续尝试真实模型；
            # 仅模型加载失败（_load_model）才置 _use_fallback
            if not EMBEDDING_FALLBACK:
                raise
            logger.warning("Embedding 单次推理失败，本次降级为本地哈希向量（下次将重试真实模型）")
            vecs = [_hash_vector(t) for t in texts]
            _active_dim = len(vecs[0]) if vecs else EMBEDDING_DIM
            return vecs

    vecs = [_hash_vector(t) for t in texts]
    _active_dim = len(vecs[0]) if vecs else EMBEDDING_DIM
    if _use_fallback:
        logger.warning("当前使用降级哈希向量（维度 %d），语义检索精度显著低于真实 Embedding 模型",
                       _active_dim)
    return vecs


def get_embedding_dim() -> int:
    """返回当前实际使用的向量维度（未执行过则回退到配置值，用于集合维度校验）。"""
    return _active_dim or EMBEDDING_DIM
