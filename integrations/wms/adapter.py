"""WMS 接入（上游 §15 `integrations/wms/`）。**状态：mock。**"""

from __future__ import annotations

from typing import Any, Mapping

from integrations._base import (
    FieldMapping,
    MappingError,
    NormalizedEvent,
    SystemMapping,
    ident,
    integer,
    normalize_event,
    text,
    to_unit,
    utc,
)

WMS_MAPPING = SystemMapping(
    system="wms",
    source_system="wms-mock",
    status="mock",
    note="WMS 原生记录 → V3 事件；字段/单位/时区口径待冻结",
    fields=(
        FieldMapping("txn.tenant", "tenant_id", text()),
        FieldMapping("txn.warehouse", "factory_id", text()),
        FieldMapping("txn.txn_no", "aggregate_id", ident(), note="库存事务号（权威标识）"),
        # 库存事务的版本由 WMS 给出；V3 不得按到达顺序自行编号
        FieldMapping("txn.seq", "aggregate_version", integer(), note="WMS 事务序号（权威）"),
        FieldMapping("txn.posted_at", "occurred_at", utc(), note="过账时间"),
        FieldMapping("txn.data_class", "data_class", text()),
    ),
    payload_fields=(
        FieldMapping("txn.material", "material_code", ident()),
        FieldMapping("txn.location", "location_code", ident(), note="库位"),
        FieldMapping("txn.movement_type", "movement_type", ident(), note="移动类型令牌"),
        FieldMapping(
            "txn.quantity_kg",
            "quantity_g",
            None,
            note="重量 kg→g，换算在 adapter 内显式调用 unit()",
        ),
    ),
)

WMS_EVENT_TYPES = ("wms.stock.changed",)


def normalize_wms_stock_txn(
    record: Mapping[str, Any],
    *,
    event_type: str = "wms.stock.changed",
    factory_timezone: str | None = None,
    received_at: str | None = None,
) -> NormalizedEvent:
    """把一条 WMS 库存事务记录归一成 V3 事件。"""
    if event_type not in WMS_EVENT_TYPES:
        raise MappingError(
            "UNKNOWN_EVENT_TYPE",
            f"WMS 适配器不认识事件类型 {event_type!r}；允许：{list(WMS_EVENT_TYPES)}",
        )
    result = normalize_event(
        WMS_MAPPING, record, factory_timezone=factory_timezone, received_at=received_at
    )
    result.envelope["event_type"] = event_type
    result.envelope["event_id"] = (
        f"wms:{result.envelope['aggregate_id']}:{result.envelope['aggregate_version']}"
    )
    result.envelope.setdefault("aggregate_type", "stock_transaction")
    raw_kg = result.payload.get("quantity_g")
    if raw_kg is not None:
        result.payload["quantity_kg_raw"] = raw_kg
        result.payload["quantity_g"] = to_unit(raw_kg, source_unit="kg", target_unit="g")
    # 换算之后**重新封哈希**——否则 payload_hash 描述的不是最终内容
    from integrations._base import seal_payload

    return seal_payload(result)
