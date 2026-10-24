"""P1 运行验收夹具。

复用 P0 的真实进程集群（`tests/p0_runtime/conftest.py`），只把证据与状态换成本阶段的文件。
不复制进程管理代码——同一套夹具跑两个阶段的验收，避免"两份实现各自漂移"。
"""

from __future__ import annotations

import pathlib

import pytest

from tests.p0_runtime.conftest import (  # noqa: F401 - 供测试直接引用
    REPO_ROOT,
    SERVICE_IDS,
    Cluster,
    ServiceProcess,
    cleanup_pycache,
    context_body,
    http,
    kill_leftovers,
    pick_port,
    schema_binding,
    tool_request_body,
    venv_python,
)

SERVICES_PKG = REPO_ROOT / "services"

# P1 新增接口所属的 owner（用于"只暴露自己接口"的断言）
P1_OWNED_PATHS = {
    "security-gateway": (
        "/internal/v1/classifications/evaluate",
        "/internal/v1/egress/evaluate",
        "/internal/v1/token-vault/tokenize",
        "/internal/v1/token-vault/restore",
    ),
    "control-plane": ("/api/v1/tasks/task-probe/knowledge-answer",),
    "manufacturing-core": ("/internal/v1/knowledge/query",),
}


def p1_evidence_path() -> pathlib.Path:
    return SERVICES_PKG / ".evidence" / "p1-runtime.jsonl"


@pytest.fixture(scope="session")
def evidence_path() -> pathlib.Path:
    """P1 会话级证据与状态文件。清掉上一轮的全部 journal，避免串场。"""
    path = p1_evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    for name in (
        "commands",
        "receipts",
        "tasks",
        "approvals",
        "runs",
        "answers",
        "egress-audit",
        "vault-mappings",
    ):
        stale = SERVICES_PKG / ".evidence" / "state" / f"{name}.jsonl"
        if stale.exists():
            stale.unlink()
    return path


@pytest.fixture(scope="session")
def cluster(evidence_path: pathlib.Path):
    instance = Cluster(evidence_path=evidence_path)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()
        kill_leftovers()
        cleanup_pycache()
