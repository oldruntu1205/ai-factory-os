"""ERP 接入（上游 §15 `integrations/erp/`）。

**状态：mock。** 真实字段/单位/时区属外部依赖（§16.1），未冻结前不猜。
"""

from __future__ import annotations

from typing import Any, Mapping

from integrations._base import (
    FieldMapping,
    NormalizedEvent,
    SystemMapping,
    ident,
    integer,
    normalize_event,
    temperature,
    text,
    to_int,
    to_str,
    to_utc,
    unit,
    utc,
)

# 我们请求 ERP 的字段（点分路径对应 ERP 侧的对象结构）。
# 每一条都写明含义，避免"字段名像就接"。
ERP_MAPPING = SystemMapping(
    system="erp",
    source_system="erp-mock",
    status="mock",
    note="ERP 原生记录 → V3 事件；字段/单位/时区口径待冻结",
    fields=(
        # 外部主键即事件标识的一部分：用 ERP 侧单号，保证重放得到同一个 event_id
        FieldMapping("header.company_code", "tenant_id", text(), note="企业编码 → 租户"),
        FieldMapping("header.plant_code", "factory_id", text(), note="工厂编码"),
        FieldMapping("header.doc_no", "aggregate_id", ident(), note="ERP 单据号（权威标识）"),
        # **版本必须来自 ERP 本身**：适配层不得推断或累加（上游要求"权威版本清楚"）
        FieldMapping("header.change_version", "aggregate_version", integer(), note="ERP 侧变更版本（权威）"),
        FieldMapping("header.created_at", "occurred_at", utc(), note="ERP 记录时间（可能为工厂本地时间）"),
        FieldMapping("header.data_class", "data_class", text(), note="数据等级由 ERP 侧标注"),
        FieldMapping("header.doc_type", "aggregate_type", ident(), note="单据类型"),
        FieldMapping("header.trace_id", "trace_id", ident(), note="ERP 侧链路标识（非必填）", required=False),
    ),
    payload_fields=(
        FieldMapping("line.material_code", "material_code", ident(), note="物料编码"),
        FieldMapping(
            "line.quantity",
            "quantity_mm_equivalent",
            unit("cm", "mm"),
            note="示例：ERP 以 cm 给出长度量，V3 统一存 mm（换算已登记，未登记则拒绝）",
            required=False,
        ),
        FieldMapping(
            "line.temperature",
            "process_temperature_c",
            temperature("fahrenheit"),
            note="示例：ERP 以华氏给出工艺温度，V3 统一摄氏（**含偏移**，不用倍率表）",
            required=False,
        ),
        FieldMapping("line.unit_of_measure", "uom", text(), note="ERP 原始计量单位（保留以便对账）"),
    ),
)

# 事件类型不由 ERP 决定，而由"这条 ERP 记录代表什么"决定（调用方显式给出）。
# 这样避免"从字段名猜事件类型"——猜错会把需求下达当成交付完成。
ERP_EVENT_TYPES = {
    "demand_released": "erp.demand.released",
}


def normalize_erp_demand(
    record: Mapping[str, Any],
    *,
    event_type: str = "erp.demand.released",
    factory_timezone: str | None = None,
    received_at: str | None = None,
) -> NormalizedEvent:
    """把一条 ERP 需求下达记录归一成 V3 事件。"""
    if event_type not in tuple(ERP_EVENT_TYPES.values()):
        from integrations._base import MappingError

        raise MappingError(
            "UNKNOWN_EVENT_TYPE",
            f"ERP 适配器不认识事件类型 {event_type!r}；允许：{sorted(ERP_EVENT_TYPES.values())}",
        )
    return _normalize(record, event_type, factory_timezone, received_at)


def _normalize(
    record: Mapping[str, Any],
    event_type: str,
    factory_timezone: str | None,
    received_at: str | None,
) -> NormalizedEvent:
    result = normalize_event(
        ERP_MAPPING, record, factory_timezone=factory_timezone, received_at=received_at
    )
    result.envelope["event_type"] = event_type
    # event_id 由**外部权威标识 + 版本**派生：重放同一条 ERP 记录得到同一个 id，
    # 从而被 inbox 去重规则收敛，而不是每次都造一条新事实。
    result.envelope["event_id"] = f"erp:{result.envelope['aggregate_id']}:{result.envelope['aggregate_version']}"
    return result
