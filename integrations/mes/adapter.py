"""MES 接入（上游 §15 `integrations/mes/`）。**状态：mock。**"""

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
    to_int,
    utc,
)

MES_MAPPING = SystemMapping(
    system="mes",
    source_system="mes-mock",
    status="mock",
    note="MES 原生记录 → V3 事件；字段/单位/时区口径待冻结",
    fields=(
        FieldMapping("workorder.tenant", "tenant_id", text()),
        FieldMapping("workorder.site", "factory_id", text()),
        FieldMapping("workorder.wo_no", "aggregate_id", ident(), note="工单号（权威标识）"),
        FieldMapping("workorder.report_seq", "aggregate_version", integer(), note="报工序号（权威版本）"),
        FieldMapping("workorder.reported_at", "occurred_at", utc(), note="报工时间"),
        FieldMapping("workorder.data_class", "data_class", text()),
        FieldMapping("workorder.line", "aggregate_type", ident(), required=False),
    ),
    payload_fields=(
        FieldMapping("workorder.operation_no", "operation_no", ident(), note="工序号"),
        FieldMapping("workorder.good_qty", "good_quantity", integer(), note="合格数量"),
        FieldMapping("workorder.scrap_qty", "scrap_quantity", integer(), note="报废数量"),
        FieldMapping("workorder.operator", "operator_ref", ident(), required=False),
    ),
)

MES_EVENT_TYPES = ("mes.operation.completed",)

# MES 报工的业务不变式：合格 + 报废 = 报工总数。
# 上游要求"类型合法但语义错误的输出被领域约束拒绝"——这里是最贴近现场的一条。
def assert_report_quantities_consistent(record: Mapping[str, Any]) -> None:
    """报工数量必须自洽；不自洽即拒绝，不做"取其一"。

    数量不自洽意味着现场数据本身有矛盾（漏报、重报或人工改数）。
    静默取一个值会让账实差异一路流到库存与成本，而且**再也查不出来**。
    """
    good = to_int(_at(record, "workorder.good_qty"))
    scrap = to_int(_at(record, "workorder.scrap_qty"))
    total = _at(record, "workorder.report_qty")
    if total is None:
        raise MappingError(
            "MISSING_REPORT_TOTAL",
            "缺少报工总数（workorder.report_qty）——没有总数就无法核验合格/报废是否自洽",
        )
    if good + scrap != to_int(total):
        raise MappingError(
            "REPORT_QUANTITY_MISMATCH",
            f"报工数量不自洽：合格 {good} + 报废 {scrap} ≠ 报工总数 {total}",
        )
    if good < 0 or scrap < 0:
        raise MappingError("NEGATIVE_QUANTITY", f"数量不得为负：good={good} scrap={scrap}")


def _at(record: Mapping[str, Any], path: str) -> Any:
    from integrations._base import _MISSING, _dig

    value = _dig(record, path)
    return None if value is _MISSING else value


def normalize_mes_report(
    record: Mapping[str, Any],
    *,
    event_type: str = "mes.operation.completed",
    factory_timezone: str | None = None,
    received_at: str | None = None,
    verify_quantities: bool = True,
) -> NormalizedEvent:
    """把一条 MES 报工记录归一成 V3 事件。"""
    if event_type not in MES_EVENT_TYPES:
        raise MappingError(
            "UNKNOWN_EVENT_TYPE",
            f"MES 适配器不认识事件类型 {event_type!r}；允许：{list(MES_EVENT_TYPES)}",
        )
    if verify_quantities:
        assert_report_quantities_consistent(record)
    result = normalize_event(
        MES_MAPPING, record, factory_timezone=factory_timezone, received_at=received_at
    )
    result.envelope["event_type"] = event_type
    result.envelope["event_id"] = (
        f"mes:{result.envelope['aggregate_id']}:{result.envelope['aggregate_version']}"
    )
    return result
