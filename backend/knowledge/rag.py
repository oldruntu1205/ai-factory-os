"""
knowledge/rag.py —— RAG 检索（第十五章）

问题 -> 向量化 -> 企业隔离检索 top-k -> 返回相关文本片段
"""
from knowledge.embedding import create_embedding
from knowledge.vector import _get_client, _use_memory, _memory_store, collection_for


def _search(question: str, company_id: int, limit: int = 5,
            score_threshold: float = None, with_source: bool = False) -> list:
    """内部共用实现：检索企业知识库，返回相关文本片段（可选含来源）。

    集合已按租户物理隔离，无需 company_id 过滤条件。
    score_threshold: 余弦相似度阈值，低于该值的片段不返回
        （None 或负值表示不过滤；qdrant 与内存分支统一用 -1.0 兜底）。
    """
    vector = create_embedding([question])[0]
    thr = score_threshold if score_threshold is not None else -1.0

    c = _get_client()
    if not _use_memory and c is not None:
        # 新版 qdrant_client 已移除 c.search，改用语义一致的 query_points
        resp = c.query_points(
            collection_name=collection_for(company_id),
            query=vector,
            limit=limit,
            score_threshold=thr,
        )
        if with_source:
            return [{"text": pt.payload.get("text", ""), "source": pt.payload.get("source", "")}
                    for pt in resp.points if pt.payload]
        return [pt.payload.get("text", "") for pt in resp.points if pt.payload]

    # 内存降级：余弦相似度检索
    store = _memory_store.get(company_id, [])
    if not store:
        return []
    scored = []
    for item in store:
        score = _cosine(vector, item["vector"])
        if score < thr:
            continue
        scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    if with_source:
        return [{"text": item["text"], "source": item.get("source", "")}
                for _, item in scored[:limit]]
    return [item["text"] for _, item in scored[:limit]]


def search(question: str, company_id: int, limit: int = 5,
           score_threshold: float = None) -> list:
    """检索企业知识库，返回相关文本片段列表。
    集合已按租户物理隔离，无需 company_id 过滤条件。
    score_threshold: 余弦相似度阈值，低于该值的片段不返回（None 表示不过滤）。
    """
    return _search(question, company_id, limit, score_threshold, with_source=False)


def search_with_source(question: str, company_id: int, limit: int = 5,
                       score_threshold: float = None) -> list:
    """检索并携带来源文件名（用于前端展示引用）。
    集合已按租户物理隔离，无需 company_id 过滤条件。
    score_threshold: 余弦相似度阈值，低于该值的片段不返回（None 表示不过滤）。
    """
    return _search(question, company_id, limit, score_threshold, with_source=True)


def _cosine(a: list, b: list) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)
