"""
connectors/erp/sap.py —— SAP ERP 连接器示例
"""
import requests
from connectors.erp.base import ERPConnector


class SAPConnector(ERPConnector):
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def get_product(self, product_id: str) -> dict:
        r = requests.get(f"{self.base_url}/api/products/{product_id}",
                         headers=self._headers(), timeout=10)
        return r.json()

    def get_stock(self, product_id: str) -> dict:
        r = requests.get(f"{self.base_url}/api/products/{product_id}/stock",
                         headers=self._headers(), timeout=10)
        return r.json()

    def create_order(self, order: dict) -> dict:
        r = requests.post(f"{self.base_url}/api/orders", headers=self._headers(),
                          json=order, timeout=10)
        return r.json()

    def get_customer(self, customer_id: str) -> dict:
        r = requests.get(f"{self.base_url}/api/customers/{customer_id}",
                         headers=self._headers(), timeout=10)
        return r.json()
