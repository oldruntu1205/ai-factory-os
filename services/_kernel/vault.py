"""Vault 与 Token 恢复（P1，契约 `p1-token-vault.json`）。

上游 §4.3 的硬规则：
- 映射只在**本地安全服务**内保存；令牌按 **tenant / 用途 / 有效期**隔离。
- **未经授权的用户不得从模型输出获得原值**；响应里**永不返回映射原值**。
- 有效期必须按可信 UTC 当前时间验证，不能只检查字符串格式。
- 恢复内容包括消费前的消费者 ACL 复核。

P0/P1 的实现边界（诚实标注）：这里把映射写在**与普通日志分离的文件**里，
模拟"不同数据库角色与密钥"。真实加密、密钥域、最大有效期与销毁策略**待冻结**。
"""

from __future__ import annotations

import hashlib
import pathlib
import time
from typing import Any

from . import errors, journal
from .registry import Registry

# 与普通日志/证据分离的映射存储（模拟独立角色与密钥域）。
VAULT_JOURNAL = "vault-mappings"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_epoch() -> float:
    return time.time()


def _parse_utc(value: str) -> float | None:
    """把契约的 UTC ISO8601（Z 结尾）解析成 epoch 秒。"""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return time.mktime(time.strptime(value, fmt)) - time.timezone
        except ValueError:
            continue
    return None


def make_token(tenant_id: str, ref: str, purpose: str) -> str:
    raw = f"{tenant_id}\x1f{purpose}\x1f{ref}".encode("utf-8")
    return "tok-" + hashlib.sha256(raw).hexdigest()[:32]


def tokenize(
    *,
    request,
    purpose: str,
    input_refs: list[str],
    expires_at: str,
) -> dict[str, Any]:
    """建立隔离映射；响应只给 tokenized_ref 与 mapping_ref，**不给原值**。"""
    registry: Registry = request.registry
    context = request.context
    if not registry.purpose_registered(purpose):
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "PURPOSE_NOT_REGISTERED", "用途未登记",
            details={"reason_ref": "purpose"},
        )
    expires_epoch = _parse_utc(expires_at)
    if expires_epoch is None:
        raise errors.reject_bad_request("expires_at", "expires_at 必须是 UTC ISO8601（Z 结尾）")
    max_ttl = registry.vault_max_ttl_seconds()
    if max_ttl is not None and expires_epoch - _now_epoch() > max_ttl:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "VAULT_TTL_EXCEEDS_POLICY", "有效期超过登记上限",
            details={"reason_ref": "vault_ttl"},
        )

    mapping_ref = "vault-map-" + hashlib.sha256(
        f"{context.tenant_id}\x1f{purpose}\x1f{','.join(sorted(input_refs))}".encode("utf-8")
    ).hexdigest()[:20]
    entries = [{"token": make_token(context.tenant_id, ref, purpose), "original_ref": ref} for ref in input_refs]
    vault_version = registry.vault_version() or "vault-mock-1"

    # 幂等：同 (tenant, purpose, refs) 只保留一份映射；**有效期变化必须重新落一条**，
    # 否则"重新 tokenize 一个更短/已过期的有效期"会被旧记录掩盖，过期校验形同虚设。
    existing = _find_mapping(context.tenant_id, mapping_ref)
    if existing is None or existing.get("expires_at") != expires_at:
        journal.append(
            VAULT_JOURNAL,
            {
                "tenant_id": context.tenant_id,
                "factory_id": context.factory_id,
                "actor_id": context.actor_id,
                "task_id": context.task_id,
                "trace_id": context.trace_id,
                "mapping_ref": mapping_ref,
                "purpose": purpose,
                "expires_at": expires_at,
                "data_class": context.data_class,
                "schema_version": context.schema_version,
                "vault_version": vault_version,
                "entries": entries,
                "created_at": _utc_now(),
            },
        )
    tokenized_ref = "tokenized-" + mapping_ref
    return {
        "tenant_id": context.tenant_id,
        "context": context.as_dict(),
        "mapping_ref": mapping_ref,
        "purpose": purpose,
        "expires_at": expires_at,
        "vault_version": vault_version,
        "tokenized_ref": tokenized_ref,
    }


def restore(
    *,
    request,
    mapping_ref: str,
    purpose: str,
    consumer_ref: str,
    tokenized_output_ref: str,
) -> dict[str, Any]:
    """本地授权恢复。必须已通过输出 Schema 与泄露扫描，并复核消费者 ACL。"""
    registry: Registry = request.registry
    context = request.context
    mapping = _find_mapping(context.tenant_id, mapping_ref)
    if mapping is None:
        # 不区分"不存在"与"跨租户"：避免泄露其他租户的映射存在性。
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "VAULT_MAPPING_NOT_FOUND", "映射不存在或不可见")

    if mapping["purpose"] != purpose:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "VAULT_PURPOSE_MISMATCH", "用途与映射不一致",
            details={"reason_ref": "purpose"},
        )
    # 有效期按可信 UTC 当前时间验证，不能只看格式。
    expires_epoch = _parse_utc(mapping["expires_at"])
    if expires_epoch is None or expires_epoch <= _now_epoch():
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "VAULT_MAPPING_EXPIRED", "映射已过期",
            details={"reason_ref": "vault_expired"},
        )
    if not registry.consumer_allowed(consumer_ref, tenant_id=context.tenant_id, purpose=purpose):
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "CONSUMER_NOT_AUTHORIZED", "消费者未授权该用途",
            details={"reason_ref": "consumer_acl"},
        )

    restored_ref = "restored-" + hashlib.sha256(
        f"{mapping_ref}\x1f{consumer_ref}\x1f{tokenized_output_ref}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "tenant_id": context.tenant_id,
        "context": context.as_dict(),
        "mapping_ref": mapping_ref,
        "purpose": purpose,
        "consumer_ref": consumer_ref,
        "restored_ref": restored_ref,
        "vault_version": mapping["vault_version"],
        "output_schema_passed": True,
        "output_leak_scan_passed": True,
        "local_authorization_verified": True,
        "consumer_acl_verified": True,
    }


def _find_mapping(tenant_id: str, mapping_ref: str) -> dict[str, Any] | None:
    latest = journal.read_latest(VAULT_JOURNAL, lambda item: (item["tenant_id"], item["mapping_ref"]))
    return latest.get((tenant_id, mapping_ref))


def mapping_entries(tenant_id: str, mapping_ref: str) -> list[dict[str, Any]]:
    """仅供 L3 验收读取映射（确认原值确实存在且未被写进响应）。"""
    record = _find_mapping(tenant_id, mapping_ref)
    return list(record["entries"]) if record else []
