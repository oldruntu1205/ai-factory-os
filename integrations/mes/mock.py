"""MES Mock 记录与对接清单。**Mock，不是生产样例**（§16.1）。"""

from __future__ import annotations

from typing import Any

REQUIRED_FROM_MES: dict[str, str] = {
    "产品与版本": "MES 产品名与版本号",
    "API 授权": "调用凭据与授权范围（凭据只走密钥管理）",
    "对象 ID 与字段": "工单号、工序号、产线编码的字段名与口径",
    "数量口径": "合格/报废/报工总数的字段名，以及三者是否必然自洽",
    "时间与时区": "报工时间字段的时区语义；工厂时区标识",
    "事件语义": "报工完成的事件粒度（按工序？按工单？）",
    "版本语义": "报工序号是否严格单调",
}

MOCK_REPORT_RECORD: dict[str, Any] = {
    "workorder": {
        "tenant": "tenant-demo",
        "site": "factory-demo",
        "wo_no": "mes-wo-demo-1",
        "report_seq": 12,
        "reported_at": "2026-10-05T10:15:00+08:00",
        "data_class": "C2",
        "line": "mes-line-a",
        "operation_no": "OP-20",
        "good_qty": 95,
        "scrap_qty": 5,
        "report_qty": 100,
        "operator": "mes-op-demo",
    }
}

# 数量不自洽的记录：合格 + 报废 ≠ 报工总数。**必须被拒绝**，
# 不允许"取其一"——静默取值会让账实差异再也查不出来。
MOCK_INCONSISTENT_REPORT_RECORD: dict[str, Any] = {
    "workorder": {**MOCK_REPORT_RECORD["workorder"], "wo_no": "mes-wo-demo-bad", "report_qty": 90}
}
