"""PMC Mock 记录与对接清单。**Mock，不是生产样例**（§16.1）。"""

from __future__ import annotations

from typing import Any

REQUIRED_FROM_PMC: dict[str, str] = {
    "产品与版本": "PMC 产品名与版本号",
    "API 授权": "调用凭据与授权范围（凭据只走密钥管理）",
    "对象 ID 与字段": "排程单号、工厂/车间编码的字段名与口径",
    "单位": "时间桶与视界的原始单位（未登记换算一律拒绝）",
    "时间与时区": "冻结时间字段的时区语义；工厂时区标识",
    "事件语义": "何时发布 schedule.published（冻结即发？修订也发？）",
    "版本语义": "version 是否严格单调、修订是否递增",
}

MOCK_SCHEDULE_RECORD: dict[str, Any] = {
    "schedule": {
        "company": "tenant-demo",
        "plant": "factory-demo",
        "schedule_no": "pmc-sched-demo-1",
        "version": 7,
        "frozen_at": "2026-10-05T09:00:00+08:00",
        "data_class": "C2",
        "trace_ref": "trace-pmc-demo-1",
        "horizon_hours": 72,
        "bucket_minutes": 30,
        "status": "frozen",
    }
}
