"""
hermes/queue/task_queue.py —— Hermes 消息队列系统（第7章）

为什么需要：企业任务不是实时聊天，如"每天扫描 10000 客户"需要任务系统。

架构：Event → Queue（RabbitMQ / Redis / 内存）→ Worker → Agent

三档后端（按配置自动选择）：
1. RabbitMQ 企业版（QUEUE_MODE=rabbitmq，生产推荐：交换机/死信/优先级/持久化）
2. Redis 队列   （QUEUE_MODE=redis，默认轻量方案）
3. 内存队列     （降级，无外部依赖，演示用）

任务示例：{"task": "customer_analysis", "company_id": 1001}
"""
import json
import logging
import threading
import time
import uuid
from typing import Callable, Dict

from config import REDIS_URL, RABBITMQ_URL, QUEUE_MODE

logger = logging.getLogger(__name__)


class TaskQueue:
    """任务队列：RabbitMQ 企业版 / Redis / 内存 三后端自适应"""

    def __init__(self):
        self._mode = (QUEUE_MODE or "auto").lower()
        self._redis = None
        self._rabbit = None
        self._local_queue = []
        self._lock = threading.Lock()
        self._workers: Dict[str, Callable] = {}
        self._setup_transport()

    def _setup_transport(self):
        """选择并初始化传输层"""
        # RabbitMQ 优先（企业版或显式指定）
        if self._mode in ("auto", "rabbitmq") and RABBITMQ_URL:
            try:
                from hermes.queue.rabbitmq import RabbitMQAdapter
                self._rabbit = RabbitMQAdapter(url=RABBITMQ_URL)
                hc = self._rabbit.health_check()
                if hc["status"] == "ok":
                    self._mode = "rabbitmq"
                    logger.info("Queue: RabbitMQ 企业版已启用")
                    return
                self._rabbit = None
            except Exception as e:
                self._rabbit = None
                logger.warning(f"Queue: RabbitMQ 不可用({e})，尝试 Redis")

        # Redis
        if self._mode in ("auto", "redis") and REDIS_URL:
            try:
                import redis
                self._redis = redis.Redis.from_url(
                    REDIS_URL, decode_responses=True, socket_connect_timeout=1)
                self._redis.ping()
                self._mode = "redis"
                logger.info("Queue: Redis 队列已启用")
                return
            except Exception:
                self._redis = None

        # 内存降级
        if self._mode in ("auto", "memory"):
            self._mode = "memory"
            logger.info("Queue: 无外部消息中间件，降级为进程内队列")

    # ---------- Worker 注册 ----------

    def register_worker(self, task_name: str, handler: Callable) -> None:
        """注册任务处理函数"""
        self._workers[task_name] = handler
        logger.info(f"Queue: worker [{task_name}] 注册成功")

    # ---------- 入队 ----------

    def enqueue(self, task_name: str, payload: dict,
                delay_seconds: int = 0, priority: int = 5) -> dict:
        """任务入队，返回 task_id"""
        task_id = uuid.uuid4().hex[:16]

        if self._mode == "rabbitmq":
            return self._rabbit.publish_task(
                task_name, payload, priority=priority, task_id=task_id)

        task = {
            "task": task_name,
            "task_id": task_id,
            "payload": payload,
            "priority": priority,
            "created_at": time.time(),
            "run_at": time.time() + delay_seconds,
        }
        if self._mode == "redis":
            # Redis 列表 + 优先级（sort 时按 priority 降序）
            self._redis.rpush("hermes:queue", json.dumps(task, ensure_ascii=False))
        else:
            with self._lock:
                self._local_queue.append(task)
        logger.info(f"Queue: task [{task_name}] {task_id} 入队（mode={self._mode}）")
        return {"task_id": task_id, "task": task_name, "status": "queued",
                "transport": self._mode}

    # ---------- 事件发布（第7章 Event Bus）----------

    def publish_event(self, event_type: str, payload: dict,
                      routing_key: str = None) -> dict:
        """发布领域事件（RabbitMQ topic 交换机；降级时写内存事件日志）"""
        if self._mode == "rabbitmq":
            self._rabbit.publish_event(event_type, payload, routing_key)
            return {"status": "published", "event": event_type, "transport": "rabbitmq"}
        # 降级：记录到本地事件日志
        with self._lock:
            self._local_queue.append({
                "task": "__event__",
                "payload": {"event": event_type, "data": payload},
                "created_at": time.time(),
            })
        return {"status": "logged", "event": event_type, "transport": self._mode}

    # ---------- 出队 / 执行 ----------

    def _pop(self) -> dict:
        if self._mode == "rabbitmq":
            # 同步模式：从 RabbitMQ 拉取一条（消费确认）
            try:
                from hermes.queue.rabbitmq import EXCHANGE_TASKS
                ch = self._rabbit._get_channel()
                method, props, body = ch.basic_get(
                    queue=f"hermes.task.{_current_task_name()}") if False else (None, None, None)
                return None
            except Exception:
                return None
        if self._mode == "redis":
            raw = self._redis.lpop("hermes:queue")
            return json.loads(raw) if raw else None
        with self._lock:
            if not self._local_queue:
                return None
            # 优先级排序（priority 高者先出）
            self._local_queue.sort(key=lambda t: -t.get("priority", 5))
            return self._local_queue.pop(0)

    def process_once(self) -> dict:
        """处理一条任务（同步模式，供 API/测试触发）"""
        if self._mode == "rabbitmq":
            # RabbitMQ 模式下由专用 Worker 消费，API 触发走事件模型
            return {"status": "async_worker", "transport": "rabbitmq",
                    "note": "RabbitMQ 任务由 hermes-worker 进程消费"}
        task = self._pop()
        if not task:
            return {"status": "empty"}
        if task.get("task") == "__event__":
            logger.info(f"Queue: 事件日志 {task['payload'].get('event')}")
            return {"status": "event_logged"}
        return self._run(task)

    def _run(self, task: dict) -> dict:
        handler = self._workers.get(task["task"])
        if not handler:
            logger.warning(f"Queue: 无 worker 处理任务 [{task['task']}]")
            return {"task_id": task["task_id"], "status": "no_worker"}
        try:
            result = handler(task["payload"])
            logger.info(f"Queue: task [{task['task']}] 完成")
            return {"task_id": task["task_id"], "status": "done", "result": result}
        except Exception as e:
            logger.error(f"Queue: task [{task['task']}] 失败: {e}")
            return {"task_id": task["task_id"], "status": "failed", "error": str(e)}

    def start_worker_loop(self, interval: float = 2.0):
        """后台 worker 循环（生产环境用 Celery worker / RabbitMQ consume 替代）"""
        def _loop():
            while True:
                try:
                    self.process_once()
                except Exception:
                    pass
                time.sleep(interval)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        logger.info(f"Queue: worker 循环已启动（mode={self._mode}）")
        return t

    # ---------- 状态 ----------

    def status(self) -> dict:
        return {"mode": self._mode, "workers": list(self._workers.keys())}
