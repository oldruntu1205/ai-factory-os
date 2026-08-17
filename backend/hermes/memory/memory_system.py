"""
hermes/memory/memory_system.py —— Hermes 三层记忆系统（第9章）

短期记忆  当前对话上下文    Redis（未配置时内存降级）
工作记忆  任务上下文        PostgreSQL（agents 表 memory 字段 / 独立表）
长期记忆  企业知识          Qdrant（由 RAG 系统承载）

记忆结构示例（客户 ABC）：
  Memory: { 历史询盘, 购买产品, 价格敏感, 沟通记录 }
"""
import json
import logging
import threading
import time
from typing import Dict, Optional

from database import SessionLocal
from models import AgentMemory
from config import REDIS_URL

logger = logging.getLogger(__name__)


class MemorySystem:
    """三层记忆统一入口"""

    def __init__(self):
        self._redis = None
        self._short_store: Dict[str, list] = {}   # 内存降级
        self._lock = threading.Lock()
        self._try_redis()

    def _try_redis(self):
        try:
            import redis
            self._redis = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                               socket_connect_timeout=1)
            self._redis.ping()
        except Exception:
            self._redis = None
            logger.info("Memory: Redis 不可用，短期记忆降级为内存模式")

    # ===== 短期记忆（当前对话，Redis）=====

    def save_short_term_memory(self, session_id: str, entry: dict) -> None:
        """写入短期记忆（对话轮次）"""
        if self._redis:
            key = f"hermes:short:{session_id}"
            self._redis.lpush(key, json.dumps(entry, ensure_ascii=False))
            self._redis.ltrim(key, 0, 19)  # 保留最近 20 条
        else:
            with self._lock:
                self._short_store.setdefault(session_id, []).append(entry)
                self._short_store[session_id] = self._short_store[session_id][-20:]

    def load_short_term_memory(self, session_id: str, limit: int = 10) -> list:
        """读取短期记忆（最近对话）"""
        if self._redis:
            raw = self._redis.lrange(f"hermes:short:{session_id}", 0, limit - 1)
            return [json.loads(r) for r in raw]
        with self._lock:
            return list(reversed(self._short_store.get(session_id, [])[-limit:]))

    # ===== 工作记忆（任务上下文，PostgreSQL）=====

    def save_working_memory(self, agent_name: str, company_id: int,
                            context: dict) -> None:
        """写入工作记忆（Agent 任务上下文，按 agent+company 维度）"""
        db = SessionLocal()
        try:
            rec = db.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name,
                AgentMemory.company_id == company_id).first()
            if not rec:
                rec = AgentMemory(agent_name=agent_name, company_id=company_id)
                db.add(rec)
            rec.context = json.dumps(context, ensure_ascii=False)
            rec.updated_at = time.time()
            db.commit()
        finally:
            db.close()

    def load_working_memory(self, agent_name: str, company_id: int) -> Optional[dict]:
        """读取工作记忆"""
        db = SessionLocal()
        try:
            rec = db.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name,
                AgentMemory.company_id == company_id).first()
            if rec and rec.context:
                return json.loads(rec.context)
            return None
        finally:
            db.close()

    # ===== 长期记忆（企业知识，Qdrant，委托 RAG）=====

    def search_long_term_memory(self, question: str, company_id: int, limit: int = 5) -> list:
        """长期记忆 = 企业知识库检索"""
        from knowledge.rag import search
        return search(question, company_id, limit=limit)
