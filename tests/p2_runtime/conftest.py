"""P2 运行期验收夹具：复用 P0 真实进程集群与 P1.5 的状态/证据隔离。"""

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
    read_jsonl,
    state_file,
)
