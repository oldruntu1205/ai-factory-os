"""
connectors/iot/mqtt.py —— IoT 设备数据接入（V2.0）

MQTT 客户端封装：订阅设备遥测数据。
"""
import json
import threading
import logging

logger = logging.getLogger(__name__)


class IoTMQTTClient:
    """轻量 MQTT 客户端（paho-mqtt），未安装时降级为模拟数据"""

    def __init__(self, broker: str = "localhost", port: int = 1883, topics=None):
        self.broker = broker
        self.port = port
        self.topics = topics or ["factory/devices/+/telemetry"]
        self._client = None
        self._running = False

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
            # 生产环境：写入时序库 / 数据湖
        except Exception as e:
            logger.error(f"[IoT] 解析失败: {e}")

    def start(self):
        mqtt = self._try_import()
        if not mqtt:
            logger.warning("[IoT] 未安装 paho-mqtt，IoT 连接不可用")
            return False
        self._client = mqtt.Client()
        self._client.on_message = self.on_message
        self._client.connect(self.broker, self.port, 60)
        for topic in self.topics:
            self._client.subscribe(topic)
        thread = threading.Thread(target=self._client.loop_forever, daemon=True)
        thread.start()
        self._running = True
        return True

    def stop(self):
        if self._client:
            self._client.disconnect()
            self._running = False
