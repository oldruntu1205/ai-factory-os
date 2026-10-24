"""P1.5 运行验收夹具：复用 P0 的真实进程集群，独立证据与状态文件。"""

from __future__ import annotations

import json
import pathlib

import pytest

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

SERVICES_PKG = REPO_ROOT / "services"
IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def p15_evidence_path() -> pathlib.Path:
    return SERVICES_PKG / ".evidence" / "p15-runtime.jsonl"


@pytest.fixture(scope="session")
def evidence_path() -> pathlib.Path:
    path = p15_evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    state = SERVICES_PKG / ".evidence" / "state"
    if state.exists():
        for item in state.rglob("*"):
            if item.is_file():
                item.unlink()
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


def p15_context(
    *,
    tenant: str = "tenant-demo",
    factory_id: str | None = "factory-demo",
    task_id: str | None = "task-demo",
    trace_id: str = "trace-demo",
    data_class: str = "C2",
) -> dict:
    return {
        "tenant_id": tenant,
        "factory_id": factory_id,
        "actor_id": "user-demo",
        "employee_id": "employee-planning",
        "task_id": task_id,
        "trace_id": trace_id,
        "data_class": data_class,
        "schema_version": "1.0.0",
        "deadline_at": "2026-10-05T00:00:00Z",
    }


def p15_body(**kwargs) -> dict:
    context = p15_context(**kwargs)
    return {"tenant_id": context["tenant_id"], "context": context}


def envelope(
    *,
    event_id: str,
    aggregate_id: str = "command-demo",
    aggregate_version: int = 0,
    event_type: str = "command.accepted",
    tenant: str = "tenant-demo",
    trace_id: str = "trace-demo",
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "1.0.0",
        "tenant_id": tenant,
        "factory_id": "factory-demo",
        "source_system": "transaction-service",
        "aggregate_type": "command",
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "occurred_at": "2026-10-04T00:00:00Z",
        "received_at": "2026-10-04T00:00:00Z",
        "trace_id": trace_id,
        "causation_id": f"cause-{aggregate_id}",
        "correlation_id": f"corr-{aggregate_id}",
        "data_class": "C2",
        "payload": {"payload_ref": f"payload-{aggregate_id}-{aggregate_version}", "payload_hash": "sha256:demo"},
    }


def deliver(cluster, *, service_id: str, evt: dict, **context_kwargs):
    return cluster.post(
        service_id,
        "/internal/v1/events",
        {**p15_body(**context_kwargs), "event": evt, "publication_evidence_ref": "evidence-demo"},
        headers=IDENTITY,
    )


def state_file(cluster, name: str) -> pathlib.Path:
    return cluster.state_dir / f"{name}.jsonl"


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
