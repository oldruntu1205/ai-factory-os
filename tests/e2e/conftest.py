"""端到端验收夹具（上游 §15 `tests/e2e/`）。

复用 P0 的真实进程集群、P1.5 的状态/证据隔离，以及 P2 的运行期工具。
"""

from __future__ import annotations

from tests.p0_runtime.conftest import (  # noqa: F401 - 供测试直接引用
    REPO_ROOT,
    SERVICE_IDS,
    SERVICE_MODULES,
    Cluster,
    cleanup_pycache,
    context_body,
    http,
    kill_leftovers,
    pick_port,
    port_env_name,
    schema_binding,
    tool_request_body,
    venv_python,
)
from tests.p15_runtime.conftest import (  # noqa: F401
    IDENTITY,
    deliver,
    envelope,
    p15_body,
    p15_context,
    state_file,
)
from tests.p15_runtime.conftest import cluster, evidence_path, read_jsonl  # noqa: F401
