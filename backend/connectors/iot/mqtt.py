"""
connectors/iot/mqtt.py —— IoT 设备数据接入（V2.0）

MQTT 客户端封装：订阅设备遥测数据。
安全/可靠性加固：支持 TLS 与用户名密码、连接/订阅异常捕获、on_connect 回调内订阅、
stop 完整停止线程、遥测数据持久化接口（当前落日志占位，生产接时序库）。
"""
import json
import threading
import logging

logger = logging.getLogger(__name__)


class IoTMQTTClient:
    """轻量 MQTT 客户端（paho-mqtt），未安装时降级为模拟数据"""

    def __init__(self, broker: str = "localhost", port: int = 1883, topics=None,
                 username: str = None, password: str = None, use_tls: bool = False,
                 ca_certs: str = None, client_cert: str = None, client_key: str = None):
        self.broker = broker
        self.port = port
        self.topics = topics or ["factory/devices/+/telemetry"]
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.ca_certs = ca_certs
        self.client_cert = client_cert
        self.client_key = client_key
        self._client = None
        self._running = False
        self._thread = None

    def _try_import(self):
        try:
            import paho.mqtt.client as mqtt
            return mqtt
        except ImportError:
            return None

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            logger.info(f"[IoT] {msg.topic}: {payload}")
            # 生产环境：写入时序库 / 数据湖（当前仅日志，不落库会丢数据——接入时实现）
            self._persist(msg.topic, payload)
        except Exception as e:
            logger.error(f"[IoT] 解析失败: {e}")

    def _persist(self, topic: str, payload: dict) -> None:
        """遥测持久化占位：生产接入时序库/DB，避免数据仅存日志即丢失"""
        pass

    def _on_connect(self, client, userdata, flags, rc):
        """连接成功后订阅（修复：此前在 connect 前 subscribe，重连场景丢订阅）"""
        if rc == 0:
            for topic in self.topics:
                client.subscribe(topic)
            logger.info(f"[IoT] 已连接 {self.broker}:{self.port}，订阅 {self.topics}")
        else:
            logger.error(f"[IoT] 连接失败 rc={rc}")

    def start(self):
        mqtt = self._try_import()
        if not mqtt:
            logger.warning("[IoT] 未安装 paho-mqtt，IoT 连接不可用")
            return False
        try:
            self._client = mqtt.Client()
            if self.use_tls:
                # 安全加固：生产必须 TLS，避免遥测明文传输
                self._client.tls_set(ca_certs=self.ca_certs,
                                     certfile=self.client_cert, keyfile=self.client_key)
            if self.username:
                self._client.username_pw_set(self.username, self.password)
            self._client.on_message = self.on_message
            self._client.on_connect = self._on_connect
            self._client.connect(self.broker, self.port, 60)
            self._thread = threading.Thread(target=self._client.loop_forever, daemon=True)
            self._thread.start()
            self._running = True
            return True
        except Exception as e:
            logger.error(f"[IoT] 启动失败: {e}")
            return False

    def stop(self):
        if self._client:
            try:
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:
                pass
        self._running = False
        logger.info("[IoT] 已停止")
