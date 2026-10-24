"""ERP 接入层的 Mock 记录与字段目录。

**这些是 Mock，不是生产样例**（上游 §16.1）。
Mock 的作用是让"映射代码能不能跑通"可验证；
真实字段/单位/时区必须由 ERP 提供后冻结替换。
"""

from __future__ import annotations

from typing import Any

# 字段目录：显式列出我们**期望**从 ERP 拿到什么。
# 这张表就是给对方的对接清单——不填编造值，只列缺口。
REQUIRED_FROM_ERP: dict[str, str] = {
    "产品与版本": "ERP 产品名与版本号（决定 API 形态）",
    "API 授权": "调用凭据与授权范围（凭据只走密钥管理，不入库/不入仓）",
    "对象 ID 与字段": "company/plant/material 等编码的字段名与取值口径",
    "单位": "各数量字段的原始计量单位（未登记的换算一律拒绝）",
    "时间与时区": "时间字段是本地时间还是带偏移；工厂时区标识（未提供则拒绝）",
    "拉取方式": "webhook 还是轮询；轮询频率与限流上限",
    "版本语义": "change_version 的语义（单调递增？何时递增？）",
}

MOCK_DEMAND_RECORD: dict[str, Any] = {
    "header": {
        "company_code": "tenant-demo",
        "plant_code": "factory-demo",
        "doc_no": "erp-demand-demo-1",
        "doc_type": "demand",
        "change_version": 3,
        # 带显式偏移：这是**唯一**可以不带工厂时区的情形
        "created_at": "2026-10-05T08:30:00+08:00",
        "data_class": "C2",
        "trace_id": "trace-erp-demo-1",
    },
    "line": {
        "material_code": "MAT-1001",
        "quantity": 12.5,          # cm
        "temperature": 194.0,      # fahrenheit
        "unit_of_measure": "cm",
    },
}

# 一条**工厂本地时间**（无时区）的记录：必须显式提供工厂时区才可归一
MOCK_DEMAND_RECORD_LOCAL_TIME: dict[str, Any] = {
    **MOCK_DEMAND_RECORD,
    "header": {
        **MOCK_DEMAND_RECORD["header"],  # type: ignore[dict-item]
        "doc_no": "erp-demand-demo-2",
        "created_at": "2026-10-05 08:30:00",
    },
}
