"""
connectors/erp/sap.py —— SAP ERP 连接器示例

安全/可靠性加固：统一异常捕获 + 状态码校验 + 结构化返回（不再裸抛 JSONDecodeError）。
注意：当前为通用 REST 壳（非真实 SAP OData/RFC），对接真实系统时按 SAP 接口调整路径。
"""
import logging
import requests
from connectors.erp.base import ERPConnector

logger = logging.getLogger(__name__)


class SAPConnector(ERPConnector):
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """统一请求：异常捕获 + 状态码校验，返回结构化结果"""
        try:
            r = requests.request(method, f"{self.base_url}{path}",
                                 headers=self._headers(), timeout=10, **kwargs)
            r.raise_for_status()
            return {"success": True, "data": r.json(), "status": r.status_code}
        except requests.exceptions.Timeout:
            return {"success": False, "error": "SAP 连接超时"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"SAP 请求失败: {e}"}
        except ValueError:
            return {"success": False, "error": "SAP 返回非 JSON"}

    def get_product(self, product_id: str) -> dict:
        return self._request("GET", f"/api/products/{product_id}")

    def get_stock(self, product_id: str) -> dict:
        return self._request("GET", f"/api/products/{product_id}/stock")

    def create_order(self, order: dict) -> dict:
        return self._request("POST", "/api/orders", json=order)

    def get_customer(self, customer_id: str) -> dict:
        return self._request("GET", f"/api/customers/{customer_id}")
