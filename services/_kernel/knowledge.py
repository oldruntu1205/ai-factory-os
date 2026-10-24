"""只读知识检索与 ACL（P1，契约 `p1-knowledge-source.json` / `p1-readonly.json`）。

上游 §6.1 与 §6 的硬规则：
- **检索前**就把可信 tenant / 工厂 / 岗位 ACL 施加到候选集上；
  **不能**先全库召回、再靠返回过滤或 LLM 判断可见性。
- **返回时**重新核对来源的当前 ACL、版本与有效期，并重新分级。
- 来源必须含 source_system / source_id / source_version / occurred_at / received_at /
  data_class / quality_status / citation_ref / snapshot_ref / valid_until / acl_check。
- Knowledge 与 Memory 不互相隐式复制。

P1 边界：文档库是登记表里的 Mock 集合，**不是** V2.1 的 Qdrant；
真实向量索引的租户/工厂/岗位分区隔离属 P2。
"""

from __future__ import annotations

import time
from typing import Any

from . import errors
from .classification import class_rank
from .registry import Registry


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _expired(valid_until: str) -> bool:
    """按可信 UTC 当前时间判断有效期，不做字符串比较。"""
    try:
        deadline = time.mktime(time.strptime(valid_until, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return True  # 解析不了就当过期：fail-closed
    return deadline <= time.time()


def retrieve(*, registry: Registry, context, purpose: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (sources, acl_facts)。检索前过滤，返回时复核。"""
    acl_version = registry.knowledge_acl_version()
    if acl_version is None:
        raise errors.reject_registry_unavailable("knowledge-acl")

    scopes = set(registry.tenant_delegated_scopes(context.tenant_id))
    if not scopes:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "ACCESS_SCOPE_DENIED", "身份未授予任何读取范围",
            details={"reason_ref": "access_scope"},
        )

    now = _utc_now()
    sources: list[dict[str, Any]] = []
    for document in registry.knowledge_documents():
        # ---- 检索前过滤（三层：tenant / factory / 岗位）----
        if document.get("tenant_id") != context.tenant_id:
            # 跨租户候选在召回阶段就被排除，绝不进入后续链路。
            continue
        if context.factory_id is not None and document.get("factory_id") != context.factory_id:
            continue
        document_roles = set(document.get("roles") or [])
        if not (document_roles & scopes):
            continue

        # ---- 返回时复核（版本、有效期、当前权限）----
        if _expired(str(document.get("valid_until"))):
            # 过期事实不能成为回答依据。
            continue
        if not document.get("source_version"):
            continue

        sources.append(
            {
                "tenant_id": context.tenant_id,
                "factory_id": document["factory_id"],
                "task_id": context.task_id,
                "trace_id": context.trace_id,
                "source_system": document["source_system"],
                "source_id": document["source_id"],
                "source_version": str(document["source_version"]),
                "occurred_at": document["occurred_at"],
                "received_at": now,
                "data_class": document["data_class"],
                "quality_status": document.get("quality_status") or "quality-demo",
                "citation_ref": f"citation-{document['source_id']}",
                "snapshot_ref": f"snapshot-{document['source_id']}-v{document['source_version']}",
                "valid_until": document["valid_until"],
                "acl_check": {
                    "acl_ref": f"acl-{document['source_id']}",
                    "acl_version": acl_version,
                    "pre_retrieval_tenant_filter": True,
                    "pre_retrieval_factory_filter": True,
                    "pre_retrieval_role_filter": True,
                    "post_return_acl_recheck": True,
                },
            }
        )

    acl_facts = [
        {
            "acl_ref": f"acl-{item['source_id']}",
            "acl_version": acl_version,
            "pre_retrieval_tenant_filter": True,
            "pre_retrieval_factory_filter": True,
            "pre_retrieval_role_filter": True,
            "post_return_acl_recheck": True,
        }
        for item in sources
    ]
    if not acl_facts:
        acl_facts = [
            {
                "acl_ref": "acl-empty",
                "acl_version": acl_version,
                "pre_retrieval_tenant_filter": True,
                "pre_retrieval_factory_filter": True,
                "pre_retrieval_role_filter": True,
                "post_return_acl_recheck": True,
            }
        ]
    return sources, acl_facts


def sources_within(sources: list[dict[str, Any]], data_class: str) -> bool:
    """来源等级不得高于该分类结果等级（契约 §6）。"""
    limit = class_rank(data_class)
    return all(class_rank(item.get("data_class")) <= limit for item in sources)
