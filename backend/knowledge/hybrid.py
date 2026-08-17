"""
knowledge/hybrid.py —— 混合检索（Hybrid Search，工艺/图纸助手检索核心）

为什么需要 Hybrid：
- 公差(±0.05)、材质(6061-T6)、标准(GB/T 1804) 是「精确规格 token」，
  稠密向量（语义）检索对这些数值/代号很差，必须靠稀疏/关键词命中。
- 本模块 = 稠密向量（复用 knowledge.rag）+ 轻量 BM25（无外部依赖） + RRF 倒数排名融合。

实现说明（与平台「降级优雅」哲学一致）：
- BM25 索引 = 进程内 _bm25_store（检索快） + bm25_chunks 持久化表（跨重启/跨进程）。
  index_chunks 同时写内存与 SQL；remove_document / clear_bm25 同步回收两者，
  从根上解决「已删文档仍被稀疏召回」与「进程重启后稀疏索引丢失 → 退化为纯稠密」。
  启动时 main.py 调用 rebuild_bm25_index() 从 SQL 表重建内存索引。
- 多进程一致性（关键修复）：每个 _BM25 维护 synced_max_id 水位线，
  _ensure_bm25 每次检索前增量拉取「本进程索引之后新增」的 SQL 行，
  使其他 worker 上传的文档也能在本进程的稀疏检索中召回。
- 落库补偿：index_chunks 落库失败时登记 _unsynced 补写队列，下次检索前幂等重写，
  避免「向量已入库、BM25 永缺该文档」。
- 混合检索同时提供「结构化参数快路径」：若查询命中 DrawingParam 表（按产品型号/参数名），
  直接返回精确行，再补 RAG 上下文，从根本上解决「问某型号公差」的精确度问题。
"""
import math
import os
import re
import threading
import logging
from collections import defaultdict

from sqlalchemy import or_

from knowledge.rag import search_with_source as _dense_search

logger = logging.getLogger(__name__)

# 统一分词（小写；按非「字母/数字/汉字」切；保留数字与汉字）
_TOKEN_RE = re.compile(r"[^a-zA-Z0-9\u4e00-\u9fa5]+")

# 稠密召回的默认余弦阈值：低于该值的片段在 RRF 融合前即被过滤，
# 避免「语义几乎无关」的低分片段混入 top-k。显式传 -1.0 可关闭过滤。
DENSE_SCORE_THRESHOLD = float(os.getenv("DENSE_SCORE_THRESHOLD", "0.3"))

# 轻量词汇重排权重（原 0.02 过弱，精确匹配难以浮到前面；提升后覆盖更多 query 词的片段排序更靠前）
SPARSE_OVERLAP_WEIGHT = float(os.getenv("SPARSE_OVERLAP_WEIGHT", "0.1"))

# 结构化参数检索停用词（去除「的/是」等无信息量 token，降低 LIKE 召回噪声）
_SPARSE_STOPWORDS = frozenset({
    "的", "是", "了", "在", "和", "与", "为", "吗", "呢", "啊", "哦",
    "什么", "如何", "怎么", "为什么", "请问", "多少", "哪些", "哪个", "是否", "可以",
    "the", "a", "an", "of", "to", "in", "for", "is", "are", "and", "or", "on",
})


def _tokenize(text: str) -> list:
    return [t for t in _TOKEN_RE.split((text or "").lower()) if t]


def _session():
    from database import SessionLocal
    return SessionLocal()


# ---------- 轻量 BM25（Okapi，无外部依赖） ----------

class _BM25:
    """轻量 BM25（Okapi）+ 倒排索引（性能优化：消除 O(N) 全量扫描与每查询全量 idf 重算）。

    - 倒排表 postings: token -> [doc_idx]，查询只遍历包含 query token 的文档
    - doc_len 缓存每文档长度，避免每次检索重新 len
    - idf/avgdl 惰性重算（_dirty 标记，批量 add 后仅首次查询重算一次）
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = []          # list of token lists
        self.doc_meta = []      # list of {text, source}
        self.doc_did = []       # 与 docs 平行的 document_id（用于按文档 prune）
        self.doc_len = []       # 每文档 token 数（缓存，避免检索时重复 len）
        self.doc_freq = []      # 每文档 token->词频 dict（预计算，检索 O(1) 查频）
        self.postings = defaultdict(list)  # token -> [doc_idx]
        self.df = defaultdict(int)
        self.idf = {}
        self.avgdl = 0.0
        self.N = 0
        self._dirty = True      # add/remove 后置 True，search 前惰性重算 idf/avgdl
        self.synced_max_id = 0  # 已持久化到 bm25_chunks 的最大行 id（跨进程增量同步水位线）

    @staticmethod
    def _tokenize(text: str) -> list:
        return _tokenize(text)

    def add(self, text: str, meta: dict, document_id=None):
        tokens = self._tokenize(text)
        idx = len(self.docs)
        self.docs.append(tokens)
        self.doc_meta.append(meta)
        self.doc_did.append(document_id)
        self.doc_len.append(len(tokens))
        freq = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1
        self.doc_freq.append(freq)
        for t in freq:
            self.postings[t].append(idx)
            self.df[t] += 1
        self.N += 1
        self._dirty = True

    def remove_document(self, document_id):
        """按 document_id 精确剔除该文档全部切片，并重建倒排表（低频操作可接受重建）。"""
        if document_id is None:
            return
        keep = [i for i in range(self.N) if self.doc_did[i] != document_id]
        self.docs = [self.docs[i] for i in keep]
        self.doc_meta = [self.doc_meta[i] for i in keep]
        self.doc_did = [self.doc_did[i] for i in keep]
        self.doc_len = [self.doc_len[i] for i in keep]
        self.doc_freq = [self.doc_freq[i] for i in keep]
        self.postings = defaultdict(list)
        self.df = defaultdict(int)
        for i, freq in enumerate(self.doc_freq):
            for t in freq:
                self.postings[t].append(i)
                self.df[t] += 1
        self.N = len(self.docs)
        self.idf = {}
        self._dirty = True

    def _ensure_stats(self):
        """惰性重算 idf/avgdl（仅在索引变更后执行一次）"""
        if not self._dirty:
            return
        if self.N:
            self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
                        for t, n in self.df.items()}
            self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        else:
            self.idf = {}
            self.avgdl = 0.0
        self._dirty = False

    def search(self, query: str, top_k: int = 10):
        if self.N == 0:
            return []
        self._ensure_stats()
        q_tokens = self._tokenize(query)
        # 倒排检索：候选文档 = 含任一 query token 的文档（postings 并集去重），
        # 词频用预计算 doc_freq O(1) 查询——远快于全量扫描 O(N)
        candidates = set()
        for qt in q_tokens:
            if qt in self.idf:
                candidates.update(self.postings.get(qt, ()))
        scores = {}
        for i in candidates:
            freq = self.doc_freq[i]
            dl = self.doc_len[i]
            score = 0.0
            for qt in q_tokens:
                idf = self.idf.get(qt)
                if not idf:
                    continue
                f = freq.get(qt, 0)
                if f == 0:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * (dl / (self.avgdl or 1)))
                score += idf * (f * (self.k1 + 1) / denom)
            if score > 0:
                scores[i] = score
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        out = []
        for i, score in ranked[:top_k]:
            meta = dict(self.doc_meta[i])
            meta["sparse_score"] = round(score, 4)
            out.append(meta)
        return out


# 内存 BM25 索引：{company_id: _BM25}
# RLock：FastAPI 线程池并发下读写互斥，防止 index/remove/search 数据竞争
_bm25_store: dict = {}
_bm25_lock = threading.RLock()

# 落库失败待补写的切片：{(company_id, document_id): {"texts": [...], "source": str}}
# 每次检索前幂等补写，从根上避免「向量已入库、BM25 永缺该文档」。
_unsynced: dict = {}


def index_chunks(texts: list, company_id: int, source_file: str = "", document_id=None) -> bool:
    """入库时建立/追加 BM25 索引：写内存（检索快）+ 写 bm25_chunks 表（跨重启/跨进程）。

    返回是否落库成功。落库失败不抛出，改为登记到 _unsynced 补写队列，
    下次检索前自动幂等重试（跨进程上传也能在任意 worker 补齐）。
    """
    cleaned = [t for t in (texts or []) if t and t.strip()]
    if not cleaned:
        return True
    with _bm25_lock:
        idx = _bm25_store.setdefault(company_id, _BM25())
        for t in cleaned:
            idx.add(t, {"text": t, "source": source_file}, document_id)
    from models import Bm25Chunk
    db = _session()
    try:
        rows = [Bm25Chunk(company_id=company_id, document_id=document_id,
                          text=t, source=source_file) for t in cleaned]
        db.bulk_save_objects(rows)
        db.flush()
        max_id = max((r.id or 0) for r in rows)
        db.commit()
        with _bm25_lock:
            idx.synced_max_id = max(idx.synced_max_id, max_id)
            _unsynced.pop((company_id, document_id), None)
        return True
    except Exception as e:
        db.rollback()
        logger.error("BM25 落库失败 company=%s doc=%s: %s（已登记自动补写）",
                     company_id, document_id, e)
        with _bm25_lock:
            _unsynced[(company_id, document_id)] = {"texts": cleaned, "source": source_file}
        return False
    finally:
        db.close()


def remove_document(company_id: int, document_id):
    """删除单个文档的 BM25 切片（内存 + SQL 同步，与 delete_document_vectors 对齐）。"""
    with _bm25_lock:
        idx = _bm25_store.get(company_id)
        if idx is not None:
            idx.remove_document(document_id)
        # 若该文档尚在补写队列（上次落库失败），一并移除，防止僵尸补写
        _unsynced.pop((company_id, document_id), None)
    from models import Bm25Chunk
    db = _session()
    try:
        db.query(Bm25Chunk).filter(
            Bm25Chunk.company_id == company_id,
            Bm25Chunk.document_id == document_id,
        ).delete()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("BM25 删除落库失败 company=%s doc=%s: %s", company_id, document_id, e)
    finally:
        db.close()


def clear_bm25(company_id: int):
    """清空企业全部 BM25 索引（内存 + SQL 同步，与 delete_company_collection 对齐）。"""
    with _bm25_lock:
        _bm25_store.pop(company_id, None)
        for k in [k for k in _unsynced if k[0] == company_id]:
            _unsynced.pop(k, None)
    from models import Bm25Chunk
    db = _session()
    try:
        db.query(Bm25Chunk).filter(Bm25Chunk.company_id == company_id).delete()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("BM25 清空落库失败 company=%s: %s", company_id, e)
    finally:
        db.close()


def rebuild_bm25_index():
    """启动重建：从 bm25_chunks 持久化表载入全部租户 BM25 索引（main.py 启动时调用）。"""
    from sqlalchemy import func
    from models import Bm25Chunk
    db = _session()
    try:
        rows = db.query(Bm25Chunk).all()
        max_by_company = dict(db.query(Bm25Chunk.company_id,
                                       func.max(Bm25Chunk.id))
                              .group_by(Bm25Chunk.company_id).all())
    finally:
        db.close()
    with _bm25_lock:
        _bm25_store.clear()
        by_company = {}
        for r in rows:
            idx = by_company.setdefault(r.company_id, _BM25())
            idx.add(r.text, {"text": r.text, "source": r.source or ""}, r.document_id)
        for cid, idx in by_company.items():
            idx.synced_max_id = max_by_company.get(cid, 0)
        _bm25_store.update(by_company)
    logger.info("BM25 启动重建完成 租户数=%d 总条数=%d", len(by_company), len(rows))
    return len(rows)


def _load_company_from_db(company_id: int):
    """懒加载单租户 BM25（用于内存未命中时的兜底，兼容启动重建未覆盖的场景）。"""
    from models import Bm25Chunk
    db = _session()
    try:
        rows = db.query(Bm25Chunk).filter(Bm25Chunk.company_id == company_id).all()
    finally:
        db.close()
    if not rows:
        return None
    idx = _BM25()
    for r in rows:
        idx.add(r.text, {"text": r.text, "source": r.source or ""}, r.document_id)
    idx.synced_max_id = max((r.id or 0) for r in rows)
    _bm25_store[company_id] = idx
    return idx


def _flush_unsynced():
    """幂等补写落库失败的 BM25 切片（以 _unsynced 队列为准，SQL 已存在的跳过）。"""
    with _bm25_lock:
        pending = list(_unsynced.items())
    if not pending:
        return
    from models import Bm25Chunk
    db = _session()
    try:
        for (company_id, document_id), item in pending:
            if document_id is not None:
                exists = db.query(Bm25Chunk.id).filter(
                    Bm25Chunk.company_id == company_id,
                    Bm25Chunk.document_id == document_id).first()
                if exists:
                    with _bm25_lock:
                        _unsynced.pop((company_id, document_id), None)
                    continue
            rows = [Bm25Chunk(company_id=company_id, document_id=document_id,
                              text=t, source=item["source"]) for t in item["texts"]]
            db.bulk_save_objects(rows)
        db.commit()
        with _bm25_lock:
            _unsynced.clear()
    except Exception as e:
        db.rollback()
        logger.warning("BM25 补写失败，将稍后重试: %s", e)
    finally:
        db.close()


def _ensure_bm25(company_id: int):
    """保证返回该租户可用的 BM25 索引，并做跨进程增量同步。

    - 内存缺失（首次/重启后）→ 从 SQL 全量加载；
    - 内存已有 → 增量拉取 id > synced_max_id 的新行，
      覆盖「其他 worker 新上传文档、本进程索引仍陈旧」的跨进程不一致问题。
    """
    with _bm25_lock:
        _flush_unsynced()
        idx = _bm25_store.get(company_id)
        if idx is None:
            return _load_company_from_db(company_id)
        if idx.N == 0:
            # 空索引：DB 可能有历史数据（如未重建的场景），尝试加载
            loaded = _load_company_from_db(company_id)
            if loaded is not None:
                return loaded
        # 增量同步（跨进程一致性兜底）
        from models import Bm25Chunk
        db = _session()
        try:
            new_rows = db.query(Bm25Chunk).filter(
                Bm25Chunk.company_id == company_id,
                Bm25Chunk.id > idx.synced_max_id).all()
        finally:
            db.close()
        if new_rows:
            for r in new_rows:
                idx.add(r.text, {"text": r.text, "source": r.source or ""}, r.document_id)
            idx.synced_max_id = max(idx.synced_max_id, max((r.id or 0) for r in new_rows))
        return idx


def _rrf(rank: int, k: int = 60) -> float:
    """Reciprocal Rank Fusion 分数"""
    return 1.0 / (k + rank)


def hybrid_search(query: str, company_id: int, limit: int = 5,
                  score_threshold: float = None) -> list:
    """混合检索：稠密向量(语义) + BM25(关键词) -> RRF 融合 -> 词汇重排。

    返回 [ {text, source, dense_rank, sparse_rank, score} ]，按融合分降序。
    score_threshold: 稠密召回的余弦阈值（低于该值不参与融合；默认 DENSE_SCORE_THRESHOLD，
    传 -1.0 可关闭过滤）。稀疏精确命中不受阈值影响。
    """
    # 1) 稠密（语义）—— 先按阈值过滤低相关结果，失败则降级为空（保证稀疏检索仍可独立工作）
    dense_thr = score_threshold if score_threshold is not None else DENSE_SCORE_THRESHOLD
    try:
        dense = _dense_search(query, company_id, limit=limit * 2,
                              score_threshold=dense_thr)
    except Exception as e:
        logger.warning("稠密检索失败，已降级为仅稀疏检索: %s", e)
        dense = []
    # 2) 稀疏（关键词）—— 内存优先，自动增量同步跨进程新增
    idx = _ensure_bm25(company_id)
    with _bm25_lock:
        sparse = idx.search(query, top_k=limit * 2) if idx else []

    fused = {}
    for rank, r in enumerate(dense):
        key = r.get("text", "")
        entry = fused.setdefault(key, {"text": key, "source": r.get("source", ""),
                                       "dense_rank": rank, "sparse_rank": None, "score": 0.0})
        entry["score"] += _rrf(rank)
        entry["dense_rank"] = rank
    for rank, r in enumerate(sparse):
        key = r["text"]
        entry = fused.setdefault(key, {"text": key, "source": r.get("source", ""),
                                       "dense_rank": None, "sparse_rank": rank, "score": 0.0})
        entry["score"] += _rrf(rank)
        entry["sparse_rank"] = rank
        if r.get("source"):
            entry["source"] = r["source"]

    # 3) 轻量词汇重排：命中 query 词越多排序越靠前；query 全 token 覆盖的片段额外奖励
    q_tokens = set(_tokenize(query))
    for r in fused.values():
        if not q_tokens:
            break
        rt = set(_tokenize(r["text"]))
        overlap = len(q_tokens & rt)
        r["score"] += SPARSE_OVERLAP_WEIGHT * overlap
        if q_tokens <= rt:
            r["score"] += 0.5

    results = sorted(fused.values(), key=lambda x: -x["score"])
    for i, r in enumerate(results):
        r["rank"] = i
    return results[:limit]


def search_drawing_params(query: str, company_id: int, db, limit: int = 20) -> list:
    """结构化参数快路径：在 DrawingParam 表中按型号/参数名/值分词模糊匹配。

    返回 DrawingParam 行（dict），用于「问某型号公差/材质」的精确答复。
    - 过滤无信息量停用词（的/是/how 等），降低 LIKE 召回噪声；
    - 宽查后按命中字段加权排序（型号 > 参数名/值 > raw），替代原先 id DESC 的「新文档优先」，
      让精确匹配的历史参数也能浮到最前。
    """
    from models import DrawingParam
    q = query.strip()
    if not q:
        return []
    tokens = [t for t in re.split(r"[^a-zA-Z0-9\u4e00-\u9fa5]+", q.lower())
              if t and t not in _SPARSE_STOPWORDS]
    if not tokens:
        return []
    conditions = []
    for t in tokens:
        like = f"%{t}%"
        conditions.append(DrawingParam.product_model.ilike(like))
        conditions.append(DrawingParam.value.ilike(like))
        conditions.append(DrawingParam.param_name.ilike(like))
        conditions.append(DrawingParam.raw.ilike(like))
    rows = db.query(DrawingParam).filter(
        DrawingParam.company_id == company_id,
        or_(*conditions)
    ).limit(limit * 5).all()

    def _hit_score(r) -> float:
        s = 0.0
        for t in tokens:
            if t in (r.product_model or "").lower():
                s += 3.0
            if t in (r.param_name or "").lower():
                s += 2.0
            if t in (r.value or "").lower():
                s += 2.0
            if t in (r.raw or "").lower():
                s += 1.0
        return s

    scored = sorted(((_hit_score(r), r) for r in rows), key=lambda x: -x[0])
    out = []
    for s, r in scored:
        if s <= 0:
            break
        out.append({
            "id": r.id,
            "product_model": r.product_model,
            "category": r.category,
            "param_name": r.param_name,
            "value": r.value,
            "raw": r.raw,
            "source_file": r.source_file,
            "location": r.location,
        })
        if len(out) >= limit:
            break
    return out
