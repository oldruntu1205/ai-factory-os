"""
channels/wecom/bot_client.py —— 企业微信智能机器人长连接接入（原生 websockets）

协议参考：企业微信开发者中心《智能机器人长连接》及官方 aibot-node-sdk
- WebSocket: wss://openws.work.weixin.qq.com
- 鉴权订阅: aibot_subscribe {cmd, headers:{req_id}, body:{bot_id, secret}}
- 心跳保活: 应用层 ping 帧（cmd="ping"），每 30s 一次；禁用标准 WebSocket ping/pong
- 收消息:   aibot_msg_callback {headers:{req_id}, body:{msgid, from:{userid}, msgtype, text:{content}, chattype, chatid}}
- 回复:     aibot_respond_msg {headers:{req_id(透传收到帧的 req_id)}, body:{msgtype:"stream", stream:{id, finish, content}}}
- 约束:     同一 bot 同一时刻仅允许一个长连接；新连接会踢掉旧连接
- 超时:     收到消息后 5s 内需先回复占位流式帧（finish=false），再发最终结果（finish=true，同一 stream.id）

企业级配置（channel=wecom_bot）优先，回退全局 env WECOM_BOT_ID/WECOM_BOT_SECRET。
"""
import asyncio
import json
import logging
import time
import uuid
from collections import deque

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://openws.work.weixin.qq.com"
SUBSCRIBE_CMD = "aibot_subscribe"
MSG_CALLBACK_CMD = "aibot_msg_callback"
EVENT_CALLBACK_CMD = "aibot_event_callback"
REPLY_CMD = "aibot_respond_msg"
HEARTBEAT_INTERVAL = 30                # 秒
REPLY_ACK_TIMEOUT = 5                  # 企微要求收到消息后 5s 内回复占位
DEDUP_WINDOW = 300                     # msgid 去重窗口（秒）


def _req_id() -> str:
    return uuid.uuid4().hex


class WeComAIBotClient:
    """单个智能机器人的长连接客户端（单连接）"""

    def __init__(self, bot_id: str, secret: str, company_id: int):
        self.bot_id = (bot_id or "").strip()
        self.secret = (secret or "").strip()
        self.company_id = company_id
        self.ws = None
        self.running = False
        self._reconnect_delay = 5
        self._cooldown = 0
        self._rl_backoff = 0  # 846609 订阅限流指数退避（秒），成功订阅才清零
        self._subscribed = False  # 本轮连接是否已成功完成订阅（errcode 0）
        self._seen_msgids: deque[tuple[str, float]] = deque()
        logger.info(f"[wecom-bot:{self.bot_id}] 初始化 bot_id_len={len(self.bot_id)} secret_len={len(self.secret)}")

    async def run(self):
        """常驻运行，断线指数退避重连。

        重连策略：
        - 已订阅成功的稳定连接断线：5s → 10s → 20s → 30s → 60s（封顶）常规指数退避重连
        - 收到 846609（订阅限流）：进入持续增长冷却（60s → 120s → 240s → 480s → 600s 封顶）后重试
        - **本轮连接从未成功订阅就退出**（含 846609 以外的「静默断连 / 鉴权无响应」）：
          企微在限流期对订阅请求常表现为直接断开且不下发错误帧，这种情况也必须计入退避增长，
          绝不可回落到 5s 频繁重连（否则会在限流期持续高频戳企微，导致 ban 永远解不开）
        - 订阅成功（errcode 0）后重置退避计数
        """
        self.running = True
        self._reconnect_delay = 5
        while self.running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[wecom-bot:{self.bot_id}] 连接断开: {e}")
            if not self.running:
                break
            if self._cooldown > 0:
                # 846609 分支已设置冷却，直接等待（不重置退避序列）
                delay = self._cooldown
                self._cooldown = 0
                self._reconnect_delay = 5
                logger.warning(f"[wecom-bot:{self.bot_id}] 冷却 {delay}s 后重连")
            elif not self._subscribed:
                # 本轮连接从未订阅成功（静默断连 / 鉴权无响应）：视为限流期未过，增长退避
                self._rl_backoff = 60 if self._rl_backoff == 0 else min(self._rl_backoff * 2, 600)
                delay = self._rl_backoff
                self._reconnect_delay = 5
                logger.warning(f"[wecom-bot:{self.bot_id}] 本轮未成功订阅（可能仍限流），退避 {delay}s 后重试"
                               f"（请勿 restart 服务，否则重置退避立即再戳）")
            else:
                delay = self._reconnect_delay
                self._reconnect_delay = min(delay * 2, 60)
                logger.info(f"[wecom-bot:{self.bot_id}] {delay}s 后重连")
            await asyncio.sleep(delay)

    async def _connect_once(self):
        # ping_interval=None：禁用 websockets 库内置的标准 WebSocket ping/pong
        # 企微协议要求发送应用层 cmd="ping" 帧保活，标准控制帧会被网关/服务端忽略或导致 1011/1002 断线
        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            close_timeout=10,
        ) as ws:
            self.ws = ws
            self._reconnect_delay = 5
            self._subscribed = False  # 本轮连接尚未完成订阅，待收到 errcode 0 才置 True
            logger.info(f"[wecom-bot:{self.bot_id}] WebSocket 已连接")
            # 1) 鉴权订阅
            auth_req = {
                "cmd": SUBSCRIBE_CMD,
                "headers": {"req_id": _req_id()},
                "body": {"bot_id": self.bot_id, "secret": self.secret},
            }
            # 安全修复：禁止完整打印订阅帧（含 bot secret），仅输出非敏感信息
            logger.info(f"[wecom-bot:{self.bot_id}] 发送鉴权: cmd={SUBSCRIBE_CMD} "
                        f"bot_id={self.bot_id} secret_len={len(self.secret)}")
            await ws.send(json.dumps(auth_req))
            # 2) 启动应用层心跳
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            try:
                # 3) 接收循环（带 45s 接收超时看门狗：企微可能不发 close 帧直接静默断连，
                #    ping_interval=None 时库内置超时失效，必须靠 recv 超时主动探测死连接）
                while self.running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                    except asyncio.TimeoutError:
                        logger.warning(f"[wecom-bot:{self.bot_id}] 45s 内无数据，判定连接已死，准备重连")
                        break
                    frame = self._safe_parse(raw)
                    if frame is None:
                        logger.warning(f"[wecom-bot:{self.bot_id}] 收到非 JSON 帧: {raw[:200]}")
                        continue
                    logger.info(f"[wecom-bot:{self.bot_id}] 收到帧: {json.dumps(frame, ensure_ascii=False)[:500]}")
                    await self._dispatch_frame(frame)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _heartbeat_loop(self):
        """每 30s 发送一次应用层 ping 帧"""
        try:
            while self.running and self.ws is not None:
                try:
                    ping_frame = {"cmd": "ping", "headers": {"req_id": _req_id()}}
                    await self.ws.send(json.dumps(ping_frame))
                    logger.debug(f"[wecom-bot:{self.bot_id}] 发送心跳 ping")
                except Exception as e:
                    logger.warning(f"[wecom-bot:{self.bot_id}] 心跳发送失败: {e}")
                    return
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            logger.debug(f"[wecom-bot:{self.bot_id}] 心跳任务结束")
            raise

    async def _dispatch_frame(self, frame: dict):
        errcode = frame.get("errcode")
        if errcode is not None and errcode != 0:
            logger.error(f"[wecom-bot:{self.bot_id}] 企微返回错误 "
                         f"errcode={errcode} errmsg={frame.get('errmsg')}")
            if errcode == 846609:
                # 订阅频率限制（企微明确：aibot_subscribe 有频率保护，反复请求会触发系统限制）。
                # 指数退避且持续增长，避免反复戳被限流的 bot 导致 ban 永远解不开：
                # 60s → 120s → 240s → 480s → 600s(封顶)。成功订阅(errcode 0)才清零。
                # 注意：手动 restart 服务会把退避重置为 0、立即再发订阅，等于亲手续 ban，切忌。
                self._rl_backoff = 60 if self._rl_backoff == 0 else min(self._rl_backoff * 2, 600)
                self._cooldown = self._rl_backoff
                logger.warning(f"[wecom-bot:{self.bot_id}] 846609 订阅限流，{self._cooldown}s 后重试"
                               f"（退避已增至 {self._cooldown}s；请勿 restart 服务，否则重置退避立即再戳）")
            await self.ws.close()
            return
        cmd = frame.get("cmd")
        if errcode == 0 and cmd == SUBSCRIBE_CMD:
            self._reconnect_delay = 5
            self._cooldown = 0
            self._rl_backoff = 0
            self._subscribed = True
            logger.info(f"[wecom-bot:{self.bot_id}] 鉴权成功")
            return
        if cmd == MSG_CALLBACK_CMD:
            asyncio.create_task(self._handle_message(frame))
            return
        if cmd == EVENT_CALLBACK_CMD:
            logger.debug(f"[wecom-bot:{self.bot_id}] 收到事件回调，暂不处理")
            return
        if cmd == "pong" or cmd == "ping":
            logger.debug(f"[wecom-bot:{self.bot_id}] 收到心跳响应")
            return
        logger.debug(f"[wecom-bot:{self.bot_id}] 收到非消息帧 cmd={cmd}")

    @staticmethod
    def _safe_parse(raw):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _is_duplicate(self, msgid: str) -> bool:
        now = time.time()
        # 清理过期窗口
        while self._seen_msgids and now - self._seen_msgids[0][1] > DEDUP_WINDOW:
            self._seen_msgids.popleft()
        for seen_id, _ in self._seen_msgids:
            if seen_id == msgid:
                return True
        if msgid:
            self._seen_msgids.append((msgid, now))
        return False

    async def _handle_message(self, frame: dict):
        try:
            body = frame.get("body", {})
            req_id = frame.get("headers", {}).get("req_id", "")
            msgid = body.get("msgid", "")
            msgtype = body.get("msgtype")
            userid = body.get("from", {}).get("userid", "")
            content = self._extract_text(body, msgtype)
            logger.info(f"[wecom-bot:{self.bot_id}] 收到消息 msgid={msgid} from={userid} msgtype={msgtype} content={content[:200]}")
            if not content:
                logger.info(f"[wecom-bot:{self.bot_id}] 忽略非文本消息({msgtype}) from={userid}")
                return
            if self._is_duplicate(msgid):
                logger.info(f"[wecom-bot:{self.bot_id}] 重复消息 msgid={msgid} 已忽略")
                return
            # 5s 内必须先发送占位流式帧，满足企微回调超时要求
            stream_id = _req_id()
            await self._reply_stream(req_id, stream_id, "正在思考，请稍候...", finish=False)
            # 调 AI 员工（同步 LLM 调用放到线程，避免阻塞事件循环）
            from agents.service import execute_agent
            answer = await asyncio.to_thread(execute_agent, content, self.company_id)
            if not answer:
                answer = "抱歉，暂时没有可用的回答。"
            logger.info(f"[wecom-bot:{self.bot_id}] AI回复 len={len(answer)} preview={answer[:200]}")
            await self._reply_stream(req_id, stream_id, answer, finish=True)
            logger.info(f"[wecom-bot:{self.bot_id}] 已回复 from={userid} len={len(answer)}")
        except Exception as e:
            logger.error(f"[wecom-bot:{self.bot_id}] 处理消息异常: {e}", exc_info=True)
            # 异常时也尝试返回一个友好提示
            try:
                req_id = frame.get("headers", {}).get("req_id", "")
                await self._reply_stream(req_id, _req_id(), "处理消息时出错了，请稍后再试。", finish=True)
            except Exception:
                pass

    @staticmethod
    def _extract_text(body: dict, msgtype: str) -> str:
        if msgtype == "text":
            return (body.get("text", {}) or {}).get("content", "").strip()
        if msgtype == "mixed":
            items = (body.get("mixed", {}) or {}).get("msg_item", []) or []
            return " ".join(
                (it.get("text", {}) or {}).get("content", "")
                for it in items if it.get("msgtype") == "text"
            ).strip()
        # image/voice/file/video 等暂不支持直接文本解析
        return ""

    async def _reply_stream(self, req_id: str, stream_id: str, content: str, finish: bool = True):
        payload = {
            "cmd": REPLY_CMD,
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": finish, "content": content},
            },
        }
        logger.info(f"[wecom-bot:{self.bot_id}] 发送回复帧(finish={finish}): {json.dumps(payload, ensure_ascii=False)[:500]}")
        await self.ws.send(json.dumps(payload))

    async def reply_text(self, req_id: str, content: str):
        """普通文本回复（非流式）"""
        payload = {
            "cmd": REPLY_CMD,
            "headers": {"req_id": req_id},
            "body": {"msgtype": "text", "text": {"content": content}},
        }
        await self.ws.send(json.dumps(payload))

    async def stop(self):
        self.running = False
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass


async def test_bot(bot_id: str, secret: str) -> dict:
    """一次性连接测试：建立连接 -> 订阅 -> 等待订阅响应。

    注意：与常驻连接共享「同 bot 单连接」约束，测试时会短暂踢掉常驻连接，
    测试完成后常驻进程会自动重连，属正常行为。
    """
    bot_id = (bot_id or "").strip()
    secret = (secret or "").strip()
    if not bot_id or not secret:
        return {"ok": False, "error": "Bot ID 与 Secret 必填"}
    logger.info(f"[wecom-bot:test] 测试鉴权 bot_id_len={len(bot_id)} secret_len={len(secret)}")
    try:
        async with websockets.connect(WS_URL, ping_interval=None, close_timeout=5) as ws:
            await ws.send(json.dumps({
                "cmd": SUBSCRIBE_CMD,
                "headers": {"req_id": _req_id()},
                "body": {"bot_id": bot_id, "secret": secret},
            }))
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            logger.info(f"[wecom-bot:test] 收到响应: {raw[:500]}")
            frame = json.loads(raw)
            errcode = frame.get("errcode")
            if errcode == 0:
                return {"ok": True, "message": "连接与鉴权成功"}
            return {"ok": False, "error": frame.get("errmsg") or f"errcode={errcode}"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "订阅超时（15s 未收到企微响应）"}
    except Exception as e:
        return {"ok": False, "error": f"连接失败: {e}"}
