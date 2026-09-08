"""
knowledge/vector.py —— 向量数据库（第十一章，多租户物理隔离版）

隔离模型（对齐 Nous Hermes Profile 的物理沙盒思想）：
  旧：单一共享集合 company_docs，靠 payload.company_id 逻辑过滤隔离。
  新：每租户独立集合 company_docs_{company_id}，A 租户向量在存储层即与 B 隔离，
      查询不再依赖 company_id 过滤条件，根除逻辑越权风险。

优先连接 Qdrant（生产推荐）；未启动时降级为进程内内存向量库（仅演示用，非安全隔离）。
"""
import threading
import time
import uuid
from typing import Optional

from config import (QDRANT_HOST, QDRANT_PORT, QDRANT_COLLECTION,
                    QDRANT_FALLBACK_MEMORY)

# QDRANT_COLLECTION 现作为每租户集合名前缀（向后兼容同名环境变量）
COLLECTION_PREFIX = QDRANT_COLLECTION

_client = None
_use_memory = False
_memory_lock = threading.Lock()
# 内存向量库：{(company_id): [ {id, vector, text, document_id, source} ]}
_memory_store = {}
# 安全修复：点 ID 改 UUID——旧实现用进程内自增计数器，进程重启复位后
# 新上传向量点 ID 1..N 会 upsert 覆盖 Qdrant 集合里历史最早向量（静默索引损坏）。
_memory_since = 0.0          # 进入降级模式的时刻（用于周期性重连探测）
_RETRY_INTERVAL = 30.0       # 降级后每 30s 探测一次 Qdrant 是否恢复

# ensure_collection 已建集合缓存（进程内，避免重复探测维度）
_ensured_lock = threading.Lock()
_ensured = set()


def _validate_company(company_id) -> int:
    """校验 company_id 合法（防止凭空建空集合 / 注入）"""
    try:
        cid = int(company_id)
    except (TypeError, ValueError):
        raise ValueError(f"非法 company_id: {company_id!r}")
    if cid <= 0:
        raise ValueError(f"非法 company_id（必须为正整数）: {cid}")
    return cid


def collection_for(company_id: int) -> str:
    """每租户独立集合名：company_docs_123（company_id 经 int 强转，无注入风险）"""
    return f"{COLLECTION_PREFIX}_{_validate_company(company_id)}"


def ensure_collection(client, name: str) -> None:
    """幂等建集合：区分『不存在』与真实错误；并发建集合视为成功。"""
    if name in _ensured:
        return
    with _ensured_lock:
        if name in _ensured:
            return
        # 1) 已存在则直接标记
        try:
            client.get_collection(name)
            _ensured.add(name)
            return
        except Exception as e:
            status = getattr(e, "status_code", None)
            msg = str(e).lower()
            # 仅『不存在』才继续创建；超时 / 鉴权失败等真实错误直接抛出
            if status != 404 and "not found" not in msg and "does not exist" not in msg:
                raise
        # 2) 创建（并发场景下另一进程可能已建好 -> already exists 视为成功）
        try:
            dim = _probe_dim()
            from qdrant_client.http import models as qmodels
            client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
            )
        except Exception as e:
            status = getattr(e, "status_code", None)
            msg = str(e).lower()
            if status == 409 or "already exist" in msg:
                pass
            else:
                raise
        _ensured.add(name)


def _get_client():
    """返回 Qdrant 客户端；不可用且允许降级时切内存模式。

    注意：QdrantClient 构造是惰性的（不真正连接），必须用一次实际请求做连通性探针，
    否则连接被拒时不会触发降级，导致后续 upsert 直接抛连接错误。

    安全修复：降级后每 _RETRY_INTERVAL 秒探测一次连接，恢复后自动切回 Qdrant
    （不再『一次降级永久内存』导致向量只进内存、重启即丢且无告警）。
    """
    global _client, _use_memory, _memory_since
    if _use_memory:
        # 降级模式：周期性重连探测（避免每次调用都尝试连接）
        if time.time() - _memory_since < _RETRY_INTERVAL:
            return _client
        try:
            from qdrant_client import QdrantClient
            c = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
            c.get_collections()
            _client = c
            _use_memory = False
            _memory_since = 0.0
            import logging
            logging.getLogger(__name__).info(
                "Qdrant 已恢复连接，向量写入切回 Qdrant（降级期间内存中的向量请重新上传）")
        except Exception:
            _memory_since = time.time()
        return _client
    if _client is not None:
        return _client
    try:
        from qdrant_client import QdrantClient
        c = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        c.get_collections()  # 连通性探针（无副作用，不建任何集合）
        _client = c
    except Exception:
        if not QDRANT_FALLBACK_MEMORY:
            # 生产配置：不降级 → 显式报错（上传方收到失败，不静默吞数据）
            raise
        _use_memory = True
        _memory_since = time.time()
        _client = None
        import logging
        logging.getLogger(__name__).warning(
            "Qdrant 不可用，已降级内存向量库（仅演示；生产请设 QDRANT_FALLBACK_MEMORY=0）")
    return _client


_dim_probe_cache = None
_dim_probe_lock = threading.Lock()


def _probe_dim() -> int:
    """探测向量维度：用一个小文本生成向量（结果缓存，避免反复触发 embedding 加载）。"""
    global _dim_probe_cache
    if _dim_probe_cache:
        return _dim_probe_cache
    with _dim_probe_lock:
        if _dim_probe_cache:
            return _dim_probe_cache
        from knowledge.embedding import create_embedding, get_embedding_dim
        try:
            d = len(create_embedding(["dim"])[0])
        except Exception:
            # 兜底用配置维度（与降级哈希向量保持一致）
            d = get_embedding_dim() or 512
        _dim_probe_cache = d
        return d


def _new_point_id():
    """生成稳定唯一的向量点 ID（UUID）——修复：进程内自增计数器在重启复位后
    upsert 会覆盖 Qdrant 集合中历史最早的向量点（确定性、静默的索引损坏）。"""
    return str(uuid.uuid4())


def save_vectors(vectors: list, texts: list, company_id: int,
                 document_id: Optional[int] = None, source_file: str = "") -> int:
    """保存向量到该租户独立集合（含 company_id 元数据，不再用于过滤）"""
    cid = _validate_company(company_id)
    if vectors:
        # 维度一致性校验：防止「集合 512 维、写入其他维度」导致的静默崩溃 / 检索混乱
        d0 = len(vectors[0])
        expected = _probe_dim()
        if d0 != expected:
            raise ValueError(
                f"向量维度不一致：当前 {d0}，模型/集合 {expected}。"
                f"请检查 EMBEDDING_DIM 与 EMBEDDING_MODEL 是否匹配，或删除旧集合后重建。")
    c = _get_client()
    if not _use_memory and c is not None:
        from qdrant_client.http import models as qmodels
        name = collection_for(cid)
        ensure_collection(c, name)
        points = []
        for i, v in enumerate(vectors):
            payload = {
                "company_id": cid,
                "document_id": document_id,
                "source": source_file,
                "text": texts[i],
            }
            points.append(qmodels.PointStruct(
                id=_new_point_id(), vector=v, payload=payload))
        c.upsert(collection_name=name, points=points)
    else:
        with _memory_lock:
            store = _memory_store.setdefault(cid, [])
            for i, v in enumerate(vectors):
                store.append({
                    "id": _new_point_id(),
                    "vector": v,
                    "text": texts[i],
                    "document_id": document_id,
                    "source": source_file,
                })
    return len(vectors)


def delete_document_vectors(company_id: int, document_id: int) -> None:
    """删除单个文档的全部向量（修复『删文档不删向量』的已存在漏洞）。"""
    cid = _validate_company(company_id)
    c = _get_client()
    name = collection_for(cid)
    if not _use_memory and c is not None:
        from qdrant_client.http import models as qmodels
        try:
            c.delete(collection_name=name,
                     points_selector=qmodels.FilterSelector(
                         filter=qmodels.Filter(must=[
                             qmodels.FieldCondition(
                                 key="document_id",
                                 match=qmodels.MatchValue(value=document_id)),
                         ])))
        except Exception as e:
            # 集合不存在（该企业无知识库）时忽略
            if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
                return
            raise
    else:
        with _memory_lock:
            store = _memory_store.get(cid)
            if store is not None:
                _memory_store[cid] = [x for x in store if x.get("document_id") != document_id]


def delete_company_collection(company_id: int) -> None:
    """删除该企业整个向量集合（清空 / 注销用）。真实失败会抛出，避免『已清空却仍召回』。"""
    cid = _validate_company(company_id)
    c = _get_client()
    name = collection_for(cid)
    if not _use_memory and c is not None:
        try:
            c.delete_collection(name)
            _ensured.discard(name)
        except Exception as e:
            if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
                return
            raise
    else:
        with _memory_lock:
            _memory_store.pop(cid, None)
