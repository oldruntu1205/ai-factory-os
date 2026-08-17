"""
connectors/erp/base.py —— ERP 连接层（第五阶段 第六部分）

不要直接绑定一个 ERP，统一抽象 Connector 接口，
SAP / 金蝶 / 用友 / Oracle 各自实现。
"""
from abc import ABC, abstractmethod


class ERPConnector(ABC):
    """ERP 统一接口"""

    @abstractmethod
    def get_product(self, product_id: str) -> dict:
        """查询产品信息"""

    @abstractmethod
    def get_stock(self, product_id: str) -> dict:
        """查询库存"""

    @abstractmethod
    def create_order(self, order: dict) -> dict:
        """创建销售订单"""

    @abstractmethod
    def get_customer(self, customer_id: str) -> dict:
        """查询客户"""
