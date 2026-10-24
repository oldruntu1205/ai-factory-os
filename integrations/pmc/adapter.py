"""PMC 接入（上游 §15 `integrations/pmc/`）。**状态：mock。**"""

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
    utc,
)

PMC_MAPPING = SystemMapping(
    system="pmc",
    source_system="pmc-mock",
    status="mock",
    note="PMC 原生记录 → V3 事件；字段/单位/时区口径待冻结",
    fields=(
        FieldMapping("schedule.company", "tenant_id", text()),
        FieldMapping("schedule.plant", "factory_id", text()),
        FieldMapping("schedule.schedule_no", "aggregate_id", ident(), note="排程单号（权威标识）"),
        # 排程版本必须来自 PMC：V3 绝不代为累加，否则乱序事件会静默覆盖新排程
        FieldMapping("schedule.version", "aggregate_version", integer(), note="PMC 侧排程版本（权威）"),
        FieldMapping("schedule.frozen_at", "occurred_at", utc(), note="排程冻结时间"),
        FieldMapping("schedule.data_class", "data_class", text()),
        FieldMapping("schedule.trace_ref", "trace_id", ident(), required=False),
        # `aggregate_type` 是契约必填目录值。PMC 的聚合就是"排程"，
        # 直接声明常量比从字段里猜更可靠（猜错会让不同聚合被混进同一版本序列）。
        FieldMapping("schedule.entity_kind", "aggregate_type", ident(), note="聚合类型（PMC 侧实体种类）", required=False),
    ),
    payload_fields=(
        FieldMapping("schedule.horizon_hours", "horizon_hours", integer(), note="排程视界（小时）"),
        # 时间跨度用登记过的换算：PMC 给分钟，V3 统一秒
        FieldMapping(
            "schedule.bucket_minutes",
            "bucket_seconds",
            None,
            note="时间桶（分钟→秒），换算在 adapter 内显式调用 unit()",
        ),
        FieldMapping("schedule.status", "schedule_status", ident(), note="PMC 排程状态令牌"),
    ),
)

PMC_EVENT_TYPES = ("pmc.schedule.published",)


def normalize_pmc_schedule(
    record: Mapping[str, Any],
    *,
    event_type: str = "pmc.schedule.published",
    factory_timezone: str | None = None,
    received_at: str | None = None,
) -> NormalizedEvent:
    """把一条 PMC 排程发布记录归一成 V3 事件。"""
    if event_type not in PMC_EVENT_TYPES:
        raise MappingError(
            "UNKNOWN_EVENT_TYPE",
            f"PMC 适配器不认识事件类型 {event_type!r}；允许：{list(PMC_EVENT_TYPES)}",
        )
    result = normalize_event(
        PMC_MAPPING, record, factory_timezone=factory_timezone, received_at=received_at
    )
    result.envelope["event_type"] = event_type
    result.envelope["event_id"] = (
        f"pmc:{result.envelope['aggregate_id']}:{result.envelope['aggregate_version']}"
    )
    result.envelope.setdefault("aggregate_type", "schedule")
    # 显式做时间桶换算（分钟 → 秒），并把原始单位留在 payload 里以便对账
    from integrations._base import seal_payload, to_unit

    raw_bucket = result.payload.get("bucket_seconds")
    if raw_bucket is not None:
        result.payload["bucket_minutes_raw"] = raw_bucket
        result.payload["bucket_seconds"] = int(to_unit(raw_bucket, source_unit="min", target_unit="s"))
    # 换算之后**重新封哈希**——否则 payload_hash 描述的不是最终内容
    return seal_payload(result)
