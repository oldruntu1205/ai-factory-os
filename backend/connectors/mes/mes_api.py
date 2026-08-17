"""
connectors/mes/mes_api.py —— MES 连接器（V2.0 第五章）

生产执行系统数据接入：生产状态、订单进度、产线负载。
安全/可靠性加固：统一异常捕获 + 状态码校验 + 结构化返回（不再裸抛 JSONDecodeError）。
"""
import logging
import requests

logger = logging.getLogger(__name__)


class MESConnector:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = requests.request(method, f"{self.url}{path}",
                                 headers=self._headers(), timeout=10, **kwargs)
            r.raise_for_status()
            return {"success": True, "data": r.json(), "status": r.status_code}
        except requests.exceptions.Timeout:
            return {"success": False, "error": "MES 连接超时"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"MES 请求失败: {e}"}
        except ValueError:
            return {"success": False, "error": "MES 返回非 JSON"}

    def get_production_status(self, order_id: str) -> dict:
        """查询订单生产状态"""
        return self._request("GET", f"/orders/{order_id}")

    def get_line_load(self, line: str) -> dict:
        """查询产线负载"""
        return self._request("GET", f"/lines/{line}/load")

    def get_orders(self, params: dict = None) -> dict:
        """查询订单列表"""
        return self._request("GET", "/orders", params=params or {})
