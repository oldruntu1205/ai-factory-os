"""
connectors/erp/kingdee.py —— 金蝶 ERP 连接器（占位实现）

安全修复：未对接真实 API 前禁止返回伪装成真实数据的假值（如 stock=0），
统一抛 NotImplementedError，调用方必须感知"未实现"而非把占位当真实业务数据。
"""
from connectors.erp.base import ERPConnector


class KingdeeConnector(ERPConnector):
    def __init__(self, base_url: str, app_id: str, app_secret: str):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret

    def _not_implemented(self, method: str) -> dict:
        raise NotImplementedError(
            f"金蝶连接器 {method} 尚未对接真实 API（base_url={self.base_url}），"
            "请实现金蝶云星空对接后再启用；当前返回占位数据会误导业务决策")

    def get_product(self, product_id: str) -> dict:
        return self._not_implemented("get_product")

    def get_stock(self, product_id: str) -> dict:
        return self._not_implemented("get_stock")

    def create_order(self, order: dict) -> dict:
        return self._not_implemented("create_order")

    def get_customer(self, customer_id: str) -> dict:
        return self._not_implemented("get_customer")
