"""
connectors/mes/mes_api.py —— MES 连接器（V2.0 第五章）

生产执行系统数据接入：生产状态、订单进度、产线负载。
"""
import requests


class MESConnector:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def get_production_status(self, order_id: str) -> dict:
        """查询订单生产状态"""
        r = requests.get(f"{self.url}/orders/{order_id}",
                         headers=self._headers(), timeout=10)
        return r.json()

    def get_line_load(self, line: str) -> dict:
        """查询产线负载"""
        r = requests.get(f"{self.url}/lines/{line}/load",
                         headers=self._headers(), timeout=10)
        return r.json()

    def get_orders(self, params: dict = None) -> list:
        """查询订单列表"""
        r = requests.get(f"{self.url}/orders", headers=self._headers(),
                         params=params or {}, timeout=10)
        return r.json()
