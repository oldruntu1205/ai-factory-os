"""跨租户隔离套件的夹具（上游 §15 `tests/isolation/`）。

复用 P0 的真实进程集群与 P1.5 的证据隔离——**不另起一套夹具**，
否则"隔离用例跑在另一套环境上"这件事本身就会让结论不可比。
"""

from __future__ import annotations

from tests.p0_runtime.conftest import (  # noqa: F401 - 供测试直接引用
    REPO_ROOT,
    Cluster,
    context_body,
    http,
)
from tests.p15_runtime.conftest import cluster, evidence_path  # noqa: F401

__all__ = [
    "REPO_ROOT",
    "Cluster",
    "context_body",
    "http",
    "cluster",
    "evidence_path",
]
