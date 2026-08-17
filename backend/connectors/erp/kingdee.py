"""
connectors/erp/kingdee.py —— 金蝶 ERP 连接器示例（占位实现）
"""
from connectors.erp.base import ERPConnector


class KingdeeConnector(ERPConnector):
    def __init__(self, base_url: str, app_id: str, app_secret: str):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret

    def get_product(self, product_id: str) -> dict:
        # TODO: 调用金蝶云星空 API
        return {"id": product_id, "source": "kingdee", "note": "需要实现金蝶 API 对接"}

    def get_stock(self, product_id: str) -> dict:
        return {"id": product_id, "stock": 0, "source": "kingdee"}

    def create_order(self, order: dict) -> dict:
        return {"status": "pending", "source": "kingdee", "order": order}

    def get_customer(self, customer_id: str) -> dict:
        return {"id": customer_id, "source": "kingdee"}
