"""
scripts/hermes_worker.py —— Hermes TaskQueue 独立消费 Worker（P0-B3 队列接线）

用法：
    python scripts/hermes_worker.py            # 按 QUEUE_MODE 自动消费
    QUEUE_MODE=rabbitmq python scripts/hermes_worker.py

行为：
- redis / memory 模式：start_worker_loop（2s 轮询）消费 hermes:queue
- rabbitmq 模式：consume_task 阻塞消费（重试 2 次后进死信）
- 任务 handler 由 register_worker 注册；未注册的任务记录日志并返回 no_worker
  （业务任务语义由产品定义后在此注册）

建议：生产由 systemd/supervisor 常驻（README_SYNC 同款服务管理）。
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hermes-worker")


def _default_handler(payload: dict) -> dict:
    """默认 handler：记录任务内容（业务 handler 注册后由具体实现接管）"""
    logger.info("执行任务 payload=%s", str(payload)[:200])
    return {"status": "done", "note": "默认 handler（未注册业务逻辑）"}


def main():
    from hermes.runtime import init_hermes
    from hermes.registry.models import AgentManifest
    from database import SessionLocal

    runtime = init_hermes()
    queue = runtime.queue
    logger.info("Hermes Worker 启动，队列模式=%s", queue._mode)

    # 注册默认 handler（业务任务语义确定后，按 task_name 注册具体实现）
    queue.register_worker("__default__", _default_handler)

    if queue._mode == "rabbitmq":
        logger.info("RabbitMQ 模式：阻塞消费 hermes.tasks（Ctrl+C 退出）")
        try:
            from hermes.queue.rabbitmq import RabbitMQAdapter
            adapter = queue._rabbit or RabbitMQAdapter(url=__import__("config", fromlist=["RABBITMQ_URL"]).RABBITMQ_URL)
            adapter.consume_task(
                task_name="*",
                handler=lambda payload: _default_handler(payload),
            )
        except KeyboardInterrupt:
            logger.info("Worker 退出（Ctrl+C）")
        except Exception as e:
            logger.error("RabbitMQ 消费异常: %s", e)
            sys.exit(1)
    else:
        logger.info("redis/memory 模式：轮询消费中（Ctrl+C 退出）")
        try:
            queue.start_worker_loop(interval=2.0)
            import time
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            logger.info("Worker 退出（Ctrl+C）")


if __name__ == "__main__":
    main()
