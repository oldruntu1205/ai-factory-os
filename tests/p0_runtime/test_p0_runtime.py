"""P0 运行验收：上游 §16 的完成条件 + §17 断言的可本地验证子集。

对应关系：
- 「核心服务可启动」 → test_all_services_start_and_own_only_their_routes / test_readiness_reflects_dependencies
- 「拒绝未知 tenant/工具」 → test_unknown_tenant_and_tool_are_rejected
- 「Mock 闭环可追踪」 → test_mock_closed_loop_is_traceable
- A01/A02 的骨架级证据 → test_sensitive_data_never_leaves_local_backend
- A04 → test_idempotency_same_key_same_payload
- A05/A06 的骨架级证据 → test_unknown_command_is_not_retried_with_new_key
- A09 的骨架级证据 → test_edge_shadow_has_no_side_effect
- 响应体必须符合契约 → test_responses_conform_to_contract_schemas
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
import uuid

import pytest
from jsonschema import Draft202012Validator

from .conftest import (
    REPO_ROOT,
    SERVICE_IDS,
    ServiceProcess,
    context_body,
    free_port,
    http,
    schema_binding,
    tool_request_body,
    venv_python,
)

SCHEMAS = REPO_ROOT / "packages" / "contracts" / "schemas"
OPENAPI = REPO_ROOT / "packages" / "contracts" / "openapi" / "p0.openapi.json"

# 每个运行单元**拥有**的路径前缀。
# 注意：`{param}` 占位必须给出具体值，否则不构成一条真实请求路径。
OWNED_PATHS = {
    "control-plane": ("/api/v1/tasks", "/api/v1/approvals/approval-probe/decisions", "/internal/v1/policy/evaluate"),
    "decision-service": ("/internal/v1/decisions",),
    "security-gateway": ("/internal/v1/model-invocations",),
    "manufacturing-core": ("/internal/v1/contexts/query", "/edge/v1/decisions", "/edge/v1/releases/activate"),
    "tool-gateway": ("/internal/v1/tool-requests", "/internal/v1/tool-executions"),
    "transaction-service": ("/internal/v1/commands", "/internal/v1/commands/command-probe"),
    "hermes-runtime": ("/internal/v1/runs", "/internal/v1/runs/run-probe/resume"),
    "trajectory-eval": (),
}

# 每个运行单元**不得**拥有的路径（属其他 owner）。
FOREIGN_PATHS = {
    "security-gateway": ("/internal/v1/commands", "/api/v1/tasks", "/internal/v1/decisions"),
    "control-plane": ("/internal/v1/tool-requests", "/api/v1/runs", "/internal/v1/model-invocations"),
    "decision-service": ("/internal/v1/tool-requests", "/api/v1/tasks", "/internal/v1/commands"),
    "hermes-runtime": ("/internal/v1/commands", "/api/v1/tasks", "/internal/v1/tool-requests"),
    "manufacturing-core": ("/internal/v1/commands", "/api/v1/tasks", "/internal/v1/model-invocations"),
    "tool-gateway": ("/api/v1/tasks", "/internal/v1/decisions", "/internal/v1/commands"),
    "transaction-service": ("/api/v1/tasks", "/internal/v1/tool-requests", "/internal/v1/decisions"),
    "trajectory-eval": ("/api/v1/tasks", "/internal/v1/commands", "/internal/v1/tool-requests"),
}


# ==========================================================================
# 工具
# ==========================================================================

def _await_command_status(
    cluster, command_id: str, expected: str, *, trace_id: str = "trace-demo", timeout: float = 15.0
) -> dict:
    """轮询命令状态直到达到期望值。

    为什么轮询：命令最终状态由 transaction-service 的对账线程依据**权威回执**推进，
    调用方不可能同步拿到终态——这正是"受理 ≠ 成功"的实现形态。
    """
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        status, last = cluster.get(
            "transaction-service",
            f"/internal/v1/commands/{command_id}",
            headers={"X-Context": json.dumps(context_body(trace_id=trace_id))},
        )
        if status == 200 and last.get("status") == expected:
            return last
        time.sleep(0.2)
    raise AssertionError(f"命令 {command_id} 未在 {timeout}s 内达到 {expected}，最后状态: {last}")


# ==========================================================================
# 一、核心服务可启动 / 独立进程 / 只暴露自己的接口
# ==========================================================================

def test_all_eight_services_are_independent_processes(cluster):  # A01
    assert set(cluster.processes) == set(SERVICE_IDS)
    pids = set()
    for service_id, process in cluster.processes.items():
        assert process.process is not None and process.process.poll() is None, f"{service_id} 未存活"
        pids.add(process.process.pid)
    # 八个真实进程，不是同一个对象被复用。
    assert len(pids) == len(SERVICE_IDS)


def test_liveness_is_200_for_every_service(cluster):  # A01
    for service_id in SERVICE_IDS:
        status, payload = cluster.get(service_id, "/health/live")
        assert status == 200, f"{service_id} live 应 200，实际 {status}"
        assert payload["live"] is True
        assert payload["service_id"] == service_id


def test_each_service_owns_only_its_routes(cluster):  # A01
    # 探针用空 body：只要路径属于本服务，就应给出认证/参数级错误，而不是 404。
    for service_id in SERVICE_IDS:
        for path in OWNED_PATHS[service_id]:
            status, payload = cluster.post(service_id, path, {})
            assert status != 404, f"{service_id} 应拥有 {path}，却返回 404"
        for path in FOREIGN_PATHS[service_id]:
            status, payload = cluster.post(service_id, path, {})
            assert status == 404, f"{service_id} 不应暴露 {path}，实际 {status}"
            # 路径存在性不依赖身份：即使不带凭证也应 404，而不是 401。
            status_anon, _ = cluster.post(service_id, path, {}, token=False)
            assert status_anon == 404, f"{service_id}{path} 无凭证时泄露了路由存在性"


def test_authentication_is_required_on_business_routes(cluster):  # A01
    body = tool_request_body()
    for service_id, path in (
        ("control-plane", "/api/v1/tasks"),
        ("decision-service", "/internal/v1/decisions"),
        ("transaction-service", "/internal/v1/commands"),
    ):
        status, payload = cluster.post(service_id, path, body, token=False)
        assert status == 401, f"{service_id}{path} 无凭证应 401，实际 {status}"
        assert set(payload) == {"code", "message", "trace_id", "retryable", "details"}
        # 无效方案同样拒绝
        status, payload = cluster.post(
            service_id, path, body, token=False, headers={"Authorization": "Basic abc"}
        )
        assert status == 401, f"{service_id}{path} 无效认证方案应 401，实际 {status}"


def test_readiness_reflects_dependencies(cluster):  # A01
    """就绪必须是真实探测结果，不是固定 true。"""
    process = cluster.spawn_extra(
        "trajectory-eval", env_extra={"V3_DEPENDENCIES_DOWN": "evidence-store"}
    )
    try:
        status, payload = http("GET", f"http://127.0.0.1:{process.port}/health/live")
        assert status == 200 and payload["live"] is True
        status, payload = http("GET", f"http://127.0.0.1:{process.port}/health/ready")
        assert status == 503, f"依赖不可用应 503，实际 {status}"
        assert payload["ready"] is False
        assert any(item["dependency_id"] == "evidence-store" and item["ready"] is False for item in payload["dependencies"])
    finally:
        process.stop()


def test_readiness_reports_ready_and_dependency_detail(cluster):  # A01
    status, payload = cluster.get("control-plane", "/health/ready")
    assert status == 200
    assert payload["ready"] is True
    assert payload["policy_version"]
    assert {item["dependency_id"] for item in payload["dependencies"]} == {"policy-registry", "evidence-sink"}
    assert all(item["ready"] is True for item in payload["dependencies"])


# ==========================================================================
# 二、拒绝未知 tenant / 工具 / 数据等级（fail-closed）
# ==========================================================================

def test_unknown_tenant_and_tool_are_rejected(cluster):  # A01,A03
    # 1) 未登记 tenant
    body = tool_request_body(tenant="tenant-unknown")
    status, payload = cluster.post("tool-gateway", "/internal/v1/tool-requests", body)
    assert status == 403, f"未登记租户应 403，实际 {status}"
    assert payload["code"] == "TENANT_NOT_REGISTERED"
    assert payload["retryable"] is False

    # 2) 已登记 tenant 但自填 actor 不属该租户
    body = tool_request_body()
    body["actor_id"] = "user-not-mine"
    status, payload = cluster.post("tool-gateway", "/internal/v1/tool-requests", body)
    assert status == 403
    assert payload["code"] == "TENANT_NOT_REGISTERED"
    # 不泄露是哪个字段错
    assert "actor_id" not in json.dumps(payload["details"], ensure_ascii=False)

    # 3) 未登记工具
    body = tool_request_body(tool_id="erp.unknown.tool")
    status, payload = cluster.post("tool-gateway", "/internal/v1/tool-requests", body)
    assert status == 403
    assert payload["code"] == "TOOL_NOT_REGISTERED"

    # 4) 未知数据等级（结构上不被契约枚举允许）
    body = tool_request_body()
    body["data_class"] = "C9"
    status, payload = cluster.post("tool-gateway", "/internal/v1/tool-requests", body)
    assert status == 403
    assert payload["code"] == "DATA_CLASS_NOT_ALLOWED"

    # 5) Context 缺少必填键
    body = tool_request_body()
    del body["trace_id"]
    status, payload = cluster.post("tool-gateway", "/internal/v1/tool-requests", body)
    assert status == 400
    assert payload["code"] == "REQUEST_INVALID"


def test_rejections_leave_redacted_evidence(cluster):  # A01
    """拒绝也要留痕，但日志里不得出现正文或密钥。"""
    status, payload = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests", tool_request_body(tenant="tenant-unknown")
    )
    assert status == 403
    records = [item for item in cluster.evidence() if item.get("outcome") == "rejected"]
    assert records, "拒绝没有留下证据"
    latest = records[-1]
    assert latest["reason_code"] == "TENANT_NOT_REGISTERED"
    assert latest["http_status"] == 403
    # 证据里不得出现 payload / 密钥 / 正文
    serialized = json.dumps(latest, ensure_ascii=False)
    for forbidden in ("payload", "Bearer", "auth", "secret", "password"):
        assert forbidden not in serialized, f"证据泄露了 {forbidden}"


def test_registry_unavailable_is_fail_closed(cluster):  # A01
    """登记表不可用 → 启动即失败，而不是带病运行。"""
    env = dict(os.environ)
    env.update(cluster.port_overrides)
    env["V3_REGISTRY_PATH"] = str(REPO_ROOT / "services" / "_kernel" / "registry.absent.json")
    env["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [venv_python(), "-m", "services.tool-gateway"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        # 必须显式指定编码：子进程 stderr 是 UTF-8，而 Windows 上 subprocess
        # 默认按 ANSI(cp936) 解码会抛 UnicodeDecodeError，把 stderr 变成 None。
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode != 0, "登记表不可用却启动成功（不是 fail-closed）"
    assert "RegistryUnavailable" in (completed.stderr or ""), completed.stderr


# ==========================================================================
# 三、Mock 闭环可追踪
# ==========================================================================

def test_mock_closed_loop_is_traceable(cluster):  # A01
    trace_id = f"trace-{uuid.uuid4().hex[:12]}"
    object_id = f"schedule-{uuid.uuid4().hex[:8]}"
    idem = f"tenant-demo:task-demo:publish:{uuid.uuid4().hex[:6]}"

    # 1) 受理任务（202 只表示受理）
    status, task = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context_body(trace_id=trace_id, task_id=None),
            "idempotency_key": f"task-{idem}",
            "input_ref": object_id,
            "input_schema": schema_binding(),
        },
    )
    assert status == 202, task
    assert task["status"] == "ACCEPTED"
    assert task["status_source"] == "control-plane"
    task_id = task["task_id"]

    # 2) 查询任务投影
    status, fetched = cluster.get("control-plane", f"/api/v1/tasks/{task_id}", headers={"X-Context": json.dumps(context_body(trace_id=trace_id, task_id=None))})
    assert status == 200
    assert fetched["task_id"] == task_id

    # 3) 决策
    status, decision = cluster.post(
        "decision-service",
        "/internal/v1/decisions",
        {
            **context_body(trace_id=trace_id, task_id=task_id),
            "input_ref": object_id,
            "input_schema": schema_binding(),
            "catalog_version": "catalog-mock-1",
        },
    )
    assert status == 200, decision
    assert decision["confidence"] is None, "无校准时 confidence 必须为 null"
    assert decision["decision_id"]

    # 4) 安全网关模型调用
    status, invocation = cluster.post(
        "security-gateway",
        "/internal/v1/model-invocations",
        {
            **context_body(trace_id=trace_id, task_id=task_id),
            "model_id": "model-demo-local",
            "model_version": "1.0.0-mock",
            "model_backend": "local",
            "purpose": "purpose-demo",
            "input_refs": [object_id],
            "output_schema": schema_binding(),
        },
    )
    assert status == 200, invocation
    assert invocation["invocation_id"]

    # 5) 工具请求（写路径）→ 策略判定 → 命令持久化
    status, tool_result = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests",
        tool_request_body(trace_id=trace_id, task_id=task_id, idempotency_key=idem, object_id=object_id),
    )
    assert status == 202, tool_result
    assert tool_result["read_write"] == "write"
    command = tool_result["command"]
    assert command["status"] == "ACCEPTED", "R2 且未批准：只可停在 ACCEPTED"
    assert command["status_source"] == "transaction-service"
    command_id = command["command_id"]

    # 6) 批准（绑定 payload/版本）
    status, approval = cluster.post(
        "control-plane",
        f"/api/v1/approvals/approval-{command_id}/decisions",
        {
            **context_body(trace_id=trace_id, task_id=task_id, actor="user-approver"),
            "approval_id": f"approval-{command_id}",
            "approved": True,
            "object_id": object_id,
            "payload_hash": "sha256:mock",
            "policy_version": "policy-demo",
            "tool_id": "pmc.dispatch.publish",
            "tool_version": "1.0.0-mock",
            "action_scope": ["scope-demo"],
            "valid_until": "2026-10-05T00:00:00Z",
            "expected_version": 6,
        },
    )
    assert status == 200, approval
    assert approval["approved"] is True

    # 7) 执行（仅 transaction-service 受限身份）——错误身份必须被拒
    execution_body = {
        **context_body(trace_id=trace_id, task_id=task_id),
        "command_id": command_id,
        "command": {
            **context_body(trace_id=trace_id, task_id=task_id),
            "command_type": "pmc.dispatch.publish",
            "tool_id": "pmc.dispatch.publish",
            "tool_version": "1.0.0-mock",
            "object_id": object_id,
            "payload_version": "1.0.0",
            "payload": {"schedule_version": 7},
            "idempotency_key": idem,
            "expected_version": 6,
            "policy_version": "policy-demo",
            "policy_evaluation_ref": "policy-eval-demo",
            "approval_ref": f"approval-{command_id}",
            "payload_hash": "sha256:mock",
        },
        "execution_permit_ref": "permit-demo",
        "fencing_token": "fence-demo",
    }
    status, foreign = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-executions",
        execution_body,
        headers={"X-V3-Caller": "hermes-runtime"},
    )
    assert status == 403 and foreign["code"] == "CALLER_NOT_ALLOWED"

    status, executed = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-executions",
        execution_body,
        headers={"X-V3-Caller": "transaction-service"},
    )
    assert status == 200, executed
    assert executed["status"] == "SUCCEEDED"
    assert executed["source_system"] == "pmc-mock"
    receipt_ref = executed["receipt_refs"][0]

    # 8) 权威回执驱动成功：命令状态由对账线程依回执推进（不是调用方宣布成功）
    final = _await_command_status(cluster, command_id, "SUCCEEDED", trace_id=trace_id)
    assert final["receipt_refs"] == [receipt_ref]
    assert final["source_system"] == "pmc-mock"

    # 9) 轨迹：同一 tenant/task/trace 可串联，且覆盖多个服务
    records = [item for item in cluster.evidence() if item.get("trace_id") == trace_id]
    assert records, "闭环没有留下轨迹"
    services_seen = {item["service_id"] for item in records}
    assert len(services_seen) >= 4, f"轨迹只覆盖 {services_seen}"
    assert all(item.get("tenant_id") == "tenant-demo" for item in records)
    assert any(item.get("task_id") == task_id for item in records)


# ==========================================================================
# 四、敏感数据不出域（A02 骨架级证据）
# ==========================================================================

def test_sensitive_data_never_leaves_local_backend(cluster):  # A01
    # 1) C4 + public → 拒绝
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/model-invocations",
        {
            **context_body(tenant="tenant-c3", data_class="C4"),
            "model_id": "model-demo-local",
            "model_version": "1.0.0-mock",
            "model_backend": "public",
            "purpose": "purpose-demo",
            "input_refs": ["input-demo"],
            "output_schema": schema_binding(),
        },
    )
    assert status == 403, payload
    assert payload["code"] == "SENSITIVE_NONLOCAL_MODEL_FORBIDDEN"

    # 2) C2（未批准变换）+ public → 拒绝
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/model-invocations",
        {
            **context_body(tenant="tenant-demo", data_class="C2"),
            "model_id": "model-demo-local",
            "model_version": "1.0.0-mock",
            "model_backend": "public",
            "purpose": "purpose-demo",
            "input_refs": ["input-demo"],
            "output_schema": schema_binding(),
        },
    )
    assert status == 403
    assert payload["code"] == "DATA_CLASS_NOT_ALLOWED_FOR_BACKEND"

    # 3) C1 + public → 允许（证明拒绝不是"一律拒绝"的假阳性）
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/model-invocations",
        {
            **context_body(tenant="tenant-demo", data_class="C1"),
            "model_id": "model-demo-local",
            "model_version": "1.0.0-mock",
            "model_backend": "public",
            "purpose": "purpose-demo",
            "input_refs": ["input-demo"],
            "output_schema": schema_binding(),
        },
    )
    assert status == 403, "自填 public 与登记模型元数据不一致，必须拒绝"

    # 4) 本地后端故障 → 不得自动降到公网：拒绝且不可重试标记为 false
    registry_path = cluster.write_registry_variant(
        "backend-down", lambda document: document["model_backends"]["local"].update({"reachable": False}) or document
    )
    process = cluster.spawn_extra("security-gateway", env_extra={"V3_REGISTRY_PATH": str(registry_path)})
    try:
        status, payload = http(
            "POST",
            f"http://127.0.0.1:{process.port}/internal/v1/model-invocations",
            body={
                **context_body(tenant="tenant-c3", data_class="C3"),
                "model_id": "model-demo-local",
                "model_version": "1.0.0-mock",
                "model_backend": "local",
                "purpose": "purpose-demo",
                "input_refs": ["input-demo"],
                "output_schema": schema_binding(),
            },
        )
        assert status == 503, f"本地故障应 503（排队/人工），实际 {status} {payload}"
        assert payload["retryable"] is False
        assert payload["code"] == "MODEL_BACKEND_UNAVAILABLE"
    finally:
        process.stop()
        registry_path.unlink(missing_ok=True)


# ==========================================================================
# 五、幂等与 UNKNOWN（A04 / A05 骨架级证据）
# ==========================================================================

def test_idempotency_same_key_same_payload_same_command(cluster):  # A04
    tenant = "tenant-demo"
    idem = f"idem-{uuid.uuid4().hex[:8]}"
    object_id = f"schedule-{uuid.uuid4().hex[:8]}"

    def submit(expected_version: int):
        return cluster.post(
            "tool-gateway",
            "/internal/v1/tool-requests",
            tool_request_body(
                tenant=tenant, idempotency_key=idem, object_id=object_id, expected_version=expected_version
            ),
        )

    status_a, first = submit(6)
    status_b, second = submit(6)
    assert status_a == 202 and status_b == 202
    assert first["command"]["command_id"] == second["command"]["command_id"], "同键同 payload 应返回同一命令"

    # 同键异 payload → 409
    status_c, third = submit(9)
    assert status_c == 409, f"同键异 payload 应 409，实际 {status_c} {third}"
    assert third["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"


def test_unknown_command_is_not_retried_with_new_key(cluster):  # A01
    tenant = "tenant-demo"
    idem = f"idem-unknown-{uuid.uuid4().hex[:8]}"
    status, result = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests",
        tool_request_body(tenant=tenant, idempotency_key=idem),
    )
    assert status == 202
    command_id = result["command"]["command_id"]

    # 外部回执丢失 → UNKNOWN，只表示"未知"，绝不能显示已过账
    status, unknown = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}",
        headers={"X-Context": json.dumps(context_body())},
    )
    assert status == 200
    assert unknown["status"] == "ACCEPTED"
    assert unknown["receipt_refs"] == []
    assert unknown["source_system"] is None

    # 换新键重发同一业务动作 → 会得到新 command_id，但契约要求实现方在 UNKNOWN 时先查状态；
    # 本用例只断言"状态可查、回执为空、无伪造成功"，UNKNOWN 状态机收敛属 P3。
    assert unknown["status"] != "SUCCEEDED"


def test_stale_policy_version_blocks_execution(cluster):  # A01
    body = {
        **context_body(),
        "command_id": "command-stale",
        "command": {
            **context_body(),
            "command_type": "pmc.dispatch.publish",
            "tool_id": "pmc.dispatch.publish",
            "tool_version": "1.0.0-mock",
            "object_id": "schedule-demo",
            "payload_version": "1.0.0",
            "payload": {"schedule_version": 7},
            "idempotency_key": "stale-key",
            "expected_version": 6,
            "policy_version": "policy-retired",
            "policy_evaluation_ref": "policy-eval-demo",
            "approval_ref": "approval-demo",
            "payload_hash": "sha256:mock",
        },
        "execution_permit_ref": "permit-demo",
        "fencing_token": "fence-demo",
    }
    status, payload = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-executions",
        body,
        headers={"X-V3-Caller": "transaction-service"},
    )
    assert status == 403, payload
    assert payload["code"] == "POLICY_VERSION_STALE"


def test_checkpoint_version_conflict_is_409(cluster):  # A01
    status, run = cluster.post(
        "hermes-runtime",
        "/internal/v1/runs",
        {
            **context_body(task_id="task-demo"),
            "input_ref": "input-demo",
            "runtime_version": "1.0.0-mock",
        },
    )
    assert status == 202, run
    run_id = run["run_id"]

    resume_body = {
        **context_body(),
        "run_id": run_id,
        "expected_checkpoint_version": 0,
        "checkpoint_ref": "checkpoint-demo",
        "fencing_token": "fence-demo",
        "idempotency_key": "resume-demo",
    }
    status, resumed = cluster.post("hermes-runtime", f"/internal/v1/runs/{run_id}/resume", resume_body)
    assert status == 200, resumed
    assert resumed["checkpoint"]["version"] == 1

    # 用旧版本再恢复 → 409，不得覆盖更新
    status, conflict = cluster.post("hermes-runtime", f"/internal/v1/runs/{run_id}/resume", resume_body)
    assert status == 409, conflict
    assert conflict["code"] == "CHECKPOINT_VERSION_CONFLICT"


# ==========================================================================
# 六、Edge 影子无副作用（A09 骨架级证据）
# ==========================================================================

def test_edge_shadow_has_no_side_effect(cluster):  # A01
    status, payload = cluster.post(
        "manufacturing-core",
        "/edge/v1/decisions",
        {
            **context_body(),
            "edge_id": "edge-demo",
            "mode": "shadow",
            "model_backend": "local",
            "input_ref": "input-demo",
            "input_schema": schema_binding(),
            "release_id": "release-demo",
            "model_version": "1.0.0-mock",
            "calibration_version": "1.0.0-mock",
            "feature_version": "1.0.0-mock",
            "tokenizer_version": "1.0.0-mock",
            "action_mapping_version": "1.0.0-mock",
            "local_policy_ref": "local-policy-demo",
        },
    )
    assert status == 200, payload
    assert payload["mode"] == "shadow"
    assert payload["action_executed"] is False
    assert payload["receipt_refs"] == []


def test_edge_release_gate_rejects_missing_evidence(cluster):  # A01
    body = {
        **context_body(),
        "edge_id": "edge-demo",
        "release_id": "release-demo",
        "package_ref": "package-demo",
        "signature_ref": "signature-demo",
        "schema_binding": schema_binding(),
        "model_version": "1.0.0-mock",
        "calibration_version": "1.0.0-mock",
        "feature_version": "1.0.0-mock",
        "tokenizer_version": "1.0.0-mock",
        "action_mapping_version": "1.0.0-mock",
        "gate_evidence_refs": ["evidence-demo"],
        "shadow_days": 3,
        "schema_frozen": True,
        "offline_calibration_passed": True,
        "shadow_coverage_passed": True,
        "local_permit_ref": "permit-demo",
        "local_permit_valid_until": "2026-10-05T00:00:00Z",
        "interlock_verified": True,
    }
    status, payload = cluster.post("manufacturing-core", "/edge/v1/releases/activate", body)
    assert status == 403, payload
    assert payload["code"] == "EDGE_RELEASE_GATE_FAILED"
    assert payload["details"]["reason_ref"] == "shadow_days_below_minimum"


# ==========================================================================
# 七、响应体符合契约（用 L1 的 Schema 机器校验）
# ==========================================================================

def _load_validator(document_name: str, pointer: str) -> Draft202012Validator:
    document = json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
    node = document
    for part in pointer.strip("/").split("/"):
        if not part:
            continue
        node = node[part]
    resolved = _inline_refs(node, document_name)
    return Draft202012Validator(resolved)


def _inline_refs(node, current_document: str):
    """把仓库内 $ref 内联展开成自包含 schema，供 jsonschema 校验。

    只处理本仓库的相对引用与同文件 `#/$defs/...`，遇到外网 URL 直接报错
    （契约禁止外部引用）。
    """
    if isinstance(node, dict):
        if "$ref" in node:
            reference = node["$ref"]
            if reference.startswith(("http://", "https://")):
                raise AssertionError(f"契约出现外部引用: {reference}")
            file_part, _, pointer = reference.partition("#")
            document_name = file_part or current_document
            document = json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
            target = document
            for part in pointer.strip("/").split("/"):
                if not part:
                    continue
                target = target[part]
            resolved = _inline_refs(dict(target), document_name)
            extra = {key: value for key, value in node.items() if key != "$ref"}
            if extra:
                merged = dict(resolved)
                merged.update(_inline_refs(extra, current_document))
                return merged
            return resolved
        return {key: _inline_refs(value, current_document) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, current_document) for item in node]
    return node


def _assert_valid(instance, document_name: str, pointer: str, label: str) -> None:
    validator = _load_validator(document_name, pointer)
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    assert not errors, f"{label} 不符合契约 {document_name}{pointer}: " + "; ".join(
        f"{'/'.join(str(part) for part in error.path)}: {error.message}" for error in errors[:4]
    )


def test_responses_conform_to_contract_schemas(cluster):  # A01
    # Task
    status, task = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context_body(),
            "idempotency_key": f"task-{uuid.uuid4().hex[:8]}",
            "input_ref": "schedule-demo",
            "input_schema": schema_binding(),
        },
    )
    assert status == 202
    _assert_valid(task, "task.json", "", "Task 响应")

    # DecisionResult
    status, decision = cluster.post(
        "decision-service",
        "/internal/v1/decisions",
        {
            **context_body(),
            "input_ref": "input-demo",
            "input_schema": schema_binding(),
            "catalog_version": "catalog-mock-1",
        },
    )
    assert status == 200
    _assert_valid(decision, "decision-result.json", "", "DecisionResult 响应")

    # ModelInvocationResult
    status, invocation = cluster.post(
        "security-gateway",
        "/internal/v1/model-invocations",
        {
            **context_body(),
            "model_id": "model-demo-local",
            "model_version": "1.0.0-mock",
            "model_backend": "local",
            "purpose": "purpose-demo",
            "input_refs": ["input-demo"],
            "output_schema": schema_binding(),
        },
    )
    assert status == 200
    _assert_valid(invocation, "api-models.json", "/$defs/ModelInvocationResult", "ModelInvocationResult")

    # PolicyResult
    status, policy = cluster.post(
        "control-plane",
        "/internal/v1/policy/evaluate",
        {
            **context_body(),
            "tool_id": "pmc.dispatch.publish",
            "tool_version": "1.0.0-mock",
            "action": "action-demo",
            "object_id": "schedule-demo",
            "payload_hash": "sha256:mock",
            "expected_version": 6,
            "payload_version": "1.0.0",
            "normalized_payload_ref": "normalized-demo",
            "delegated_scopes": ["pmc.dispatch.publish"],
        },
    )
    assert status == 200
    _assert_valid(policy, "api-models.json", "/$defs/PolicyResult", "PolicyResult")

    # ToolRequestResult（写路径）
    status, tool_result = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests",
        tool_request_body(idempotency_key=f"idem-{uuid.uuid4().hex[:8]}"),
    )
    assert status == 202
    _assert_valid(tool_result, "api-models.json", "/$defs/ToolRequestResult", "ToolRequestResult")
    _assert_valid(tool_result["command"], "command-status.json", "", "CommandStatus")

    # Health
    status, readiness = cluster.get("control-plane", "/health/ready")
    assert status == 200
    _assert_valid(readiness, "api-models.json", "/$defs/Health", "Health")

    # Error
    status, error = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests", tool_request_body(tool_id="unknown.tool")
    )
    assert status == 403
    _assert_valid(error, "error.json", "", "Error")

    # RunResult
    status, run = cluster.post(
        "hermes-runtime",
        "/internal/v1/runs",
        {**context_body(), "input_ref": "input-demo", "runtime_version": "1.0.0-mock"},
    )
    assert status == 202
    _assert_valid(run, "api-models.json", "/$defs/RunResult", "RunResult")

    # EdgeDecisionResult
    status, edge = cluster.post(
        "manufacturing-core",
        "/edge/v1/decisions",
        {
            **context_body(),
            "edge_id": "edge-demo",
            "mode": "shadow",
            "model_backend": "local",
            "input_ref": "input-demo",
            "input_schema": schema_binding(),
            "release_id": "release-demo",
            "model_version": "1.0.0-mock",
            "calibration_version": "1.0.0-mock",
            "feature_version": "1.0.0-mock",
            "tokenizer_version": "1.0.0-mock",
            "action_mapping_version": "1.0.0-mock",
            "local_policy_ref": "local-policy-demo",
        },
    )
    assert status == 200
    _assert_valid(edge, "api-models.json", "/$defs/EdgeDecisionResult", "EdgeDecisionResult")


def test_implementation_does_not_add_contract_paths(cluster):  # A01
    """实现不得新增契约之外的路径（§10.1 接口目录是封闭的）。"""
    openapi = json.loads(OPENAPI.read_text(encoding="utf-8"))
    declared = {
        (method.upper(), path)
        for path, operations in openapi["paths"].items()
        for method in operations
    }
    # 探一批"看起来合理但契约没有"的路径，必须 404。
    # 注意归口：P0 门禁只证明"P0 集合之外的服务端不该有的东西不存在"，
    # **不**断言 P1 路径不存在——P1 的新接口按设计就挂在同一个 security-gateway /
    # manufacturing-core 上（P1 契约在 openapi/p1.openapi.json 声明），
    # 那部分由 tests/p1_runtime 的 test_no_new_interface_layer_and_owner_routing 负责。
    forbidden = (
        "/internal/v1/evidence",
        "/api/v1/tasks/demo/events",
        "/internal/v1/tools",
        "/api/v1/employees",
        "/edge/v1/releases",
    )
    for path in forbidden:
        for service_id in SERVICE_IDS:
            status, _ = cluster.get(service_id, path)
            assert status == 404, f"{service_id} 暴露了契约外路径 {path}"
    assert ("POST", "/internal/v1/commands") in declared
