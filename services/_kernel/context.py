"""公共 Context 校验（契约 `packages/contracts/schemas/common-context.json`）。

契约要点：
- 九个键**全部显式必填**；不适用时显式 `null`，不允许缺键或由默认值补全。
- `tenant_id` 来自可信认证，输入值必须与认证主体一致；未知 tenant 拒绝。
- `data_class` 是封闭枚举，不得缺省或降级；未知等级拒绝。
- 时间是 UTC ISO8601，只接受 `Z` 结尾；不接受本地时区。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from . import errors
from .registry import Registry

# 与契约 $defs/Identifier 的 pattern 一致
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
# 与契约 $defs/UtcTimestamp 的 pattern 一致
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

CONTEXT_KEYS = (
    "tenant_id",
    "factory_id",
    "actor_id",
    "employee_id",
    "task_id",
    "trace_id",
    "data_class",
    "schema_version",
    "deadline_at",
)
# 这些键允许显式 null，其余必须是合法 Identifier
NULLABLE_CONTEXT_KEYS = ("factory_id", "employee_id", "task_id")


class Context:
    """已校验的公共 Context。字段原样保留，不补默认值。"""

    __slots__ = CONTEXT_KEYS

    def __init__(self, values: dict[str, Any]) -> None:
        for key in CONTEXT_KEYS:
            setattr(self, key, values[key])

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in CONTEXT_KEYS}

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"Context(tenant_id={self.tenant_id!r}, task_id={self.task_id!r}, trace_id={self.trace_id!r})"


def extract(payload: Any, *, source: str) -> dict[str, Any]:
    """从请求体（POST）或 X-Context 头（GET）取出 Context 原始字典。

    缺键即 400：契约要求所有键显式提供，不能靠默认值补齐。
    """
    if not isinstance(payload, dict):
        raise errors.V3Error(
            errors.HTTP_BAD_REQUEST, "REQUEST_INVALID", f"{source} 必须是 JSON object"
        )
    missing = [key for key in CONTEXT_KEYS if key not in payload]
    if missing:
        raise errors.V3Error(
            errors.HTTP_BAD_REQUEST,
            "REQUEST_INVALID",
            "公共 Context 的键必须全部显式提供",
            details={"field": ",".join(missing)},
        )
    return {key: payload[key] for key in CONTEXT_KEYS}


def _is_real_utc_timestamp(value: str) -> bool:
    """UTC 时间戳校验：既查格式，也查**日历上真实存在**。

    只查正则会把 `2026-02-30T00:00:00Z`（2 月没有 30 日）当合法输入放过去，
    这属于"格式对但事实错"，必须拒绝。
    """
    if not UTC_RE.match(value):
        return False
    try:
        dt.datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z" if "." not in value else "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return False
    return True


def validate(raw: dict[str, Any], registry: Registry, *, identity_tenant: str | None = None) -> Context:
    """逐字段校验并核实租户归属；任何未知项一律拒绝。

    `identity_tenant`：来自**可信认证**的租户。契约要求
    「tenant 从可信认证得出，不信任请求自填 tenant」——因此两者必须一致；
    不一致时拒绝（不透露请求自称的租户是否真实存在）。
    """
    for key in CONTEXT_KEYS:
        value = raw[key]
        if value is None:
            if key not in NULLABLE_CONTEXT_KEYS:
                raise errors.reject_bad_request(key, f"{key} 不允许为空")
            continue
        if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
            raise errors.reject_bad_request(key, f"{key} 不是合法标识")

    if not _is_real_utc_timestamp(str(raw["deadline_at"])):
        raise errors.reject_bad_request("deadline_at", "deadline_at 必须是真实存在的 UTC ISO8601 时刻（Z 结尾）")

    data_class = raw["data_class"]
    if not registry.known_data_class(data_class):
        raise errors.reject_unknown_data_class(data_class)

    tenant_id = raw["tenant_id"]
    if registry.tenant(tenant_id) is None:
        raise errors.reject_unknown_tenant()

    # 请求自填 tenant 必须与可信身份的 tenant 一致（不能靠自填取得他人数据域）。
    if identity_tenant is not None and tenant_id != identity_tenant:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN,
            "TENANT_IDENTITY_MISMATCH",
            "请求租户与可信身份不一致",
            details={"denied_field": "tenant_id"},
        )

    max_class = registry.tenant_max_data_class(tenant_id)
    if max_class is None:
        # 登记表没给出该租户允许的最高等级 → 无法判定，拒绝而不是放行。
        raise errors.reject_unknown_data_class(data_class)
    if _class_rank(data_class) > _class_rank(max_class):
        raise errors.reject_unknown_data_class(data_class)

    if not registry.tenant_allows(
        tenant_id,
        factory_id=raw["factory_id"],
        actor_id=raw["actor_id"],
        employee_id=raw["employee_id"],
    ):
        # 身份与自填 tenant/factory/actor/employee 不一致 → 拒绝，且不透露哪个字段错。
        raise errors.reject_unknown_tenant()

    return Context(raw)


def _class_rank(data_class: str) -> int:
    order = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}
    return order.get(data_class, 99)  # 未知等级排到最后，确保被拒


def merge_scope(outer: Context, inner: dict[str, Any]) -> None:
    """嵌套对象若自带 tenant_id，必须与外层完全一致（契约：match_nested_tenant）。"""
    nested = inner.get("tenant_id")
    if nested is not None and nested != outer.tenant_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN,
            "NESTED_TENANT_MISMATCH",
            "嵌套对象租户与外层不一致",
            details={"field": "tenant_id"},
        )
