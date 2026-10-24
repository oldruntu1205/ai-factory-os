"""事务边界套件的夹具（上游 §15 `tests/transactions/`）。

复用 P0 的真实进程集群——不另起一套，否则结论不可比。
"""

from __future__ import annotations

from tests.p0_runtime.conftest import (  # noqa: F401 - 供测试直接引用
    REPO_ROOT,
    Cluster,
    context_body,
    http,
    schema_binding,
    tool_request_body,
)
from tests.p15_runtime.conftest import cluster, evidence_path  # noqa: F401

#: 受控执行/服务间调用的身份头。与 `tests/p0_runtime` 保持一致：
#: 契约要求 `X-V3-Identity` 是**服务身份**，审批/执行的归属另由 Context 表达。
IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}

__all__ = [
    "REPO_ROOT",
    "Cluster",
    "IDENTITY",
    "context_body",
    "http",
    "schema_binding",
    "tool_request_body",
    "cluster",
    "evidence_path",
]
