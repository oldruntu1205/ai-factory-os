"""
run_wecom_bot.py —— 企业微信智能机器人长连接守护进程（独立进程）

启动方式（建议用 systemd 守护）：
    python run_wecom_bot.py

- 扫描所有配置了 wecom_bot（企业级 channel_configs）的企业，以及全局 env
  WECOM_BOT_ID / WECOM_BOT_SECRET
- 每个 bot 起一个常驻长连接（满足「同 bot 单连接」约束）
- 支持 SIGINT / SIGTERM 优雅退出
"""
import asyncio
import logging
import signal

from channels.wecom.bot_client import WeComAIBotClient
from channels.wecom.config import WECOM_BOT_ID, WECOM_BOT_SECRET
from channels.config_utils import get_company_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wecom-bot] %(levelname)s %(message)s",
)
logger = logging.getLogger("wecom-bot")


def _collect_bots():
    """返回 [(bot_id, secret, company_id), ...]"""
    bots = []
    # 1) 全局 env（company_id=0，运行时路由到演示企业 1）
    if WECOM_BOT_ID and WECOM_BOT_SECRET:
        bots.append((WECOM_BOT_ID, WECOM_BOT_SECRET, 0))
    # 2) 企业级配置
    try:
        from database import SessionLocal
        from models import Company
        db = SessionLocal()
        try:
            for c in db.query(Company).all():
                cfg = get_company_config(c.id, "wecom_bot")
                bid = (cfg.get("wecom_bot_id") or "").strip()
                sec = (cfg.get("wecom_bot_secret") or "").strip()
                if bid and sec:
                    bots.append((bid, sec, c.id))
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"扫描企业级智能机器人配置失败: {e}")
    return bots


async def main():
    bots = _collect_bots()
    if not bots:
        logger.warning("未发现任何已配置的智能机器人（全局 env 或企业级 wecom_bot），进程退出。")
        return

    clients = []
    for bot_id, secret, company_id in bots:
        cid = company_id if company_id else 1   # 全局配置回退到演示企业 1
        clients.append(WeComAIBotClient(bot_id, secret, cid))

    tasks = [asyncio.create_task(c.run()) for c in clients]
    logger.info(f"已启动 {len(clients)} 个智能机器人长连接")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass  # Windows 不支持 add_signal_handler

    await stop.wait()
    logger.info("收到退出信号，正在关闭长连接...")
    for c in clients:
        await c.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
