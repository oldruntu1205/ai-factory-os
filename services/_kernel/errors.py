"""统一错误体（契约 `packages/contracts/schemas/error.json`）。

错误体固定为 5 个键，全部必填：
    code / message / trace_id / retryable / details

`details` 是**封闭对象**，只允许：denied_field / object_ref / expected_version /
actual_version / reason_ref。其中 `denied_field` 又是四值封闭枚举。任何其他键
（包括实现者顺手加的 `field`）都会被丢弃——这就是契约要求的"脱敏且封闭"。

约束（契约原文）：
- 401/403/404 不得泄露跨租户对象是否存在。
- `retryable` 不能被 SDK 解读为无条件重发副作用。
"""

from __future__ import annotations

from typing import Any

# 契约 §6 错误码表里冻结的 HTTP 语义；业务 code 目录待冻结，这里只用结构性 code。
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_TOO_MANY = 429
HTTP_UNAVAILABLE = 503

IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"

# 与 error.json#/$defs/Details 的键集合逐字一致
_DETAIL_KEYS = frozenset({"denied_field", "object_ref", "expected_version", "actual_version", "reason_ref"})
# 与 error.json 里 denied_field 的 enum 逐字一致
_DENIED_FIELDS = frozenset({"tenant_id", "tool_id", "data_class", "model_backend"})


class V3Error(Exception):
    """服务层统一抛出的错误，由 HTTP 层转换为契约错误体。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = _sanitize(details)


def _sanitize(details: dict[str, Any] | None) -> dict[str, Any]:
    """只保留契约允许的键；非法键静默丢弃，避免契约外字段泄漏到响应里。"""
    if not details:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in details.items():
        if key not in _DETAIL_KEYS or value is None:
            continue
        if key == "denied_field":
            # 非枚举值一律丢弃：不能把任意字段名当诊断回给调用方。
            if value in _DENIED_FIELDS:
                cleaned[key] = value
            continue
        if key in ("expected_version", "actual_version"):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                cleaned[key] = value
            continue
        if isinstance(value, str) and value:
            cleaned[key] = value[:120]
    return cleaned


def error_body(error: V3Error, trace_id: str) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "trace_id": trace_id,
        "retryable": error.retryable,
        "details": dict(error.details),
    }


def reject_unknown_tenant() -> V3Error:
    # 不区分"不存在"与"无权"，也不指出是哪个字段不一致，避免泄露跨租户对象存在性。
    return V3Error(
        HTTP_FORBIDDEN,
        "TENANT_NOT_REGISTERED",
        "租户未登记或不可用",
        details={"denied_field": "tenant_id"},
    )


def reject_unknown_tool(tool_id: str) -> V3Error:
    return V3Error(
        HTTP_FORBIDDEN,
        "TOOL_NOT_REGISTERED",
        "工具未登记或不可用",
        details={"denied_field": "tool_id", "object_ref": tool_id},
    )


def reject_unknown_data_class(data_class: str | None) -> V3Error:
    details: dict[str, Any] = {"denied_field": "data_class"}
    if isinstance(data_class, str) and data_class:
        details["reason_ref"] = data_class
    return V3Error(
        HTTP_FORBIDDEN,
        "DATA_CLASS_NOT_ALLOWED",
        "数据等级未知或超出该租户允许范围",
        details=details,
    )


def reject_registry_unavailable(reason: str) -> V3Error:
    return V3Error(
        HTTP_UNAVAILABLE,
        "REGISTRY_UNAVAILABLE",
        "登记表或策略不可用，拒绝新动作",
        retryable=False,
        details={"reason_ref": reason[:120]},
    )


def reject_sensitive_nonlocal() -> V3Error:
    return V3Error(
        HTTP_FORBIDDEN,
        "SENSITIVE_NONLOCAL_MODEL_FORBIDDEN",
        "C3/C4 数据只允许本地已批准模型后端",
        details={"denied_field": "model_backend"},
    )


def reject_unauthenticated() -> V3Error:
    return V3Error(HTTP_UNAUTHORIZED, "AUTHENTICATION_REQUIRED", "缺少或无效的服务身份凭证")


def reject_bad_request(field: str, reason: str) -> V3Error:
    """结构/参数错误。

    契约把 400/422 归为"结构或参数错误"，但 details 只允许四值 denied_field，
    因此普通字段名改放 object_ref（也是 Identifier），不硬塞进 denied_field。
    """
    details: dict[str, Any] = {}
    if field in _DENIED_FIELDS:
        details["denied_field"] = field
    elif field:
        details["object_ref"] = field if field[0].isalnum() else "requestBody"
    return V3Error(HTTP_BAD_REQUEST, "REQUEST_INVALID", reason, details=details)
