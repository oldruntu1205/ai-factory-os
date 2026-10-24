"""外部系统接入层的公共骨架（上游 §15 `integrations/`）。

## 这一层解决什么

上游 §16 把 P2 的依赖明确写成「**ERP/PMC/MES/WMS 字段/事件映射**」，§15 要求
`integrations/{erp,pmc,mes,wms}/` 提供「原生接口映射、兼容测试及 Mock」。

所以这一层的职责是**唯一**的一件事：把外部系统的原生记录，**确定性地**翻译成
V3 的事件事实（`event-envelope.json`）。它不属于任何服务，也不持有状态——
事件的排序、去重、迟到处理已在 `services/_kernel/events.py` 实现，这里只管翻译。

## 三条硬规则（都来自上游，且都能被测试）

### 1. 不编造生产值（§16.1 明文）

> 对未填项实现显式配置和 Mock，**不填编造的生产值**。

因此映射规则里：

- 缺字段 ⇒ **拒绝**（明确报出缺哪个），**不用默认值补齐**；
- 单位不一致 ⇒ **拒绝**，不"取近似"；
- 没有时区信息的本地时间 ⇒ **拒绝**，不假设它是 UTC。

这三条如果放水，产生的都是"看起来对、实际错"的事实——比报错危险得多。

### 2. `aggregate_version` 必须来自**权威系统**

上游要求「重复/乱序/中断恢复不破坏事实；权威版本清楚」。
版本的权威来源是外部系统本身，适配层**绝不代为推断或累加**。
把版本号"猜"出来，就等于让乱序事件静默覆盖新事实。

### 3. 时区必须显式（`UtcTimestamp` 要求 `Z` 结尾，禁止本地时区）

外部系统常给**本地时间**（`2026-10-05 08:30:00`）。这类值**没有**可推导的 UTC 含义：
工厂时区一旦猜错，事件就整体平移几小时，而所有校验依然通过。
所以只有两种合法输入：

- 已带显式偏移/`Z` 的时间戳 → 归一到 UTC；
- 工厂本地时间 → **必须**给出工厂时区（`zoneinfo`），按其换算。

两者都不是 ⇒ 拒绝。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ==========================================================================
# 错误
# ==========================================================================


class MappingError(Exception):
    """映射失败。**必须可定位**：报错要指出是哪个字段、哪条记录。

    上游要求"失败可被定位"：一句笼统的『映射失败』等于没报错。
    """

    def __init__(self, code: str, message: str, *, external_field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.external_field = external_field


# ==========================================================================
# 转换器：**显式注册**，不隐式兜底
# ==========================================================================


def to_utc(value: Any, *, factory_timezone: str | None) -> str:
    """把外部时间归一成契约要求的 UTC（`...Z`）。

    只接受两种输入（其余一律拒绝）：

    1. **带显式时区**的时间戳：`2026-10-05T08:30:00+08:00`、`...Z`、`... +0800`
       —— 有明确偏移，可直接归一；
    2. **工厂本地时间**：`2026-10-05 08:30:00` —— 需要 `factory_timezone`
       指定它属于哪个时区。

    **拒绝**没有时区信息、又没给工厂时区的裸时间。假设它是 UTC 会在
    真实工厂里整体平移数小时，而且**所有校验依然通过**——属于"静默错误"。
    """
    text = str(value).strip()
    if not text:
        raise MappingError("EMPTY_TIMESTAMP", "时间戳为空")

    normalized = text.replace(" ", "T")
    parsed = _parse_with_offset(normalized)
    if parsed is not None:
        return _to_z(parsed)

    # 到这里说明没有时区信息——必须是工厂本地时间，且必须给出时区
    naive = _parse_naive(normalized)
    if naive is None:
        raise MappingError("UNPARSABLE_TIMESTAMP", f"无法解析的时间戳：{text!r}")

    if not factory_timezone:
        raise MappingError(
            "TIMEZONE_REQUIRED",
            f"时间戳 {text!r} 不带时区信息，必须提供工厂时区才能归一（"
            "不得假定它是 UTC——猜错会让事件整体平移且所有校验仍通过）",
        )
    try:
        zone = ZoneInfo(factory_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise MappingError("UNKNOWN_TIMEZONE", f"未知时区：{factory_timezone!r}") from exc
    return _to_z(naive.replace(tzinfo=zone))


def _parse_with_offset(text: str) -> datetime | None:
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_naive(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _to_z(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}".rstrip("0")
    return text + "Z"


# 单位换算：**只有登记过的换算**，不做"取近似"。
# 键是 (外部单位, 目标单位)，值是倍率——外部值 × 倍率 = 目标值。
UNIT_FACTORS: dict[tuple[str, str], float] = {
    ("mm", "mm"): 1.0,
    ("cm", "mm"): 10.0,
    ("m", "mm"): 1000.0,
    ("g", "g"): 1.0,
    ("kg", "g"): 1000.0,
    ("t", "g"): 1_000_000.0,
    ("s", "s"): 1.0,
    ("min", "s"): 60.0,
    ("h", "s"): 3600.0,
    ("c", "c"): 1.0,  # 温度用绝对刻度，见 to_celsius
    ("celsius", "c"): 1.0,
    ("fahrenheit", "c"): None,  # 需要偏移，不能只用倍率——显式拒绝，见下
}


def to_unit(value: Any, *, source_unit: str, target_unit: str) -> float:
    """按**登记过的**换算表做单位归一。

    未登记的换算对**一律拒绝**——不做"取近似"、不做隐式假设。
    上游明确要求不得编造值；单位猜错产生的错误事实会一路流到排程与账务。
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise MappingError("NOT_A_NUMBER", f"单位换算需要数值，得到 {value!r}") from exc

    if source_unit not in {src for src, _ in UNIT_FACTORS}:
        raise MappingError("UNKNOWN_SOURCE_UNIT", f"未登记的外部单位：{source_unit!r}")
    if target_unit not in {dst for _, dst in UNIT_FACTORS}:
        raise MappingError("UNKNOWN_TARGET_UNIT", f"未登记的目标单位：{target_unit!r}")

    factor = UNIT_FACTORS.get((source_unit, target_unit))
    if factor is None:
        raise MappingError(
            "UNSUPPORTED_CONVERSION",
            f"{source_unit!r} → {target_unit!r} 不是纯倍率换算（需偏移量），未登记则拒绝",
        )
    return round(numeric * factor, 6)


def to_celsius(value: Any, *, source_unit: str) -> float:
    """温度必须单独处理：华氏→摄氏含偏移，**不能用倍率表**。"""
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise MappingError("NOT_A_NUMBER", f"温度需要数值，得到 {value!r}") from exc
    unit = source_unit.strip().lower()
    if unit in ("c", "celsius", "℃"):
        return round(numeric, 3)
    if unit in ("f", "fahrenheit", "℉"):
        return round((numeric - 32.0) * 5.0 / 9.0, 3)
    raise MappingError("UNKNOWN_TEMPERATURE_UNIT", f"未登记的温度单位：{source_unit!r}")


def to_str(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise MappingError("EMPTY_VALUE", "值不能为空")
    return text


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise MappingError("NOT_AN_INTEGER", f"布尔值不是整数：{value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise MappingError("NOT_AN_INTEGER", f"不是整数：{value!r}")
    return int(text)


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def to_identifier(value: Any) -> str:
    """契约 `Identifier`：不得含空格、不得以符号开头。"""
    text = to_str(value)
    if not IDENTIFIER_RE.match(text):
        raise MappingError(
            "NOT_AN_IDENTIFIER",
            f"{text!r} 不符合契约 Identifier（禁止空格与自由文本）",
        )
    return text


# ==========================================================================
# 字段映射声明
# ==========================================================================


@dataclass(frozen=True)
class FieldMapping:
    """一条字段映射。

    - `external`：外部记录里的点分路径（`header.order_no`）；
    - `internal`：目标字段名；
    - `convert`：转换器；缺省即"原样取值"；
    - `required`：缺了是**拒绝**还是列入 `unmapped`。
    """

    external: str
    internal: str
    convert: Callable[..., Any] | None = None
    required: bool = True
    note: str = ""


@dataclass(frozen=True)
class SystemMapping:
    """一个外部系统的映射契约。

    `status` 必须是 `mock` 或 `待冻结`——**不允许**在这种声明里写"已按生产口径"，
    因为生产字段/单位/时区属外部依赖（上游 §16.1）。
    """

    system: str
    source_system: str
    status: str
    fields: tuple[FieldMapping, ...]
    payload_fields: tuple[FieldMapping, ...] = ()
    note: str = ""

    def declared_external_paths(self) -> tuple[str, ...]:
        return tuple(item.external for item in self.fields) + tuple(
            item.external for item in self.payload_fields
        )


def _dig(record: Mapping[str, Any], path: str) -> Any:
    """按点分路径取值；中途缺失返回 `_MISSING` 哨兵（与"值为 None"区分开）。"""
    current: Any = record
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MISSING>"


_MISSING = _Missing()


# ==========================================================================
# 归一化结果
# ==========================================================================


@dataclass
class NormalizedEvent:
    """一次外部事件的归一化结果。

    `envelope` 可直接交给 `services/_kernel/events.py` 的 outbox/ inbox；
    `unmapped` / `assumptions` 让"哪些没有生产口径"始终可见，
    而不是隐没在代码里。
    """

    system: str
    envelope: dict[str, Any]
    unmapped: dict[str, str] = field(default_factory=dict)
    assumptions: dict[str, str] = field(default_factory=dict)
    status: str = "mock"

    @property
    def payload(self) -> dict[str, Any]:
        return self.envelope["payload"]


def payload_hash(payload: Mapping[str, Any]) -> str:
    """最小化 payload 的可信 hash（契约要求 `payload_hash` 非空）。

    用规范化 JSON（键排序）算，保证同一内容得到同一 hash。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def split_envelope_fields(mapping: SystemMapping) -> tuple[tuple[FieldMapping, ...], tuple[FieldMapping, ...]]:
    """把映射分成「信封字段」与「payload 字段」两部分。"""
    return mapping.fields, mapping.payload_fields


def missing_external_paths(
    mapping: SystemMapping, record: Mapping[str, Any]
) -> list[str]:
    """列出该记录里**缺失的必填外部字段**（诊断用，也用于预检）。"""
    return [
        item.external
        for item in mapping.fields + mapping.payload_fields
        if item.required and _dig(record, item.external) is _MISSING
    ]


# 供实现方使用的协议（保持能力面最小）
class EventAdapter(Protocol):
    """外部系统适配器：只做"把一条原生记录翻译成一条 V3 事件"。"""

    mapping: SystemMapping

    def normalize(self, record: Mapping[str, Any], *, factory_timezone: str | None = None) -> NormalizedEvent:
        ...


# ==========================================================================
# 公共归一化实现
# ==========================================================================

_ENVELOPE_FIELDS = (
    "event_id",
    "event_type",
    "schema_version",
    "tenant_id",
    "factory_id",
    "source_system",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "occurred_at",
    "received_at",
    "trace_id",
    "causation_id",
    "correlation_id",
    "data_class",
)


def normalize_event(
    mapping: SystemMapping,
    record: Mapping[str, Any],
    *,
    factory_timezone: str | None = None,
    received_at: str | None = None,
) -> NormalizedEvent:
    """把一条外部记录翻译成 V3 事件。

    **缺必填字段 ⇒ 直接拒绝**，不补默认值（上游 §16.1：不编造生产值）。
    非必填字段缺失会出现在 `unmapped` 里，保持"哪些没有口径"可见。

    `received_at` 由调用方传入（缓存/批处理时"收到时刻"是外部事实，
    不是本次函数调用的时刻——自己取 `now()` 会让重放产生不同结果）。
    """
    if mapping.status not in ("mock", "待冻结"):
        raise MappingError(
            "INVALID_MAPPING_STATUS",
            f"映射状态必须是 mock 或 待冻结（当前 {mapping.status!r}）——"
            "生产口径属外部依赖，未冻结前不得声明为已定",
        )

    missing = missing_external_paths(mapping, record)
    if missing:
        raise MappingError(
            "MISSING_REQUIRED_FIELD",
            f"{mapping.system} 记录缺少必填字段：{', '.join(missing)}"
            "（拒绝而非补默认值——补出来的值是编造的）",
            external_field=missing[0],
        )

    envelope: dict[str, Any] = {
        "source_system": mapping.source_system,
        "schema_version": "1.0.0",
        "received_at": received_at or _now_z(),
        "correlation_id": mapping.source_system,
    }
    unmapped: dict[str, str] = {}
    assumptions: dict[str, str] = {}

    for item in mapping.fields:
        raw = _dig(record, item.external)
        if raw is _MISSING:
            unmapped[item.internal] = f"外部字段 {item.external} 缺失（非必填），未提供生产口径"
            continue
        try:
            envelope[item.internal] = _apply(item, raw, factory_timezone=factory_timezone)
        except MappingError as error:
            # 补上外部字段名，让报错能直接定位到源记录的哪一项
            raise MappingError(
                error.code,
                f"{mapping.system}.{item.external} → {item.internal}: {error.message}",
                external_field=item.external,
            ) from error

    payload: dict[str, Any] = {}
    for item in mapping.payload_fields:
        raw = _dig(record, item.external)
        if raw is _MISSING:
            unmapped[f"payload.{item.internal}"] = (
                f"外部字段 {item.external} 缺失（非必填），未提供生产口径"
            )
            continue
        try:
            payload[item.internal] = _apply(item, raw, factory_timezone=factory_timezone)
        except MappingError as error:
            raise MappingError(
                error.code,
                f"{mapping.system}.{item.external} → payload.{item.internal}: {error.message}",
                external_field=item.external,
            ) from error

    # 契约要求 payload 至少含 `payload_ref` 与 `payload_hash`。
    #
    # **哈希不要在这里定稿**：适配器可能随后还要做 payload 变换
    # （WMS 的 kg→g、PMC 的 min→s），此时算的哈希会与**最终内容不符**——
    # 账实对账时无法自证，而且**不会有任何报错**。
    # 实测踩过：这条由 `test_result_payload_hash_matches_recomputation` 抓出。
    # 所以这里只补 `payload_ref`，哈希由 `seal_payload()` 在全部变换后统一封。
    payload.setdefault(
        "payload_ref",
        str(envelope.get("aggregate_id") or "payload-ref-missing"),
    )

    for key in ("causation_id",):
        envelope.setdefault(key, str(envelope.get("aggregate_id") or "causation-missing"))

    # `trace_id` 是契约必填。外部系统常常没有它——此时**从外部权威标识派生**
    # （而不是生成随机值）：同一条外部记录重放必须得到同一个 trace_id，
    # 否则取证链路会被重放污染。
    if not envelope.get("trace_id"):
        envelope["trace_id"] = f"trace-{mapping.system}-{envelope.get('aggregate_id', 'unknown')}"

    # payload 是信封的必填组成部分，且必须是契约允许的**最小化**形状
    envelope["payload"] = payload

    unknown = sorted(set(envelope) - set(_ENVELOPE_FIELDS) - {"payload"})
    if unknown:
        raise MappingError(
            "UNKNOWN_ENVELOPE_FIELD",
            f"映射产出了契约外的信封字段：{unknown}（契约 additionalProperties=false 之外的多余字段会被拒绝）",
        )

    result = NormalizedEvent(
        system=mapping.system,
        envelope=envelope,
        unmapped=unmapped,
        assumptions=assumptions,
        status=mapping.status,
    )
    return seal_payload(result)


def seal_payload(event: "NormalizedEvent") -> "NormalizedEvent":
    """在**所有 payload 变换之后**封存 `payload_hash`。

    适配器（如 WMS/PMC）会在 `normalize_event` 返回后再做单位换算，
    因此哈希必须在最后一步统一计算——否则哈希描述的不是最终内容。
    """
    payload = event.envelope["payload"]
    payload["payload_hash"] = payload_hash(
        {key: value for key, value in payload.items() if key != "payload_hash"}
    )
    return event


def _apply(item: FieldMapping, raw: Any, *, factory_timezone: str | None) -> Any:
    """执行一条映射的转换（把需要的上下文显式传给转换器）。"""
    convert = item.convert
    if convert is None:
        return raw
    if convert is to_utc:
        return convert(raw, factory_timezone=factory_timezone)
    return convert(raw)


def _now_z() -> str:
    from datetime import datetime as _dt

    return _to_z(_dt.now(timezone.utc))


# 常用转换器的柯里化封装：让映射声明保持声明式、无 lambda 噪声
def ident() -> Callable[[Any], str]:
    return to_identifier


def utc() -> Callable[..., str]:
    return to_utc


def integer() -> Callable[[Any], int]:
    return to_int


def text() -> Callable[[Any], str]:
    return to_str


def unit(source_unit: str, target_unit: str) -> Callable[[Any], float]:
    """带单位归一的数量转换（`unit("cm","mm")`）。"""

    def _convert(value: Any) -> float:
        return to_unit(value, source_unit=source_unit, target_unit=target_unit)

    return _convert


def temperature(source_unit: str) -> Callable[[Any], float]:
    """温度转换（**含偏移**，与倍率表分开）。"""

    def _convert(value: Any) -> float:
        return to_celsius(value, source_unit=source_unit)

    return _convert
