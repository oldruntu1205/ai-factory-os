"""P4 运行验收夹具：复用 P0 真实进程集群与 P1.5 的状态/证据隔离。

与 P3 同样的约定：**不裁剪服务集合**（`Cluster` 一次起全部运行单元），
因为"只起两个进程"的裁剪会掩盖跨 owner 的可见性问题——
而 P4 恰恰要求"中心与现场各自的存储/权限边界清楚"。
"""

from __future__ import annotations

from tests.p0_runtime.conftest import (  # noqa: F401 - 供测试直接引用
    REPO_ROOT,
    SERVICE_IDS,
    Cluster,
    cleanup_pycache,
    context_body,
    http,
    kill_leftovers,
    schema_binding,
    tool_request_body,
    venv_python,
)
from tests.p15_runtime.conftest import (  # noqa: F401
    IDENTITY,
    cluster,
    evidence_path,
    p15_body,
    p15_context,
    read_jsonl,
    state_file,
)
