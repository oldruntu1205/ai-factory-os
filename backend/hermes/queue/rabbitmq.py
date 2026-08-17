"""
hermes/queue/rabbitmq.py —— RabbitMQ 企业版消息队列（第7章）

企业级消息中间件，相比 Redis 队列提供：
- 交换机（Exchange）路由模型：direct / topic / fanout
- 持久化消息与队列（重启不丢）
- 死信队列（DLX：任务失败/超时的消息转入死信）
- 优先级队列
- 消费确认（ack/nack）

依赖：pika（pip install pika）
"""
import json
import logging
import os
import threading
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Hermes 消息路由约定
EXCHANGE_TASKS = "hermes.tasks"          # 任务交换机（direct）
EXCHANGE_EVENTS = "hermes.events"        # 事件总线（topic）
EXCHANGE_DEAD = "hermes.dead"            # 死信交换机

QUEUE_PREFIX = "hermes"
ROUTING_ALL = "task.*"                   # topic 通配


class RabbitMQAdapter:
    """
    RabbitMQ 适配器：发布 / 消费 / 死信 / 优先级。

    使用示例：
        mq = RabbitMQAdapter(url="amqp://guest:guest@localhost:5672/")
        mq.publish_task("customer_analysis", {"company_id": 1001}, priority=5)
        mq.consume_task("customer_analysis", handler, prefetch=10)
    """

    def __init__(self, url: str = "amqp://guest:guest@localhost:5672/",
                 exchange_tasks: str = EXCHANGE_TASKS,
                 exchange_events: str = EXCHANGE_EVENTS):
        self.url = url
        self.exchange_tasks = exchange_tasks
        self.exchange_events = exchange_events
        self._connection = None
        self._channel = None
        self._lock = threading.Lock()

    # ---------- 连接管理 ----------

    def _get_channel(self):
        """懒连接：获取 channel（自动声明交换机与死信队列）"""
        with self._lock:
            if self._channel is not None and self._channel.is_open:
                return self._channel
            import pika
            # 可靠性加固：显式连接超时/重试（此前无参数，broker 不可达时 BlockingConnection 可挂死）
            params = pika.URLParameters(self.url)
            params.connection_attempts = int(os.getenv("RABBITMQ_CONNECTION_ATTEMPTS", "3"))
            params.retry_delay = int(os.getenv("RABBITMQ_RETRY_DELAY", "2"))
            params.socket_timeout = int(os.getenv("RABBITMQ_SOCKET_TIMEOUT", "5"))
            self._connection = pika.BlockingConnection(params)
            ch = self._connection.channel()
            ch.confirm_delivery()   # 发布确认（可靠投递）

            # 主交换机（direct：按 routing_key 精确路由任务）
            ch.exchange_declare(exchange=self.exchange_tasks,
                                exchange_type="direct", durable=True)
            # 事件交换机（topic：多级路由）
            ch.exchange_declare(exchange=self.exchange_events,
                                exchange_type="topic", durable=True)
            # 死信交换机
            ch.exchange_declare(exchange=EXCHANGE_DEAD,
                                exchange_type="direct", durable=True)

            self._channel = ch
            logger.info("RabbitMQ: 连接建立，交换机就绪")
            return ch

    def close(self):
        with self._lock:
            if self._connection:
                try:
                    self._connection.close()
                except Exception:
                    pass
            self._connection = None
            self._channel = None

    # ---------- 任务队列（direct + 死信 + 优先级）----------

    def declare_task_queue(self, task_name: str, priority: bool = True,
                           max_retry: int = 3) -> str:
        """
        声明任务队列：
        - 绑定到任务交换机（routing_key = task_name）
        - 绑定死信交换机（任务失败 N 次后转入 hermes.dead）
        - 支持优先级（x-max-priority=10）
        """
        ch = self._get_channel()
        queue = f"{QUEUE_PREFIX}.task.{task_name}"
        args = {"x-dead-letter-exchange": EXCHANGE_DEAD,
                "x-dead-letter-routing-key": f"dead.{task_name}",
                "x-message-ttl": 7 * 24 * 3600 * 1000}   # 7 天 TTL
        if priority:
            args["x-max-priority"] = 10
        ch.queue_declare(queue=queue, durable=True, arguments=args)
        ch.queue_bind(exchange=self.exchange_tasks, queue=queue,
                      routing_key=task_name)

        # 死信队列
        dead_queue = f"{QUEUE_PREFIX}.dead.{task_name}"
        ch.queue_declare(queue=dead_queue, durable=True)
        ch.queue_bind(exchange=EXCHANGE_DEAD, queue=dead_queue,
                      routing_key=f"dead.{task_name}")
        return queue

    def publish_task(self, task_name: str, payload: dict,
                     priority: int = 5, retry_count: int = 0,
                     task_id: str = None) -> dict:
        """发布任务（direct 路由，支持优先级与重试计数）"""
        import uuid
        ch = self._get_channel()
        self.declare_task_queue(task_name)
        message = {
            "task": task_name,
            "task_id": task_id or uuid.uuid4().hex[:16],
            "payload": payload,
            "priority": priority,
            "retry_count": retry_count,
            "created_at": __import__("time").time(),
        }
        props = {
            "delivery_mode": 2,            # 持久化
            "priority": priority,
            "content_type": "application/json",
            "headers": {"retry_count": retry_count},
        }
        ch.basic_publish(
            exchange=self.exchange_tasks,
            routing_key=task_name,
            body=json.dumps(message, ensure_ascii=False).encode(),
            properties=__import__("pika").BasicProperties(**props),
        )
        logger.info(f"RabbitMQ: 任务 [{task_name}] {message['task_id']} 发布（priority={priority}）")
        return {"task_id": message["task_id"], "task": task_name, "status": "queued",
                "transport": "rabbitmq"}

    def consume_task(self, task_name: str, handler: Callable,
                     prefetch: int = 10, auto_ack: bool = False):
        """消费任务队列（阻塞式，用于 Worker 进程）"""
        ch = self._get_channel()
        queue = self.declare_task_queue(task_name)
        ch.basic_qos(prefetch_count=prefetch)

        def _callback(ch_, method, properties, body):
            try:
                message = json.loads(body.decode())
                result = handler(message.get("payload", {}))
                ch_.basic_ack(delivery_tag=method.delivery_tag)
                logger.info(f"RabbitMQ: 任务 [{task_name}] 完成 -> {str(result)[:80]}")
            except Exception as e:
                retry = int((properties.headers or {}).get("retry_count", 0))
                if retry < 2:   # 重试 2 次后进死信
                    ch_.basic_nack(delivery_tag=method.delivery_tag,
                                   requeue=False)
                    # 重新发布（retry+1）
                    message = json.loads(body.decode())
                    self.publish_task(task_name, message.get("payload", {}),
                                      priority=message.get("priority", 5),
                                      retry_count=retry + 1)
                else:
                    ch_.basic_nack(delivery_tag=method.delivery_tag,
                                   requeue=False)
                logger.error(f"RabbitMQ: 任务 [{task_name}] 失败(重试{retry}): {e}")

        ch.basic_consume(queue=queue, on_message_callback=_callback,
                         auto_ack=auto_ack)
        logger.info(f"RabbitMQ: Worker 开始消费队列 [{queue}]（prefetch={prefetch}）")
        ch.start_consuming()

    # ---------- 事件总线（topic 多级路由）----------

    def publish_event(self, event_type: str, payload: dict,
                      routing_key: str = None) -> None:
        """发布事件（topic 交换机）"""
        ch = self._get_channel()
        key = routing_key or f"event.{event_type}"
        message = {"event": event_type, "payload": payload,
                   "ts": __import__("time").time()}
        ch.basic_publish(
            exchange=self.exchange_events,
            routing_key=key,
            body=json.dumps(message, ensure_ascii=False).encode(),
            properties=__import__("pika").BasicProperties(delivery_mode=2),
        )
        logger.info(f"RabbitMQ: 事件 [{event_type}] -> {key}")

    def subscribe_events(self, routing_key: str = ROUTING_ALL,
                         handler: Callable = None, queue_name: str = None):
        """订阅事件（topic 通配）"""
        ch = self._get_channel()
        queue = queue_name or f"{QUEUE_PREFIX}.events.{routing_key.replace('.', '_')}"
        ch.queue_declare(queue=queue, durable=True)
        ch.queue_bind(exchange=self.exchange_events, queue=queue,
                      routing_key=routing_key)

        def _callback(ch_, method, properties, body):
            try:
                message = json.loads(body.decode())
                if handler:
                    handler(message)
                ch_.basic_ack(delivery_tag=method.delivery_tag)
            except Exception as e:
                ch_.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                logger.error(f"RabbitMQ: 事件消费失败: {e}")

        ch.basic_consume(queue=queue, on_message_callback=_callback)
        logger.info(f"RabbitMQ: 订阅事件 [{routing_key}] -> {queue}")
        ch.start_consuming()

    # ---------- 管理 ----------

    def health_check(self) -> dict:
        """连通性检查"""
        try:
            ch = self._get_channel()
            return {"status": "ok", "transport": "rabbitmq",
                    "exchange_tasks": self.exchange_tasks,
                    "exchange_events": self.exchange_events}
        except Exception as e:
            return {"status": "error", "transport": "rabbitmq", "error": str(e)}
