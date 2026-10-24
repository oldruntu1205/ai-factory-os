"""出域尝试审计（P1，契约 `p1-egress-audit.json`）。

上游 §4.3 最后一步与 §14.2 的硬规则：
- 审计**追加**写入；失败尝试同样要留痕（否则"没有成功记录"无法自证）。
- 只允许**安全摘要引用、引用、哈希和版本**；禁止敏感原文、原始输出、令牌值、恢复值与密钥。
- 审计存储故障**不得**转成安全绕行（此处返回失败标记，由调用方决定拒绝）。

P1 边界：这里落 JSONL，模拟 append-only；真实追加存储、outbox 补送与保留策略待冻结。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import journal

AUDIT_JOURNAL = "egress-audit"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def summary_hash(value: Any) -> str:
    """只留规范化摘要：任何情况下都不把原文写进审计。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record(
    *,
    context,
    audit_ref: str,
    classification_known: bool,
    highest_data_class: str | None,
    policy_version: str,
    classification_rule_version: str,
    model_version: str | None,
    requested_backend: str | None,
    invocation_performed: bool,
    invocation_succeeded: bool,
    output_released: bool,
    stage_refs: list[dict[str, Any]],
    evidence_refs: list[str] | None = None,
    input_hash: str | None = None,
    output_hash: str | None = None,
    summary_ref: str | None = None,
) -> dict[str, Any]:
    """追加一条出域审计事实。

    `invocation_performed=false` 时，契约要求 succeeded 与 output_released 也必须为 false，
    这里强制归一，避免调用方传错导致审计撒谎。
    """
    if not invocation_performed:
        invocation_succeeded = False
        output_released = False
    if not invocation_succeeded:
        output_released = False
    if not classification_known:
        highest_data_class = None
        invocation_performed = False
        invocation_succeeded = False
        output_released = False

    record_body = {
        "tenant_id": context.tenant_id,
        "factory_id": context.factory_id,
        "actor_id": context.actor_id,
        "task_id": context.task_id,
        "trace_id": context.trace_id,
        "audit_ref": audit_ref,
        "occurred_at": _utc_now(),
        "classification_known": classification_known,
        "highest_data_class": highest_data_class,
        "policy_version": policy_version,
        "classification_rule_version": classification_rule_version,
        "model_version": model_version,
        "requested_backend": requested_backend,
        "invocation_performed": invocation_performed,
        "invocation_succeeded": invocation_succeeded,
        "output_released": output_released,
        "evidence_refs": list(evidence_refs or []),
        "input_hash": input_hash,
        "output_hash": output_hash,
        "summary_ref": summary_ref,
        # 契约 p1-egress-audit.json 的 stage_refs 项目只允许 stage + evidence_ref。
        "stage_refs": [
            {"stage": item["stage"], "evidence_ref": item.get("result_ref") or "evidence-ref"}
            for item in stage_refs
        ],
    }
    journal.append(AUDIT_JOURNAL, record_body)
    return record_body


def all_records() -> list[dict[str, Any]]:
    return journal.read_all(AUDIT_JOURNAL)


def external_invocations() -> list[dict[str, Any]]:
    """所有"对外部后端实际发起调用"的审计条目（验收断言用）。

    这是 P1 完成条件「C3/C4 无出域路径」的直接证据来源：
    该列表必须**永远为空**，无论请求怎么构造。
    """
    return [
        item
        for item in all_records()
        if item.get("invocation_performed") is True
        and item.get("requested_backend") in ("enterprise_cloud", "public")
    ]
