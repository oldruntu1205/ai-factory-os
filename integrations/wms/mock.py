"""WMS Mock 记录与对接清单。**Mock，不是生产样例**（§16.1）。"""

from __future__ import annotations

from typing import Any

REQUIRED_FROM_WMS: dict[str, str] = {
    "产品与版本": "WMS 产品名与版本号",
    "API 授权": "调用凭据与授权范围（凭据只走密钥管理）",
    "对象 ID 与字段": "库存事务号、库位、物料编码的字段名与口径",
    "单位": "数量字段的原始单位与计量基准（重量？件数？体积？）",
    "时间与时区": "过账时间的时区语义；工厂时区标识",
    "核销口径": "库存与账务的核销基准（上游要求『账随实动』）",
    "版本语义": "事务序号是否严格单调、是否保证连续",
}

MOCK_STOCK_TXN_RECORD: dict[str, Any] = {
    "txn": {
        "tenant": "tenant-demo",
        "warehouse": "factory-demo",
        "txn_no": "wms-txn-demo-1",
        "seq": 41,
        "posted_at": "2026-10-05T11:20:00+08:00",
        "data_class": "C2",
        "material": "MAT-1001",
        "location": "WH-A-01",
        "movement_type": "receipt",
        "quantity_kg": 250.5,
    }
}
