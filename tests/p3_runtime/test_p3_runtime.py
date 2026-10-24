"""P3 运行验收：受控规范化、绑定哈希与无人值守写入准入。

覆盖 SPEC `docs/v3-upgrade/04-阶段-P3-受控写入.md` 中本批已实现的两个判定型端点。
其余 P3 接口（批准绑定/执行前复核/受控执行/查询收敛/Saga）尚在实现中，
**不得**以本文件通过为由宣称 P3 完成。
"""

from __future__ import annotations

import json
import uuid

import pytest
from jsonschema import Draft202012Validator

from tests.p0_runtime.conftest import REPO_ROOT, context_body, schema_binding

from .conftest import IDENTITY, p15_context

SCHEMAS = REPO_ROOT / "packages" / "contracts" / "schemas"


def _headers(*, trace_id: str = "trace-p3") -> dict:
    return {"X-Context": json.dumps(context_body(trace_id=trace_id))}


def _normalize_body(*, tool_id: str = "pmc.dispatch.publish", tool_version: str = "1.0.0-mock",
                    object_id: str = "schedule-demo", expected_version: int = 6,
                    input_ref: str = "schedule-input-demo", trace_id: str = "trace-p3") -> dict:
    context = context_body(trace_id=trace_id)
    return {
        **context,
        "context": context,
        "object_id": object_id,
        "expected_version": expected_version,
        "tool_id": tool_id,
        "tool_version": tool_version,
        "command_type": "pmc.dispatch.publish",
        "input_ref": input_ref,
        "input_schema": schema_binding(),
    }


# ==========================================================================
# 一、受控规范化与绑定哈希
# ==========================================================================

def test_normalize_returns_canonical_hash_and_binding(cluster):  # A06
    status, view = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests/normalize", _normalize_body(), headers=_headers()
    )
    assert status == 200, view
    binding = view["binding"]
    assert view["normalizer_unit"] == "tool-gateway", "哈希必须由可信服务产出"
    assert len(binding["payload_hash"]) == 64 and binding["payload_hash"] == binding["payload_hash"].lower()
    assert all(c in "0123456789abcdef" for c in binding["payload_hash"])
    assert binding["normalization_ref"] == f"norm-{binding['payload_hash'][:16]}"
    # action_scope 来自受信目录，不由请求体决定
    assert binding["action_scope"] == ["pmc.dispatch.publish"]
    # schema 绑定被原样带上，便于后续校验 hash 与版本
    assert binding["payload_schema"]["schema_ref"] == schema_binding()["schema_ref"]


def test_canonical_hash_is_deterministic_and_input_sensitive(cluster):  # A06
    """同一绑定必须永远算出同一哈希；任一绑定维度变化必须换哈希。"""
    first = cluster.post("tool-gateway", "/internal/v1/tool-requests/normalize", _normalize_body(), headers=_headers())
    second = cluster.post("tool-gateway", "/internal/v1/tool-requests/normalize", _normalize_body(), headers=_headers())
    assert first[0] == 200 and second[0] == 200
    assert first[1]["binding"]["payload_hash"] == second[1]["binding"]["payload_hash"], "哈希不得含时间或随机量"

    changed = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests/normalize",
        _normalize_body(input_ref="schedule-input-other"),
        headers=_headers(),
    )
    assert changed[0] == 200
    assert changed[1]["binding"]["payload_hash"] != first[1]["binding"]["payload_hash"]

    version_changed = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests/normalize",
        _normalize_body(expected_version=7),
        headers=_headers(),
    )
    assert version_changed[0] == 200
    assert version_changed[1]["binding"]["payload_hash"] != first[1]["binding"]["payload_hash"]


def test_normalize_rejects_unknown_tool_and_version_mismatch(cluster):  # A03
    status, payload = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests/normalize",
        _normalize_body(tool_id="not.a.registered.tool"), headers=_headers(),
    )
    assert status == 404, payload
    assert payload["code"] == "TOOL_NOT_IN_CATALOG"

    status, payload = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests/normalize",
        _normalize_body(tool_version="9.9.9"), headers=_headers(),
    )
    assert status == 409, payload
    assert payload["code"] == "TOOL_VERSION_MISMATCH"


def test_normalize_rejects_read_only_tool(cluster):  # A06
    """只读工具不得进入写入规范化（读路径与写路径必须分离）。"""
    status, payload = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests/normalize",
        _normalize_body(tool_id="erp.stock.query", tool_version="1.0.0-mock"), headers=_headers(),
    )
    assert status == 403, payload
    assert payload["code"] == "TOOL_IS_READ_ONLY"


# ==========================================================================
# 二、无人值守高风险写入的准入
# ==========================================================================

def test_admission_denies_unattended_write_when_capability_missing(cluster):  # A06
    """`pmc.dispatch.publish` 缺 compensation → 不得无人值守执行高风险写入。"""
    status, view = cluster.get(
        "tool-gateway", "/internal/v1/tools/pmc.dispatch.publish/write-admission",
        headers=_headers(),
    )
    assert status == 200, view
    assert view["tool_spec"]["compensation"] is False
    assert view["unattended_high_risk_write_allowed"] is False
    assert view["adapter_ledger_required"] is True
    assert view["serial_object_lock_required"] is True
    assert view["manual_reconciliation_required"] is True
    assert view["reason_ref"], "拒绝必须给出可追溯的原因引用"


def test_admission_denies_for_read_only_tool_without_capabilities(cluster):  # A06
    """`erp.stock.query` 三项能力位全 false → 同样不得无人值守地写。"""
    status, view = cluster.get(
        "tool-gateway", "/internal/v1/tools/erp.stock.query/write-admission", headers=_headers()
    )
    assert status == 200, view
    assert view["unattended_high_risk_write_allowed"] is False
    assert view["adapter_ledger_required"] is True


def test_admission_unknown_tool_is_404(cluster):  # A06
    status, payload = cluster.get(
        "tool-gateway", "/internal/v1/tools/not.registered/write-admission", headers=_headers()
    )
    assert status == 404, payload
    assert payload["code"] == "TOOL_NOT_IN_CATALOG"


# ==========================================================================
# 三、规范化哈希的复核一致性（§10.4：Transaction 复核，不只信传来的值）
# ==========================================================================

def _write_truth(cluster, name: str, records: list[dict]) -> None:
    """直接写 journal，用于构造"实现尚未接线"的既有事实。

    P3 的受控命令入口（`/internal/v1/commands/controlled`）尚未实现，
    因此这里用内核的 journal 构造"命令带 normalization_ref"这一既有事实，
    用来验收收执路径的哈希复核逻辑本身。
    """
    import json as _json

    path = cluster.state_dir / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(_json.dumps(record, ensure_ascii=False) + "\n")


def _canonical_hash(**kwargs) -> str:
    import sys

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from services._kernel import transactions

    return transactions.canonical_payload_hash(**kwargs)


def test_receipt_recheck_rejects_missing_normalization_record(cluster):  # A06
    """命令声明了规范化记录但记录不存在 → 拒绝收执（fail-closed，不靠猜）。"""
    command_id = f"command-{uuid.uuid4().hex[:10]}"
    trace = f"trace-recheck-{uuid.uuid4().hex[:8]}"
    _write_truth(
        cluster,
        "commands",
        [{
            "command_id": command_id,
            "tenant_id": "tenant-demo",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
            "payload_hash": "a" * 64,
            "normalization_ref": "norm-does-not-exist",
            "status": "AUTHORIZED",
            "updated_at": "2026-10-04T00:00:00Z",
        }],
    )
    status, payload = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/receipts",
        {
            "tenant_id": "tenant-demo",
            "context": context_body(trace_id=trace),
            "command_id": command_id,
            "idempotency_key": "idem-recheck",
            "command_payload_hash": "a" * 64,
            "receipt_id": f"receipt-{uuid.uuid4().hex[:8]}",
            "source_system": "pmc-mock",
            "authority_receipt_ref": "ref",
            "authority_receipt_hash": "hash",
        },
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "NORMALIZATION_RECORD_MISSING"


def test_receipt_recheck_rejects_tampered_normalization(cluster):  # A06
    """规范化记录被改过（重算哈希与落库不一致）→ 拒绝收执。"""
    command_id = f"command-{uuid.uuid4().hex[:10]}"
    trace = f"trace-tamper-{uuid.uuid4().hex[:8]}"
    canonical_kwargs = dict(
        tenant_id="tenant-demo",
        object_id="schedule-demo",
        expected_version=6,
        tool_id="pmc.dispatch.publish",
        tool_version="1.0.0-mock",
        command_type="pmc.dispatch.publish",
        action_scope=["pmc.dispatch.publish"],
        payload_ref="schedule-input-demo",
    )
    good_hash = _canonical_hash(**canonical_kwargs)

    # 规范化记录指向一个**不同**的 payload_ref，因此重算结果不会等于落库哈希
    tampered = dict(canonical_kwargs)
    tampered["payload_ref"] = "schedule-input-TAMPERED"
    _write_truth(
        cluster,
        "normalizations",
        [{
            "tenant_id": "tenant-demo",
            "normalization_ref": "norm-tampered",
            "binding": {**tampered, "context": context_body(trace_id=trace)},
            "recorded_at": "2026-10-04T00:00:00Z",
        }],
    )
    _write_truth(
        cluster,
        "commands",
        [{
            "command_id": command_id,
            "tenant_id": "tenant-demo",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
            "payload_hash": good_hash,
            "normalization_ref": "norm-tampered",
            "status": "AUTHORIZED",
            "updated_at": "2026-10-04T00:00:00Z",
        }],
    )
    status, payload = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/receipts",
        {
            "tenant_id": "tenant-demo",
            "context": context_body(trace_id=trace),
            "command_id": command_id,
            "idempotency_key": "idem-tamper",
            "command_payload_hash": good_hash,
            "receipt_id": f"receipt-{uuid.uuid4().hex[:8]}",
            "source_system": "pmc-mock",
            "authority_receipt_ref": "ref",
            "authority_receipt_hash": "hash",
        },
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "NORMALIZATION_HASH_MISMATCH"


def test_receipt_accepts_consistent_normalization(cluster):  # A06
    """规范化记录与落库哈希一致时不得误拒（防止上面的守卫变成一律拒绝）。"""
    command_id = f"command-{uuid.uuid4().hex[:10]}"
    trace = f"trace-ok-{uuid.uuid4().hex[:8]}"
    canonical_kwargs = dict(
        tenant_id="tenant-demo",
        object_id="schedule-demo",
        expected_version=6,
        tool_id="pmc.dispatch.publish",
        tool_version="1.0.0-mock",
        command_type="pmc.dispatch.publish",
        action_scope=["pmc.dispatch.publish"],
        payload_ref="schedule-input-demo",
    )
    payload_hash = _canonical_hash(**canonical_kwargs)
    _write_truth(
        cluster,
        "normalizations",
        [{
            "tenant_id": "tenant-demo",
            "normalization_ref": "norm-consistent",
            "binding": {**canonical_kwargs, "context": context_body(trace_id=trace)},
            "recorded_at": "2026-10-04T00:00:00Z",
        }],
    )
    _write_truth(
        cluster,
        "commands",
        [{
            "command_id": command_id,
            "tenant_id": "tenant-demo",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
            "payload_hash": payload_hash,
            "normalization_ref": "norm-consistent",
            "status": "AUTHORIZED",
            "updated_at": "2026-10-04T00:00:00Z",
        }],
    )
    status, receipt = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/receipts",
        {
            "tenant_id": "tenant-demo",
            "context": context_body(trace_id=trace),
            "command_id": command_id,
            "idempotency_key": "idem-ok",
            "command_payload_hash": payload_hash,
            "receipt_id": f"receipt-{uuid.uuid4().hex[:8]}",
            "source_system": "pmc-mock",
            "authority_receipt_ref": "ref",
            "authority_receipt_hash": "hash",
        },
        headers=IDENTITY,
    )
    assert status == 200, receipt
    assert receipt["business_success"] is True
    assert receipt["posting_status"] == "NOT_CONFIRMED"


# ==========================================================================
# 五、命令主干：受控持久化 → 租约 → 执行前复核 → 执行意图
# ==========================================================================

def _normalize(cluster, *, trace_id: str, payload_ref: str = "schedule-input-demo",
               object_id: str = "schedule-demo"):
    """规范化。

    `object_id` 必须能按用例指定：同一对象上的**串行对象锁**在 60s 租约内独占，
    若多个用例共用 `schedule-demo`，"对象已被加锁"会先于被测结论触发，
    造成用例互相干扰、门禁偶发误报（实测踩过：一条幂等用例因此被误判为"逃逸"）。
    """
    status, view = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests/normalize",
        _normalize_body(input_ref=payload_ref, object_id=object_id, trace_id=trace_id),
        headers=_headers(trace_id=trace_id),
    )
    assert status == 200, view
    return view


def _write_approval(cluster, *, approval_id: str, binding: dict, valid_until: str = "2099-01-01T00:00:00Z",
                    approver_ref: str = "user-approver"):
    """构造一条**已授予**的批准事实。

    必须写进 `approvals` —— 那是批准事实的**唯一**存储（P0 决策接口也写这里）。
    早期这里写的是 `p3-approval-bindings` 这个从未被读的通道，于是
    "批准明明存在却报 APPROVAL_NOT_FOUND"，掩盖了真实缺陷。
    """
    import sys

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from services._kernel import routes as _routes

    _write_truth(
        cluster,
        "approvals",
        [{
            "tenant_id": "tenant-demo",
            "factory_id": "factory-demo",
            "approval_id": approval_id,
            "approved": True,
            # 默认是"合法审批人"（与发起人 user-demo 不同）。
            # 需要构造**自批**这类历史事实时，调用方显式传 approver_ref=发起人。
            "approver_ref": approver_ref,
            "binding": binding,
            "binding_hash": _routes._p3_binding_hash(binding),
            "policy_evaluation_ref": f"policy-eval-{approval_id}",
            "decision_ref": f"approval-decision-{approval_id}",
            "decided_at": "2026-10-04T00:00:00Z",
            "valid_from": "2026-10-04T00:00:00Z",
            "valid_until": valid_until,
            "record_version": 1,
            "updated_at": "2026-10-04T00:00:00Z",
        }],
    )


def _submit(cluster, *, trace_id: str, normalization_ref: str, approval_ref=None,
            idempotency_key: str | None = None, task_id: str | None = None):
    context = context_body(trace_id=trace_id)
    if task_id is not None:
        # 让命令挂到**真任务**上（`command.json` 的 required 含 `task_id`）。
        # 默认值 `task-demo` 只适用于"命令与任务无关"的老用例。
        context = {**context, "task_id": task_id}
    return cluster.post(
        "transaction-service",
        "/internal/v1/commands/controlled",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "normalization_ref": normalization_ref,
            "policy_evaluation_ref": f"policy-{trace_id}",
            "approval_ref": approval_ref,
            "idempotency_key": idempotency_key or f"idem-{uuid.uuid4().hex[:8]}",
        },
        headers=IDENTITY,
    )


def _take_lease(cluster, *, command_id: str, version: int, worker: str, trace_id: str, previous=None):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/leases",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "command_id": command_id,
            "worker_ref": worker,
            "expected_record_version": version,
            "previous_lease_ref": previous,
        },
        headers=IDENTITY,
    )


def _execute(cluster, *, command_id: str, version: int, lease, trace_id: str):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execute",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "command_id": command_id,
            "expected_record_version": version,
            "lease_ref": lease["lease_ref"],
            "fencing_token": lease["fencing_token"],
            "review_ref": f"review-{trace_id}",
        },
        headers=IDENTITY,
    )


def test_controlled_submit_persists_before_accept_and_is_visible_to_other_owner(cluster):  # A14
    trace = f"trace-ctl-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])

    status, accepted = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"], approval_ref=approval_id)
    assert status == 202, accepted
    assert accepted["status"] == "accepted"
    assert accepted["business_success"] is False, "受理不是业务成功"
    assert accepted["posting_status"] == "NOT_CONFIRMED"
    command_id = accepted["command_id"]

    # 另一个 owner（manufacturing-core）能读到同一权威事实 → 已持久化而非内存态
    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execution-record",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200, record
    assert record["status"] == "AUTHORIZED"
    assert record["status_source"] == "transaction-service"
    assert record["business_success"] is False
    assert record["posting_status"] == "NOT_CONFIRMED"
    assert record["execution_credential"] is None


def test_same_key_same_payload_returns_same_command(cluster):  # A04
    trace = f"trace-idem-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    key = f"idem-{uuid.uuid4().hex[:8]}"

    first = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"], approval_ref=approval_id, idempotency_key=key)
    second = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"], approval_ref=approval_id, idempotency_key=key)
    assert first[0] == 202 and second[0] == 202
    assert first[1]["command_id"] == second[1]["command_id"], "同键同 payload 必须返回同一命令"


def test_same_key_different_payload_is_conflict(cluster):  # A04
    trace = f"trace-conf-{uuid.uuid4().hex[:8]}"
    first_norm = _normalize(cluster, trace_id=trace, payload_ref="schedule-input-a")
    second_norm = _normalize(cluster, trace_id=trace, payload_ref="schedule-input-b")
    assert first_norm["binding"]["payload_hash"] != second_norm["binding"]["payload_hash"]

    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=first_norm["binding"])
    key = f"idem-{uuid.uuid4().hex[:8]}"
    assert _submit(cluster, trace_id=trace, normalization_ref=first_norm["normalization_ref"],
                   approval_ref=approval_id, idempotency_key=key)[0] == 202

    status, payload = _submit(cluster, trace_id=trace, normalization_ref=second_norm["normalization_ref"],
                              approval_ref=approval_id, idempotency_key=key)
    assert status == 409, payload
    assert payload["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"


def test_lease_fencing_blocks_stale_worker_and_serializes_object_lock(cluster):  # A06
    trace = f"trace-lease-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-lock-demo")
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]

    status, lease1 = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)
    assert status == 200, lease1
    assert lease1["fencing_token"] == 1

    # 第二个 worker 不能抢同一对象的锁
    status, payload = _take_lease(cluster, command_id=command_id, version=2, worker="worker-b", trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "OBJECT_LOCK_HELD"

    # worker-a 续租 → token 递增
    status, lease2 = _take_lease(cluster, command_id=command_id, version=2, worker="worker-a",
                                 trace_id=trace, previous=lease1["lease_ref"])
    assert status == 200, lease2
    assert lease2["fencing_token"] == 2

    # 用**旧 token** 提交执行 → 拒绝（僵尸 worker）
    status, payload = _execute(cluster, command_id=command_id, version=3, lease=lease1, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "FENCING_TOKEN_STALE"

    # 用当前 token → 受理，状态 EXECUTING，且仅受理
    status, accepted = _execute(cluster, command_id=command_id, version=3, lease=lease2, trace_id=trace)
    assert status == 202, accepted
    assert accepted["business_success"] is False
    assert accepted["posting_status"] == "NOT_CONFIRMED"

    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execution-record",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200
    assert record["status"] == "EXECUTING"
    assert record["execution_credential"]["lease"]["fencing_token"] == 2
    assert record["business_success"] is False, "执行中不得宣称业务成功"


def test_lease_version_cas_rejects_stale_expected_version(cluster):  # A06
    trace = f"trace-cas-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]

    status, payload = _take_lease(cluster, command_id=command_id, version=99, worker="worker-a", trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "COMMAND_VERSION_CONFLICT"


def test_execute_requires_prior_lease(cluster):  # A06
    """没有租约就执行 → 拒绝（防止绕过串行对象锁）。"""
    trace = f"trace-nolock-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]

    status, payload = _execute(
        cluster, command_id=command_id, version=1,
        lease={"lease_ref": "lease-forged", "fencing_token": 1}, trace_id=trace,
    )
    assert status == 409, payload
    assert payload["code"] == "LEASE_NOT_HELD"


def test_expired_approval_blocks_execution(cluster):  # A06
    """批准过期 → 执行前复核失败（A06）。"""
    trace = f"trace-exp-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"], valid_until="2020-01-01T00:00:00Z")
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]

    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]
    status, payload = _execute(cluster, command_id=command_id, version=2, lease=lease, trace_id=trace)
    assert status == 403, payload
    assert payload["code"] == "PRE_EXECUTION_REVIEW_FAILED"
    assert payload["details"]["reason_ref"] == "APPROVAL_EXPIRED"


def test_high_risk_command_without_approval_stays_accepted(cluster):  # A06
    """R2 且无批准 → 停在 ACCEPTED 等批准事件，不得假装可执行。"""
    trace = f"trace-noappr-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"])[1]["command_id"]

    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execution-record",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200
    assert record["status"] == "ACCEPTED"
    assert record["approval_ref"] is None

    # 也不能直接执行
    status, payload = _execute(
        cluster, command_id=command_id, version=0,
        lease={"lease_ref": "lease-x", "fencing_token": 1}, trace_id=trace,
    )
    assert status == 409


def test_command_view_conforms_to_p3_contract(cluster):  # A06
    trace = f"trace-view-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    status, accepted = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                               approval_ref=approval_id)
    assert status == 202
    _assert_valid(accepted, "p3-command.json", "/$defs/Accepted", "CommandAccepted")

    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{accepted['command_id']}/execution-record",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200
    _assert_valid(record, "p3-command.json", "/$defs/View", "CommandView")

    lease = _take_lease(cluster, command_id=accepted["command_id"], version=1, worker="worker-a", trace_id=trace)
    assert lease[0] == 200
    _assert_valid(lease[1], "p3-execution.json", "/$defs/Lease", "Lease")


def test_command_state_survives_transaction_service_restart(cluster):  # A07
    """**重启恢复**（上游 P3 完成条件之一）：事务服务重启后必须重建同一事实。

    上游 §12：worker 用租约及 fencing token 防止旧 worker 继续提交。
    重启后内存必然清空，因此这条断言实际验证的是
    「命令账是持久的，恢复后旧 fencing token 依然被拒」。
    """
    trace = f"trace-restart-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-restart-demo")
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease1 = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    # 重启 transaction-service（只重启它，模拟单点恢复）
    cluster.restart("transaction-service")

    # 重启后仍能读到同一命令与同一租约
    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execution-record",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200, record
    assert record["status"] == "AUTHORIZED"
    # 取锁已经推进过一次记录版本，故断言"有推进"而不是某个具体值
    assert record["record_version"] >= 1
    assert record["execution_credential"] is None

    # 旧 worker 拿旧 token 提交 → 依然被拒（fencing 是持久的，不因重启而失效）
    # 注意 expected_record_version 必须取**权威读**里的当前值：
    # 取锁本身会推进一次记录版本，硬编码版本会先撞上 CAS 冲突而掩盖 fencing 结论。
    current_version = record["record_version"]
    status, payload = _execute(cluster, command_id=command_id, version=current_version,
                               lease={"lease_ref": "lease-forged", "fencing_token": lease1["fencing_token"]},
                               trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "LEASE_NOT_HELD"

    # 另一个 worker 抢同一对象的锁 → 拒绝（原锁在有效期内独占，重启不改变这一点）
    status, payload = _take_lease(cluster, command_id=command_id, version=current_version,
                                  worker="worker-b", trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "OBJECT_LOCK_HELD"

    # 原持有者续租 → fencing token 跨重启继续**单调递增**
    # （token 由记录里的旧值推进，而非内存计数器，因此重启不会重置）
    status, lease2 = _take_lease(cluster, command_id=command_id, version=current_version,
                                 worker="worker-a", trace_id=trace, previous=lease1["lease_ref"])
    assert status == 200, lease2
    assert lease2["fencing_token"] > lease1["fencing_token"], "fencing token 必须跨重启单调递增"
    assert lease2["object_lock_ref"] == lease1["object_lock_ref"], "同一对象的串行锁引用应稳定"


def test_command_get_view_supports_transaction_created_records(cluster):  # A14
    """两个命令视图都必须能读**P3 交易路径创建的**命令记录。

    **这是一条回归断言，来自一次真实 500（2026-10-05，联调时发现）**：
    真实跑通一条 P3 链路后去读命令，`GET /internal/v1/commands/{id}`
    直接 `KeyError: 'source_system'`，连接被掐断（`RemoteDisconnected`）。

    根因：命令记录有**两种形状**——
    P0/P1.15 路径有 `source_system` / `receipt_refs`；P3 受控交易路径没有，
    它有的是 `binding` / `authority_receipt` / `lease`。
    前一个视图按前者的键硬取，碰到后者就崩。

    **此前测不出来的原因**：路由可达性测试用的是占位 command_id，
    走到 404 就返回了，从没让"一条真实存在的 P3 命令"走过这两个视图。

    ## 两个视图的形状由**各自的契约**规定，不能互相借用

    | 接口 | 契约 | 该有 `posting_status`？ |
    | --- | --- | --- |
    | `/internal/v1/commands/{id}` | `command-status.json`（16 字段，`additionalProperties: false`） | **否** |
    | `/internal/v1/commands/{id}/execution-record` | `p3-command.json#/$defs/View` | **是** |

    我第一版把 `posting_status` 断言加在前者上，结果**自己把契约测红了**——
    正确的修法是读对接口，而不是改宽契约。
    """
    trace = f"trace-cmdview-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-cmdview")
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(
        cluster,
        trace_id=trace,
        normalization_ref=view["normalization_ref"],
        approval_ref=approval_id,
    )[1]["command_id"]
    headers = {"X-Context": json.dumps(context_body(trace_id=trace))}

    # ① P0 形状视图：修复前这里会 500
    status, body = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}",
        headers=headers,
    )
    assert status == 200, f"P3 命令读取崩了：{status} {body}"
    assert body["command_id"] == command_id
    assert body["status"] in ("ACCEPTED", "AUTHORIZED")
    # 该契约是 additionalProperties: false ⇒ 不许混进 P3 的字段
    assert "posting_status" not in body, (
        "command-status.json 是 additionalProperties: false，不得混入 P3 字段"
    )

    # ② P3 形状视图：账务结论只认这里
    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execution-record",
        headers=headers,
    )
    assert status == 200, f"P3 命令权威视图读取失败：{status} {record}"
    assert "posting_status" in record, "p3-command View 必须暴露 posting_status"
    assert "business_success" in record, "p3-command View 必须暴露 business_success"
    # 未执行 ⇒ 无回执 ⇒ 未过账，且**不得**编造已过账
    assert record["posting_status"] == "NOT_CONFIRMED", record["posting_status"]
    assert record["business_success"] is False


def test_receipt_after_restart_still_drives_success(cluster):  # A14
    """重启后权威回执仍能驱动业务成功（业务成功与过账状态始终分离）。"""
    trace = f"trace-restart2-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    submitted = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                        approval_ref=approval_id)
    command_id = submitted[1]["command_id"]

    cluster.restart("transaction-service")

    payload_hash = view["binding"]["payload_hash"]
    status, receipt = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/receipts",
        {
            "tenant_id": "tenant-demo",
            "context": context_body(trace_id=trace),
            "command_id": command_id,
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
            "command_payload_hash": payload_hash,
            "receipt_id": f"receipt-{uuid.uuid4().hex[:8]}",
            "source_system": "pmc-mock",
            "authority_receipt_ref": "ref",
            "authority_receipt_hash": "hash",
        },
        headers=IDENTITY,
    )
    assert status == 200, receipt
    assert receipt["business_success"] is True
    assert receipt["posting_status"] == "NOT_CONFIRMED"


def test_cancelled_before_start_command_cannot_execute(cluster):  # A06
    """**取消语义**（§12）：未开始步骤可停止，且停止后不得再执行。

    上游：取消只能停止未开始步骤，不能把 Task 设为 CANCELLED 就假定撤销。
    这里验证"开始前已取消"这一标记会挡住执行——取消接口尚未接线，
    故先用内核事实构造该状态。
    """
    trace = f"trace-cancel-{uuid.uuid4().hex[:8]}"
    object_id = "schedule-cancel-demo"
    view = _normalize(cluster, trace_id=trace, object_id=object_id)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]

    status, lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)
    assert status == 200, lease

    # 标记"开始前已取消"
    _write_truth(
        cluster,
        "commands",
        [{
            "command_id": command_id,
            "tenant_id": "tenant-demo",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
            "payload_hash": view["binding"]["payload_hash"],
            "binding": view["binding"],
            "normalization_ref": view["normalization_ref"],
            "policy_evaluation_ref": "policy-cancel",
            "approval_ref": approval_id,
            "status": "AUTHORIZED",
            "record_version": 2,
            "risk_class": "R2",
            "lease": lease,
            "cancelled_before_start": True,
            "updated_at": "2026-10-04T00:00:00Z",
        }],
    )

    status, payload = _execute(cluster, command_id=command_id, version=2, lease=lease, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "CANCELLED_BEFORE_START", "取消标记必须挡住执行"


# ==========================================================================
# 六、UNKNOWN 收敛（禁止换键重发 / 禁止直接补偿）
# ==========================================================================

def _reconcile(cluster, *, command_id: str, lease: dict, source_state: str, version: int, trace_id: str):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/reconcile",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "command_id": command_id,
            "expected_record_version": version,
            "lease_ref": lease["lease_ref"],
            "fencing_token": lease["fencing_token"],
            "source_state": source_state,
        },
        headers=IDENTITY,
    )


def _executing_command(cluster, *, object_id: str, trace: str):
    """造一个处于 EXECUTING 且有租约的命令（收敛的入口场景）。"""
    view = _normalize(cluster, trace_id=trace, object_id=object_id)
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]
    assert _execute(cluster, command_id=command_id, version=lease["record_version"],
                    lease=lease, trace_id=trace)[0] == 202
    return command_id, lease


def test_reconcile_confirmed_success_records_receipt_and_succeeds(cluster):  # A05
    trace = f"trace-unk-succ-{uuid.uuid4().hex[:8]}"
    command_id, lease = _executing_command(cluster, object_id="schedule-unk-a", trace=trace)

    status, record = _reconcile(cluster, command_id=command_id, lease=lease,
                                source_state="SUCCEEDED", version=lease["record_version"], trace_id=trace)
    assert status == 200, record
    assert record["command_status"] == "SUCCEEDED"
    assert record["business_success"] is True
    assert record["posting_status"] == "NOT_CONFIRMED", "业务成功不等于已过账"
    assert record["recorded_receipt"]["authority_verified"] is True
    assert record["prohibited_actions"] == ["RESEND_NEW_KEY", "DIRECT_COMPENSATION"]


def test_reconcile_not_executed_retries_with_same_key(cluster):  # A05
    """只有**确认未执行**才允许重试，且必须用同一幂等键。"""
    trace = f"trace-unk-retry-{uuid.uuid4().hex[:8]}"
    command_id, lease = _executing_command(cluster, object_id="schedule-unk-b", trace=trace)

    status, record = _reconcile(cluster, command_id=command_id, lease=lease,
                                source_state="NOT_EXECUTED", version=lease["record_version"], trace_id=trace)
    assert status == 200, record
    assert record["command_status"] == "AUTHORIZED"
    assert record["next_action"] == "RETRY_SAME_KEY"
    assert record["retry_idempotency_key"] == record["idempotency_key"], "重试必须复用同一幂等键"
    assert record["retry_requires_fresh_review"] is True, "重试需重新走执行前复核"
    assert "RESEND_NEW_KEY" in record["prohibited_actions"]


def test_reconcile_unconfirmed_goes_to_manual_review(cluster):  # A05
    trace = f"trace-unk-manual-{uuid.uuid4().hex[:8]}"
    command_id, lease = _executing_command(cluster, object_id="schedule-unk-c", trace=trace)

    status, record = _reconcile(cluster, command_id=command_id, lease=lease,
                                source_state="UNKNOWN", version=lease["record_version"], trace_id=trace)
    assert status == 200, record
    assert record["command_status"] == "MANUAL_REVIEW"
    assert record["next_action"] == "MANUAL_REVIEW"
    assert record["manual_queue_ref"], "人工接管必须给出队列引用"
    assert record["business_success"] is False, "人工状态不得被当成成功"
    assert record["retry_idempotency_key"] is None


def test_reconcile_never_exposes_new_key_resend_or_direct_compensation(cluster):  # A05
    """任何源系统结论都不得把"换新键重发/直接补偿"作为候选动作。"""
    trace = f"trace-unk-guard-{uuid.uuid4().hex[:8]}"
    for index, state in enumerate(("SUCCEEDED", "NOT_EXECUTED", "UNKNOWN")):
        command_id, lease = _executing_command(cluster, object_id=f"schedule-unk-g{index}", trace=trace)
        status, record = _reconcile(cluster, command_id=command_id, lease=lease,
                                    source_state=state, version=lease["record_version"], trace_id=trace)
        assert status == 200, record
        # candidate_actions 是**分场景常量**（契约 allOf/then）：任何结论都不得出现禁止动作，\n        # 且必须落在允许动作全集内。\n        assert set(record["candidate_actions"]) <= {"QUERY_SOURCE", "RETRY_SAME_KEY", "MANUAL_REVIEW"}\n        assert set(record["candidate_actions"]) & {"RESEND_NEW_KEY", "DIRECT_COMPENSATION"} == set()
        assert record["prohibited_actions"] == ["RESEND_NEW_KEY", "DIRECT_COMPENSATION"]
        assert record["next_action"] not in ("RESEND_NEW_KEY", "DIRECT_COMPENSATION")


def test_reconcile_rejects_unknown_source_state(cluster):  # A06
    trace = f"trace-unk-bad-{uuid.uuid4().hex[:8]}"
    command_id, lease = _executing_command(cluster, object_id="schedule-unk-d", trace=trace)

    status, payload = _reconcile(cluster, command_id=command_id, lease=lease,
                                 source_state="TOTALLY_MADE_UP", version=lease["record_version"], trace_id=trace)
    assert status == 400, payload


def test_reconcile_requires_lease(cluster):  # A06
    """没有租约不得发起收敛（防止任意方伪造源系统结论）。"""
    trace = f"trace-unk-nolock-{uuid.uuid4().hex[:8]}"
    command_id, lease = _executing_command(cluster, object_id="schedule-unk-e", trace=trace)

    status, payload = _reconcile(cluster, command_id=command_id,
                                 lease={"lease_ref": "lease-forged", "fencing_token": 1},
                                 source_state="SUCCEEDED", version=lease["record_version"], trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "LEASE_NOT_HELD"


def test_reconciliation_record_readable_and_conforms_to_contract(cluster):  # A06
    trace = f"trace-unk-read-{uuid.uuid4().hex[:8]}"
    command_id, lease = _executing_command(cluster, object_id="schedule-unk-f", trace=trace)
    status, written = _reconcile(cluster, command_id=command_id, lease=lease,
                                 source_state="UNKNOWN", version=lease["record_version"], trace_id=trace)
    assert status == 200

    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/reconciliation",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200, record
    assert record["reconciliation_ref"] == written["reconciliation_ref"]
    assert record["query_attempt_count"] == written["query_attempt_count"]
    _assert_valid(record, "p3-unknown.json", "/$defs/Record", "ConvergenceRecord")


def test_reconciliation_404_before_any_convergence(cluster):  # A06
    trace = f"trace-unk-none-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-unk-none")
    approval_id = f"approval-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]

    status, payload = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/reconciliation",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 404, payload
    assert payload["code"] == "RECONCILIATION_NOT_FOUND"


# ==========================================================================
# 七、批准：绑定 → 事实 → 执行前复核（A06）
# ==========================================================================

def _create_binding(cluster, *, normalization_ref: str, trace_id: str, valid_until: str = "2099-01-01T00:00:00Z"):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "control-plane",
        "/internal/v1/approvals/bindings",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "normalization_ref": normalization_ref,
            "policy_evaluation_ref": f"policy-{uuid.uuid4().hex[:8]}",
            "idempotency_key": f"bind-{uuid.uuid4().hex[:8]}",
            "valid_until": valid_until,
        },
        headers=IDENTITY,
    )


def _decide(cluster, *, approval_id: str, binding: dict, approved: bool, trace_id: str,
            valid_until: str = "2099-01-01T00:00:00Z", actor_id: str = "user-approver"):
    """走 **P0 的批准决策接口**建立权威决定（批准事实与 P3 共用同一条）。"""
    context = {**context_body(trace_id=trace_id), "actor_id": actor_id}
    return cluster.post(
        "control-plane",
        f"/api/v1/approvals/{approval_id}/decisions",
        {
            **context,
            "context": context,
            "approval_id": approval_id,
            "approved": approved,
            "object_id": binding["object_id"],
            "payload_hash": binding["payload_hash"],
            "policy_version": binding["policy_version"],
            "tool_id": binding["tool_id"],
            "tool_version": binding["tool_version"],
            "action_scope": binding["action_scope"],
            "command_type": binding["command_type"],
            "payload_ref": binding["payload_ref"],
            "payload_schema": binding["payload_schema"],
            "normalization_ref": binding["normalization_ref"],
            "expected_version": binding["expected_version"],
            "valid_until": valid_until,
        },
        headers=IDENTITY,
    )


def _review(cluster, *, command_id: str, version: int, lease: dict, trace_id: str,
            fencing_token: int | None = None):
    return cluster.post(
        "control-plane",
        "/internal/v1/approvals/pre-execution-review",
        {
            "tenant_id": "tenant-demo",
            "context": context_body(trace_id=trace_id),
            "command_id": command_id,
            "command_record_version": version,
            "lease_ref": lease["lease_ref"],
            "fencing_token": lease["fencing_token"] if fencing_token is None else fencing_token,
        },
        headers=IDENTITY,
    )


def test_approval_chain_then_review_blocks_when_capability_not_admitted(cluster):  # A06
    """完整批准链路：绑定 → 决策 → 执行前复核。

    这里刻意验证**能力准入是硬闸门**：`pmc.dispatch.publish` 的
    `compensation=false`，即便批准齐备、租约有效，复核仍必须拒绝
    （原因 `reason-capability-not-admitted`）——无补偿能力的高风险写入
    不允许无人值守执行（§5 末段）。这是**正确行为**，不是链路缺陷。
    """
    trace = f"trace-appr-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-appr-a")
    status, binding_view = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)
    assert status == 200, binding_view
    approval_id = binding_view["approval_id"]
    # 绑定必须**复用**规范化哈希，不得由调用方另填
    assert binding_view["binding"]["payload_hash"] == view["binding"]["payload_hash"]

    assert _decide(cluster, approval_id=approval_id, binding=view["binding"],
                   approved=True, trace_id=trace)[0] == 200

    status, fact = cluster.get(
        "control-plane", f"/api/v1/approvals/{approval_id}/fact",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200, fact
    assert fact["approved"] is True
    assert fact["approver_ref"] == "user-approver"
    assert fact["status_source"] == "control-plane"

    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace)
    assert status == 200, review
    assert review["approval_valid"] is True, "批准本身必须有效"
    assert review["payload_hash_matches"] is True
    assert review["duties_separated"] is True
    assert review["capabilities_admitted"] is False, "缺补偿能力不得准入无人值守写入"
    assert review["allowed"] is False
    assert review["reason_ref"] == "reason-capability-not-admitted"


def test_review_allows_when_approval_and_capabilities_all_pass(cluster):  # A06
    """批准有效 + 能力位齐备时，复核必须放行（证明闸门不是"一律拒绝"）。

    用 `erp.inventory.reserve`（R2 写、三项能力位全 true）走完整链路；
    与 `..._blocks_when_capability_not_admitted` 的唯一差别就是 `compensation`，
    因此这条用例真正验证的是**能力准入位**。
    """
    trace = f"trace-appr-ok-{uuid.uuid4().hex[:8]}"
    status, view = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests/normalize",
        _normalize_body(tool_id="erp.inventory.reserve", object_id="reserve-appr-g",
                        input_ref="reserve-input-demo", trace_id=trace),
        headers=_headers(trace_id=trace),
    )
    assert status == 200, view

    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]
    assert _decide(cluster, approval_id=approval_id, binding=view["binding"],
                   approved=True, trace_id=trace)[0] == 200

    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace)
    assert status == 200, review
    assert review["approval_required"] is True, "R2 工具要求批准"
    assert review["capabilities_admitted"] is True, "能力位齐备必须准入"
    assert review["allowed"] is True, review.get("reason_ref")
    assert review["reason_ref"] is None
    for flag in ("approval_valid", "payload_hash_matches", "policy_version_matches",
                 "action_scope_matches", "object_version_matches", "current_permission_valid",
                 "delegation_valid", "duties_separated", "capabilities_admitted",
                 "lease_current", "fencing_current", "object_lock_held",
                 "not_cancelled_before_start"):
        assert review[flag] is True, flag


def test_review_denies_expired_approval_with_single_locatable_reason(cluster):  # A06
    trace = f"trace-appr-exp-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-appr-b")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]
    _decide(cluster, approval_id=approval_id, binding=view["binding"], approved=True,
            trace_id=trace, valid_until="2020-01-01T00:00:00Z")
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace)
    assert status == 200, review
    assert review["allowed"] is False
    assert review["approval_valid"] is False
    assert review["reason_ref"] == "reason-approval-expired", "拒绝必须给出**唯一**可定位原因"


def test_review_denies_when_duties_not_separated(cluster):  # A06
    """职责分离：审批人不得是发起人（**执行前复核**这道关口）。

    ## 2026-10-06 重写：两道关口，各测各的

    此前这条靠"用 `user-demo` 走批准接口"来构造"自己批自己"，
    而批准接口**现在就会拒绝**（`DUTIES_NOT_SEPARATED`，P3 SPEC 要求
    批准写入时即校验职责分离）——于是它测不到**执行前复核**那道关口了。

    所以改成：**批准事实用账本直接种**（模拟"规则收紧之前已存在的那类批准"），
    再验证 `revalidate_before_execution` 仍然**独立地**把它拦下。
    这样两道关口各自都有判据：

    | 关口 | 判据位置 |
    | --- | --- |
    | 批准写入 | `routes.py::_handler_approval_decision`（本文件另有 `test_self_approval_is_refused_at_decision_time`） |
    | 执行前复核 | **本条** |
    """
    trace = f"trace-appr-ds-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-appr-c")
    approval_id = f"approval-ds-{uuid.uuid4().hex[:8]}"
    # 直接种"发起人自批"的批准事实：**绕开**批准接口的关口，单测复核关口。
    # `approver_ref` 必须等于**命令的 actor**（`context_body` 的默认 actor），
    # 否则就不是"自批"，复核自然会判成 `duties_separated=True`。
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"], approver_ref="user-demo")
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace)
    assert status == 200, review
    assert review["duties_separated"] is False
    assert review["allowed"] is False
    assert review["reason_ref"] == "reason-duties-not-separated"


def test_self_approval_is_refused_at_decision_time(cluster):  # A06
    """职责分离：**批准写入时**就拒绝"发起人自批"（P3 SPEC 复核点表第一行）。

    ## 为什么这条必须单独存在

    上游 P3 SPEC 的复核点表把「批准决定写入」列为**独立的**复核点，
    要求校验「当前批准权限、**职责分离**、绑定、对象/策略、有效期、批准记录 CAS」，
    失败即**拒绝**。

    只在执行前复核时拦是不够的，后果有两条且都很具体：
    ① 台账里会留下一条**本不该存在**的批准事实——而事实追加不可变，**删不掉**；
    ② 界面会显示"已批准"，直到执行时才被拒 —— 正是本项目一贯要防的
       「显示成功而实际不行」。

    判据取的是**待批准记录里的 `binding.context.actor_id`**（受委托执行人，
    来自 Tool Gateway 的规范化结果），不是本次请求的 context——
    后者在"批准"这个动作里是**审批人自己**。
    """
    trace = f"trace-appr-self-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-self-approve")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]

    # 发起人是 context_body 的默认 actor；用它去批准自己 ⇒ 必须被拒
    status, payload = _decide(cluster, approval_id=approval_id, binding=view["binding"],
                              approved=True, trace_id=trace, actor_id="user-demo")
    assert status == 403, payload
    assert payload["code"] == "DUTIES_NOT_SEPARATED"

    # 换一个**合法审批人**（同一租户、不同 actor）⇒ 通过
    # 这条同时证明校验不是"一律拒绝"：它比的是身份，不是拒绝一切
    ok_status, ok_payload = _decide(cluster, approval_id=approval_id, binding=view["binding"],
                                    approved=True, trace_id=trace, actor_id="user-approver")
    assert ok_status == 200, ok_payload
    assert ok_payload["approved"] is True


def test_review_denies_stale_fencing_token(cluster):  # A06
    trace = f"trace-appr-fence-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-appr-d")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]
    _decide(cluster, approval_id=approval_id, binding=view["binding"], approved=True, trace_id=trace)
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace, fencing_token=lease["fencing_token"] + 7)
    assert status == 200, review
    assert review["fencing_current"] is False
    assert review["allowed"] is False


def test_review_denies_when_payload_changed_after_approval(cluster):  # A06
    """TOCTOU：批准后 payload 变了 → 复核必须拒绝（绑定指纹不一致）。"""
    trace = f"trace-appr-toctou-{uuid.uuid4().hex[:8]}"
    first = _normalize(cluster, trace_id=trace, object_id="schedule-appr-f", payload_ref="payload-a")
    approval_id = _create_binding(cluster, normalization_ref=first["normalization_ref"], trace_id=trace)[1]["approval_id"]
    _decide(cluster, approval_id=approval_id, binding=first["binding"], approved=True, trace_id=trace)

    # 用**另一个 payload** 提交命令，但引用同一批准
    second = _normalize(cluster, trace_id=trace, object_id="schedule-appr-f", payload_ref="payload-b")
    command_id = _submit(cluster, trace_id=trace, normalization_ref=second["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace)
    assert status == 200, review
    assert review["payload_hash_matches"] is False
    assert review["allowed"] is False
    assert review["reason_ref"] == "reason-approval-binding-changed"


def test_review_response_conforms_to_contract(cluster):  # A06
    trace = f"trace-appr-schema-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-appr-e")
    binding_view = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]
    _assert_valid(binding_view, "p3-approval.json", "/$defs/ApprovalBinding", "ApprovalBinding")

    approval_id = binding_view["approval_id"]
    _decide(cluster, approval_id=approval_id, binding=view["binding"], approved=True, trace_id=trace)
    status, fact = cluster.get(
        "control-plane", f"/api/v1/approvals/{approval_id}/fact",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200
    _assert_valid(fact, "p3-approval.json", "/$defs/Fact", "ApprovalFact")

    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]
    status, review = _review(cluster, command_id=command_id, version=lease["record_version"],
                             lease=lease, trace_id=trace)
    assert status == 200
    _assert_valid(review, "p3-approval.json", "/$defs/Review", "Review")


# ==========================================================================
# 八、Saga：实物先于账务 → 补偿 → 失败进人工队列（A08）
# ==========================================================================

def _create_saga(cluster, *, trace_id: str, steps: list[dict], idempotency_key: str | None = None):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "transaction-service",
        "/internal/v1/sagas",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "plan_ref": f"plan-{trace_id}",
            "plan_hash": "e" * 64,
            "idempotency_key": idempotency_key or f"saga-{uuid.uuid4().hex[:8]}",
            "steps": steps,
        },
        headers=IDENTITY,
    )


def _compensate(cluster, *, saga_id: str, step_id: str, version: int, trace_id: str):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "transaction-service",
        f"/internal/v1/sagas/{saga_id}/compensations",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "saga_id": saga_id,
            "expected_record_version": version,
            "step_id": step_id,
            "lease_ref": "lease-saga",
            "fencing_token": 1,
        },
        headers=IDENTITY,
    )


def _saga_get(cluster, *, saga_id: str, trace_id: str):
    return cluster.get(
        "transaction-service", f"/internal/v1/sagas/{saga_id}",
        headers={"X-Context": json.dumps(context_body(trace_id=trace_id))},
    )


def _step(step_id: str, domain: str, **overrides) -> dict:
    base = {
        "step_id": step_id,
        "domain": domain,
        "object_id": f"object-{step_id}",
        "expected_version": 1,
        "command_id": f"command-{step_id}",
        "command_key": f"key-{step_id}",
        "command_status": "SUCCEEDED",
        "started": True,
        "compensation_supported": True,
        "compensable_action_ref": f"action-reverse-{step_id}",
        "compensation_permission": {
            "policy_evaluation_ref": "policy-perm",
            "approval_ref": None,
            "action_scope": ["compensate.demo"],
            "valid_until": "2099-01-01T00:00:00Z",
            "permission_evidence_ref": "permission-evidence-demo",
        },
        "compensation_required": True,
    }
    base.update(overrides)
    return base


def _drive_compensation_to_receipt(cluster, *, view: dict, step_id: str, trace_id: str) -> dict:
    """把某步骤的补偿命令**真的跑完**，让它取得权威回执。

    ## 为什么要这一步（2026-10-05 修正）

    旧实现里，`saga_compensate` 会**直接写上一份 `authority_verified: True`
    的回执**并把整单标成 `COMPENSATED`——而那条补偿命令从未派发、从未执行。

    这违反上游 §6 / A14：**只有真实执行产生的权威回执能驱动成功**。

    现在补偿命令是一条**真实命令记录**（`AUTHORIZED`、无回执），
    必须走与其它命令同一条路才能完成。所以这个 helper 做的事就是
    "取租约 → 执行 → 收敛"，收敛那一步才会产生权威回执。

    这样 "`COMPENSATED`" 是**挣来的**，不是写上的。
    """
    step = next(s for s in view["steps"] if s["step_id"] == step_id)
    command_id = step["compensation"]["command_id"]

    # 补偿命令登记时 `record_version = 0`（与受控提交一致）⇒ 取租约用 expected=0；
    # 取到后以返回的版本执行。
    lease = _take_lease(
        cluster, command_id=command_id, version=0, worker=f"worker-comp-{step_id}", trace_id=trace_id
    )[1]
    status, _ = _execute(
        cluster, command_id=command_id, version=lease["record_version"], lease=lease, trace_id=trace_id
    )
    assert status == 202, "补偿命令的执行受理应为 202"
    status, record = _reconcile(
        cluster, command_id=command_id, lease=lease, source_state="SUCCEEDED",
        version=lease["record_version"], trace_id=trace_id,
    )
    assert status == 200, record
    return record


def test_saga_requires_physical_restore_before_accounting_compensation(cluster):  # A08
    """§5：MES/WMS 实物域恢复**先于** ERP 账务冲销——顺序不可颠倒。

    并且：**补偿完成必须凭已验证的权威回执**（见 `_drive_compensation_to_receipt`）。
    """
    trace = f"trace-saga-order-{uuid.uuid4().hex[:8]}"
    created = _create_saga(cluster, trace_id=trace, steps=[
        _step("s-physical", "PHYSICAL"),
        _step("s-accounting", "ACCOUNTING"),
    ])
    assert created[0] == 200, created
    saga_id = created[1]["saga_id"]

    # 先补偿账务 → 必须被拒（实物尚未核实恢复）
    status, payload = _compensate(cluster, saga_id=saga_id, step_id="s-accounting", version=0, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "PHYSICAL_RESTORE_REQUIRED"

    # 先补偿实物 → 受理，并留下恢复证据
    status, view = _compensate(cluster, saga_id=saga_id, step_id="s-physical", version=0, trace_id=trace)
    assert status == 200, view
    assert view["physical_verified_and_restored"] is True
    assert view["physical_evidence_refs"], "实物恢复必须有可核验证据"
    assert view["accounting_recovery_started"] is False, "实物未完成前不得开始账务恢复"
    # 契约用 prefixItems 固定两域顺序：先 PHYSICAL 后 ACCOUNTING
    assert [item["domain"] for item in view["recovery_order"]] == ["PHYSICAL", "ACCOUNTING"]
    # **补偿命令此时只是受理**：不得声称已成功、不得声称已有回执
    physical_step = next(s for s in view["steps"] if s["step_id"] == "s-physical")
    assert physical_step["compensation"]["command_status"] == "AUTHORIZED", (
        "补偿刚受理时只能是 AUTHORIZED——不得预先声称成功"
    )
    assert physical_step["compensation"]["authority_receipt"] is None, (
        "还没有真实回执时不得伪造一份（上游 §6）"
    )
    assert view["all_compensation_receipts_verified"] is False
    assert view["status"] != "COMPENSATED", "补偿命令尚未完成，整单不得报已补偿"

    # 再补偿账务 → 受理
    status, view = _compensate(cluster, saga_id=saga_id, step_id="s-accounting", version=1, trace_id=trace)
    assert status == 200, view
    assert view["accounting_recovery_started"] is True
    assert view["all_compensation_receipts_verified"] is False, "两条补偿都还没跑完"

    # **真的把两条补偿命令跑完**，才允许整单报已补偿
    _drive_compensation_to_receipt(cluster, view=view, step_id="s-physical", trace_id=trace)
    _drive_compensation_to_receipt(cluster, view=view, step_id="s-accounting", trace_id=trace)

    status, final = _saga_get(cluster, saga_id=saga_id, trace_id=trace)
    assert status == 200, final
    assert final["all_compensation_receipts_verified"] is True, (
        "两条补偿都取得了权威回执，才应判定为已验证："
        f"{[s.get('compensation') for s in final['steps']]}"
    )
    assert final["status"] == "COMPENSATED", final["status"]


def test_shipped_or_posted_steps_go_to_branches_and_manual_queue(cluster):  # A08
    """已发货/已过账必须走分支处理，且整单进人工队列——不得谎报已补偿。"""
    trace = f"trace-saga-branch-{uuid.uuid4().hex[:8]}"
    created = _create_saga(cluster, trace_id=trace, steps=[
        _step("s-shipped", "PHYSICAL", is_shipped=True),
        _step("s-posted", "ACCOUNTING", is_posted=True),
    ])
    saga_id = created[1]["saga_id"]

    status, view = _compensate(cluster, saga_id=saga_id, step_id="s-shipped", version=0, trace_id=trace)
    assert status == 200, view
    step = next(s for s in view["steps"] if s["step_id"] == "s-shipped")
    assert step["selected_branch"] == "shipped"
    assert step["compensation"]["action_ref"] == "action-return-or-credit-note"
    assert step["compensation"]["command_status"] == "MANUAL_REVIEW"
    assert view["status"] == "MANUAL_REVIEW"
    assert view["manual_queue_ref"], "人工状态必须有队列引用"
    assert view["all_compensation_receipts_verified"] is False


def test_non_compensable_step_goes_to_manual_review(cluster):  # A08
    """不可补偿的步骤 → 人工队列；Saga **不得**对外声称已完全补偿。"""
    trace = f"trace-saga-nocomp-{uuid.uuid4().hex[:8]}"
    created = _create_saga(cluster, trace_id=trace, steps=[
        _step("s-nocomp", "PHYSICAL", compensation_supported=False),
    ])
    saga_id = created[1]["saga_id"]

    status, view = _compensate(cluster, saga_id=saga_id, step_id="s-nocomp", version=0, trace_id=trace)
    assert status == 200, view
    assert view["status"] == "MANUAL_REVIEW"
    assert view["status"] != "COMPENSATED"
    assert view["manual_queue_ref"]
    assert view["all_compensation_receipts_verified"] is False


def test_saga_create_is_idempotent(cluster):  # A06
    trace = f"trace-saga-idem-{uuid.uuid4().hex[:8]}"
    key = f"saga-{uuid.uuid4().hex[:8]}"
    steps = [_step("s1", "PHYSICAL")]
    first = _create_saga(cluster, trace_id=trace, steps=steps, idempotency_key=key)
    second = _create_saga(cluster, trace_id=trace, steps=steps, idempotency_key=key)
    assert first[0] == 200 and second[0] == 200
    assert first[1]["saga_id"] == second[1]["saga_id"]


def test_saga_rejects_version_conflict_and_unknown_step(cluster):  # A06
    trace = f"trace-saga-err-{uuid.uuid4().hex[:8]}"
    created = _create_saga(cluster, trace_id=trace, steps=[_step("s1", "PHYSICAL")])
    saga_id = created[1]["saga_id"]

    status, payload = _compensate(cluster, saga_id=saga_id, step_id="s1", version=99, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "SAGA_VERSION_CONFLICT"

    status, payload = _compensate(cluster, saga_id=saga_id, step_id="not-a-step", version=0, trace_id=trace)
    assert status == 404, payload
    assert payload["code"] == "SAGA_STEP_NOT_FOUND"


def test_saga_view_conforms_to_contract(cluster):  # A06
    trace = f"trace-saga-schema-{uuid.uuid4().hex[:8]}"
    created = _create_saga(cluster, trace_id=trace, steps=[_step("s1", "PHYSICAL")])
    assert created[0] == 200
    _assert_valid(created[1], "p3-saga.json", "/$defs/Accepted", "SagaAccepted")
    saga_id = created[1]["saga_id"]

    status, view = _saga_get(cluster, saga_id=saga_id, trace_id=trace)
    assert status == 200
    _assert_valid(view, "p3-saga.json", "/$defs/View", "SagaView")


# ==========================================================================
# 九、并发（P3 完成条件：并发批准 / 并发同键提交）
# ==========================================================================

def test_approval_fact_is_append_only_and_replay_is_idempotent(cluster):  # A06
    """契约 `append_only_fact`：已成立的决定不可改判，重复同决定返回**同一事实**。

    - `identical_decision_replay: same_fact` → 重复同一决定必须幂等；
    - `different_decision_or_binding: 409` → 相反决定必须被拒。

    ⚠️ 2026-10-06：批准人改成 `user-approver`。此前的 `user-demo` 是**发起人**
    （`_create_binding` 用 `context_body` 的默认 actor），而"自己批自己"现在
    在**批准写入时**就被拒（`DUTIES_NOT_SEPARATED`，见 P3 SPEC 复核点表）。
    本条测的是**追加不可变**，不是职责分离，所以用合法审批人才能到达被测逻辑。
    """
    trace = f"trace-appr-append-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-append-a")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]

    status, first = _decide(cluster, approval_id=approval_id, binding=view["binding"],
                            approved=True, trace_id=trace, actor_id="user-approver")
    assert status == 200, first

    # 重复同一决定 → 幂等，返回同一事实（不得推进版本、不得换审批人）
    status, replay = _decide(cluster, approval_id=approval_id, binding=view["binding"],
                             approved=True, trace_id=trace, actor_id="user-approver")
    assert status == 200, replay
    assert replay["record_version"] == first["record_version"], "重复同决定不得推进版本"

    # 相反决定 → 409（事实追加不可变，不能改判）
    status, payload = _decide(cluster, approval_id=approval_id, binding=view["binding"],
                              approved=False, trace_id=trace, actor_id="user-approver")
    assert status == 409, payload
    assert payload["code"] == "APPROVAL_ALREADY_GRANTED"

    # 权威读必须仍是**原来的**批准
    status, fact = cluster.get(
        "control-plane", f"/api/v1/approvals/{approval_id}/fact",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200, fact
    assert fact["approved"] is True
    assert fact["approver_ref"] == first["actor_id"], "审批人不得被后来的请求改写"
    assert fact["record_version"] == first["record_version"]


def test_denied_approval_cannot_be_reversed_to_granted(cluster):  # A06
    """反向同样成立：先否决后不得改判为通过。

    ⚠️ 2026-10-06：第二次（改判）请求的 actor 改成 `user-approver`。
    此前用的是 `user-demo`，而那是**发起人**——现在会在批准写入时先被
    `DUTIES_NOT_SEPARATED` 拦掉（403），从而**测不到**本条的 409「已否决不可改判」。
    这里要用一个**合法审批人**去尝试改判，才走到被测的 CAS 逻辑。
    """
    trace = f"trace-appr-deny-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-append-b")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]

    assert _decide(cluster, approval_id=approval_id, binding=view["binding"],
                   approved=False, trace_id=trace, actor_id="user-approver")[0] == 200
    status, payload = _decide(cluster, approval_id=approval_id, binding=view["binding"],
                              approved=True, trace_id=trace, actor_id="user-approver")
    assert status == 409, payload
    assert payload["code"] == "APPROVAL_ALREADY_DENIED"


def test_concurrent_approvals_race_one_cas_winner(cluster):  # A06
    """**并发批准**：同一条批准事实的并发决策只能有一个赢。

    上游 §12 的乐观并发语义：`record_version` 做 CAS 前置条件，
    后到的决策者必须撞上版本冲突，而不是静默覆盖前一个决定。

    并发发出两个**相反**的决定（批准 / 否决），断言：
    - 恰好一个成功；
    - 另一个收到可定位的冲突（不允许"最后一个写入者获胜"）；
    - 最终事实与成功者一致（用**权威读**复核，而不是只看响应）。
    """
    import concurrent.futures

    trace = f"trace-conc-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-conc-a")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]

    def decide(approved: bool, actor: str):
        return _decide(cluster, approval_id=approval_id, binding=view["binding"],
                       approved=approved, trace_id=trace, actor_id=actor)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(decide, True, "user-demo"),
                   pool.submit(decide, False, "user-approver")]
        outcomes = [f.result() for f in futures]

    accepted = [body for status, body in outcomes if status == 200]
    rejected = [body for status, body in outcomes if status != 200]
    assert len(accepted) == 1, f"并发批准必须恰好一个成功，实际状态: {[s for s, _ in outcomes]}"
    assert len(rejected) == 1, f"另一个必须被拒，实际: {outcomes}"
    assert rejected[0].get("code"), f"拒绝必须带可定位错误码: {rejected[0]}"

    status, fact = cluster.get(
        "control-plane", f"/api/v1/approvals/{approval_id}/fact",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200, fact
    assert fact["approved"] == accepted[0]["approved"], "事实必须与唯一成功者一致"
    assert fact["approver_ref"] == accepted[0]["actor_id"], "事实的审批人必须与唯一成功者一致"


def test_concurrent_command_submit_same_key_creates_one_command(cluster):  # A04
    """并发同键提交只能产生**一个**命令（幂等键按租户+键原子生效）。"""
    import concurrent.futures

    trace = f"trace-conc-cmd-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-conc-b")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]
    assert _decide(cluster, approval_id=approval_id, binding=view["binding"],
                   approved=True, trace_id=trace)[0] == 200
    key = f"idem-{uuid.uuid4().hex[:8]}"

    def submit():
        return _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                       approval_ref=approval_id, idempotency_key=key)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = [f.result() for f in [pool.submit(submit) for _ in range(3)]]

    assert all(status == 202 for status, _ in results), results
    command_ids = {body["command_id"] for _, body in results}
    assert len(command_ids) == 1, f"并发同键提交必须只产生一个命令，实际: {command_ids}"


# ==========================================================================
# 十、取消：只停未开始步骤（§12）
# ==========================================================================

def _cancel_headers(trace_id: str) -> dict:
    return {"X-Context": json.dumps(context_body(trace_id=trace_id))}


def _new_task(cluster, *, trace_id: str) -> str:
    """建一个可取消的任务（P0 的受理入口）。"""
    status, task = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context_body(data_class="C2", trace_id=trace_id, task_id=None),
            "idempotency_key": f"p3-cancel-{uuid.uuid4().hex[:10]}",
            "input_ref": "schedule-demo",
            "input_schema": schema_binding(),
        },
    )
    assert status == 202, task
    return task["task_id"]


def _cancel_body(*, task_id: str, trace_id: str, expected_version: int, idempotency_key: str) -> dict:
    context = context_body(trace_id=trace_id, task_id=None)
    return {
        **context,
        "context": context,
        "task_id": task_id,
        "expected_task_version": expected_version,
        "idempotency_key": idempotency_key,
        "reason_ref": "reason-user-cancel",
    }


def test_cancel_request_stops_unstarted_and_never_claims_reversal(cluster):  # A06
    """没有任何在途步骤：未开始步骤被停止，且取消**不等于撤销**。"""
    trace = f"trace-cancel-{uuid.uuid4().hex[:8]}"
    task_id = _new_task(cluster, trace_id=trace)

    status, accepted = cluster.post(
        "control-plane",
        f"/api/v1/tasks/{task_id}/cancellation-requests",
        _cancel_body(task_id=task_id, trace_id=trace, expected_version=0,
                     idempotency_key=f"cancel-{uuid.uuid4().hex[:8]}"),
        headers=_cancel_headers(trace),
    )
    assert status == 202, accepted
    assert accepted["status"] == "accepted"
    assert accepted["business_success"] is False
    assert accepted["posting_status"] == "NOT_CONFIRMED"

    status, result = cluster.get(
        "control-plane", f"/api/v1/tasks/{task_id}/cancellation-requests/{accepted['request_id']}",
        headers=_cancel_headers(trace),
    )
    assert status == 200, result
    assert result["prevents_new_steps"] is True, "取消意图记录后不得再启动新步骤"
    assert result["task_cancelled_is_reversal"] is False, "取消绝不等于撤销"
    assert result["execution_status_source"] == "transaction-service"
    assert result["status_source"] == "control-plane"


def test_cancel_never_reports_cancelled_when_effects_started(cluster):  # A06
    """存在**已开始**步骤时：任务不得置 CANCELLED，需人工确认与适用补偿。

    ## 这条用例在 2026-10-06 被重写（原因是它原先**验不出东西**）

    旧版本用 `_write_truth` 在任务记录上种了两个**幽灵字段**：
    `command_refs: ["command-abc"]` 与 `started_command_refs: ["command-abc"]`。

    而实现侧那时的判据正是读 `task["started_command_refs"]`——
    两边**共享同一个不存在的字段**，于是用例"通过"了，
    但真实系统里那个字段**永远没人写**：取消任何任务都会把在途命令报成"未开始"。

    换句话说，**这条用例当时掩盖了它要防的缺陷**：
    它自造了实现所需的事实，所以永远看不到现实里那个事实不存在。

    ## 现在改成 seed **真命令**

    命令账本（`commands.jsonl`）是 Transaction 的权威记录（上游 §13）。
    在这里种两条命令：一条 `EXECUTING`（真的动过了）、一条 `AUTHORIZED`（尚未动），
    然后验证取消结果**分别**对待它们——这才是这条用例该有的判据。
    """
    trace = f"trace-cancel-started-{uuid.uuid4().hex[:8]}"
    task_id = _new_task(cluster, trace_id=trace)

    started_command = f"command-{uuid.uuid4().hex[:16]}"
    pending_command = f"command-{uuid.uuid4().hex[:16]}"
    _write_truth(
        cluster,
        "tasks",
        [{
            "task_id": task_id, "tenant_id": "tenant-demo", "status": "RUNNING",
            "record_version": 0, "updated_at": "2026-10-04T00:00:00Z",
            "idempotency_key": "seed-cancel", "result_refs": [],
            # 任务记录上的 `command_refs` **故意留空**：真实实现不读它
            # （命令清单由命令账本派生）。留空才是真实形态。
            "command_refs": [],
        }],
    )
    _write_truth(
        cluster,
        "commands",
        [
            {
                "command_id": started_command, "tenant_id": "tenant-demo",
                "task_id": task_id, "status": "EXECUTING",
                "idempotency_key": f"seed-started-{uuid.uuid4().hex[:8]}",
                "updated_at": "2026-10-04T00:00:01Z",
                "authority_receipt": None,
            },
            {
                "command_id": pending_command, "tenant_id": "tenant-demo",
                "task_id": task_id, "status": "AUTHORIZED",
                "idempotency_key": f"seed-pending-{uuid.uuid4().hex[:8]}",
                "updated_at": "2026-10-04T00:00:02Z",
                "authority_receipt": None,
            },
        ],
    )

    status, accepted = cluster.post(
        "control-plane",
        f"/api/v1/tasks/{task_id}/cancellation-requests",
        _cancel_body(task_id=task_id, trace_id=trace, expected_version=0,
                     idempotency_key=f"cancel-{uuid.uuid4().hex[:8]}"),
        headers=_cancel_headers(trace),
    )
    assert status == 202, accepted

    status, result = cluster.get(
        "control-plane", f"/api/v1/tasks/{task_id}/cancellation-requests/{accepted['request_id']}",
        headers=_cancel_headers(trace),
    )
    assert status == 200, result

    steps = {step["command_id"]: step for step in result["steps"]}
    assert set(steps) == {started_command, pending_command}, (
        f"必须列出该任务下的**全部**命令（由命令账本派生），实际 {sorted(steps)}"
    )

    started = steps[started_command]
    assert started["started"] is True, "EXECUTING 的命令必须算作已开始"
    assert started["stopped_before_start"] is False
    assert started["confirmation_required"] is True, (
        "已开始的副作用必须要求人工确认（上游 §12：不能把 Task 设为 CANCELLED 即假定撤销）"
    )

    pending = steps[pending_command]
    assert pending["started"] is False, "AUTHORIZED 尚未执行，算未开始"
    assert pending["stopped_before_start"] is True
    assert pending["confirmation_required"] is False

    assert result["task_status"] != "CANCELLED", "有已开始副作用时不得谎报已取消"
    assert result["task_status"] == "WAITING_HUMAN"
    assert result["manual_queue_ref"], "需人工确认时必须给出队列引用"


def test_cancel_rejects_stale_version_and_is_idempotent(cluster):  # A06
    trace = f"trace-cancel-ver-{uuid.uuid4().hex[:8]}"
    task_id = _new_task(cluster, trace_id=trace)
    key = f"cancel-{uuid.uuid4().hex[:8]}"

    status, payload = cluster.post(
        "control-plane",
        f"/api/v1/tasks/{task_id}/cancellation-requests",
        _cancel_body(task_id=task_id, trace_id=trace, expected_version=99, idempotency_key=key),
        headers=_cancel_headers(trace),
    )
    assert status == 409, payload
    assert payload["code"] == "TASK_VERSION_CONFLICT"

    def submit():
        return cluster.post(
            "control-plane",
            f"/api/v1/tasks/{task_id}/cancellation-requests",
            _cancel_body(task_id=task_id, trace_id=trace, expected_version=0, idempotency_key=key),
            headers=_cancel_headers(trace),
        )

    first = submit()
    second = submit()
    assert first[0] == 202 and second[0] == 202
    assert first[1]["request_id"] == second[1]["request_id"], "同幂等键必须返回同一取消请求"


def test_cancel_result_conforms_to_contract(cluster):  # A06
    trace = f"trace-cancel-schema-{uuid.uuid4().hex[:8]}"
    task_id = _new_task(cluster, trace_id=trace)
    status, accepted = cluster.post(
        "control-plane",
        f"/api/v1/tasks/{task_id}/cancellation-requests",
        _cancel_body(task_id=task_id, trace_id=trace, expected_version=0,
                     idempotency_key=f"cancel-{uuid.uuid4().hex[:8]}"),
        headers=_cancel_headers(trace),
    )
    assert status == 202
    _assert_valid(accepted, "p3-cancellation.json", "/$defs/Accepted", "CancellationAccepted")

    status, result = cluster.get(
        "control-plane", f"/api/v1/tasks/{task_id}/cancellation-requests/{accepted['request_id']}",
        headers=_cancel_headers(trace),
    )
    assert status == 200
    _assert_valid(result, "p3-cancellation.json", "/$defs/Result", "CancellationResult")


# ==========================================================================
# 十一、受控派发（仅 transaction-service 身份；入口再核凭证与 fencing）
# ==========================================================================

_TRANSACTION_IDENTITY = {"X-V3-Identity": "svc-transaction-service"}


def _executing_capable_command(cluster, *, trace: str, object_id: str):
    """造一个处于 EXECUTING 且能力齐备的命令，返回 (command_id, credential, lease)。"""
    status, view = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests/normalize",
        _normalize_body(tool_id="erp.inventory.reserve", object_id=object_id,
                        input_ref=f"{object_id}-input", trace_id=trace),
        headers=_headers(trace_id=trace),
    )
    assert status == 200, view
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]
    assert _decide(cluster, approval_id=approval_id, binding=view["binding"],
                   approved=True, trace_id=trace)[0] == 200

    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]
    assert _execute(cluster, command_id=command_id, version=lease["record_version"],
                    lease=lease, trace_id=trace)[0] == 202

    status, record = cluster.get(
        "transaction-service", f"/internal/v1/commands/{command_id}/execution-record",
        headers={"X-Context": json.dumps(context_body(trace_id=trace))},
    )
    assert status == 200 and record["status"] == "EXECUTING", record
    # 命令视图不含顶层 lease（契约无此字段），执行凭证里内嵌租约
    credential = record["execution_credential"]
    return command_id, credential, credential["lease"]


def _dispatch(cluster, *, command_id: str, credential_ref: str, lease: dict, trace_id: str,
              identity: dict | None = None, fencing_token: int | None = None):
    context = context_body(trace_id=trace_id)
    return cluster.post(
        "tool-gateway",
        "/internal/v1/tool-executions/controlled",
        {
            "tenant_id": "tenant-demo",
            "context": context,
            "command_id": command_id,
            "execution_credential_ref": credential_ref,
            "lease_ref": lease["lease_ref"],
            "fencing_token": lease["fencing_token"] if fencing_token is None else fencing_token,
        },
        headers=_TRANSACTION_IDENTITY if identity is None else identity,
    )


def test_controlled_dispatch_requires_transaction_identity(cluster):  # A14
    """契约 `x-required-caller`：只有 transaction-service 身份可派发。"""
    trace = f"trace-dispatch-id-{uuid.uuid4().hex[:8]}"
    command_id, credential, lease = _executing_capable_command(cluster, trace=trace, object_id="dispatch-a")

    # 非 transaction 身份（runtime 的服务账号） → 拒绝
    status, payload = _dispatch(cluster, command_id=command_id, credential_ref=credential["execution_ref"],
                                lease=lease, trace_id=trace, identity=IDENTITY)
    assert status == 403, payload
    assert payload["code"] == "CALLER_NOT_AUTHORIZED_FOR_CONTROLLED_EXECUTION"

    # 正确身份 → 受理，且仅受理
    status, accepted = _dispatch(cluster, command_id=command_id, credential_ref=credential["execution_ref"],
                                 lease=lease, trace_id=trace)
    assert status == 202, accepted
    assert accepted["status"] == "accepted"
    assert accepted["business_success"] is False
    assert accepted["posting_status"] == "NOT_CONFIRMED"


def test_controlled_dispatch_checks_credential_and_fencing(cluster):  # A06
    """执行入口必须**再次**核对凭证与 fencing（旧 worker 不得在 adapter 侧制造副作用）。"""
    trace = f"trace-dispatch-fence-{uuid.uuid4().hex[:8]}"
    command_id, credential, lease = _executing_capable_command(cluster, trace=trace, object_id="dispatch-b")

    # 凭证不匹配 → 409
    status, payload = _dispatch(cluster, command_id=command_id, credential_ref="exec-forged",
                                lease=lease, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "EXECUTION_CREDENTIAL_MISMATCH"

    # 旧 fencing token → 409
    status, payload = _dispatch(cluster, command_id=command_id, credential_ref=credential["execution_ref"],
                                lease=lease, trace_id=trace, fencing_token=lease["fencing_token"] + 5)
    assert status == 409, payload
    assert payload["code"] == "FENCING_TOKEN_STALE"

    # 租约引用不符 → 409
    status, payload = _dispatch(cluster, command_id=command_id, credential_ref=credential["execution_ref"],
                                lease={**lease, "lease_ref": "lease-forged"}, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "LEASE_NOT_HELD"


def test_controlled_dispatch_rejects_command_not_executing(cluster):  # A06
    """命令不在 EXECUTING 就派发 → 拒绝（不能跳过执行前复核）。"""
    trace = f"trace-dispatch-state-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="dispatch-c")
    approval_id = _create_binding(cluster, normalization_ref=view["normalization_ref"], trace_id=trace)[1]["approval_id"]
    _decide(cluster, approval_id=approval_id, binding=view["binding"], approved=True, trace_id=trace)
    command_id = _submit(cluster, trace_id=trace, normalization_ref=view["normalization_ref"],
                         approval_ref=approval_id)[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-a", trace_id=trace)[1]

    status, payload = _dispatch(cluster, command_id=command_id, credential_ref="exec-none",
                                lease=lease, trace_id=trace)
    assert status == 409, payload
    assert payload["code"] == "COMMAND_NOT_EXECUTING"


def test_controlled_dispatch_conforms_to_contract(cluster):  # A06
    trace = f"trace-dispatch-schema-{uuid.uuid4().hex[:8]}"
    command_id, credential, lease = _executing_capable_command(cluster, trace=trace, object_id="dispatch-d")
    status, accepted = _dispatch(cluster, command_id=command_id, credential_ref=credential["execution_ref"],
                                 lease=lease, trace_id=trace)
    assert status == 202
    _assert_valid(accepted, "p3-command.json", "/$defs/Accepted", "DispatchAccepted")


# ==========================================================================
# 四、响应体符合 P3 契约
# ==========================================================================

def _inline(node, current_document):
    if isinstance(node, dict):
        if "$ref" in node:
            reference = node["$ref"]
            file_part, _, pointer = reference.partition("#")
            document_name = file_part or current_document
            document = json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
            target = document
            for part in pointer.strip("/").split("/"):
                if part:
                    target = target[part]
            resolved = _inline(dict(target), document_name)
            extra = {key: value for key, value in node.items() if key != "$ref"}
            if extra:
                merged = dict(resolved)
                merged.update(_inline(extra, current_document))
                return merged
            return resolved
        return {key: _inline(value, current_document) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline(item, current_document) for item in node]
    return node


def _assert_valid(instance, document_name, pointer, label):
    document = json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
    node = document
    for part in pointer.strip("/").split("/"):
        if part:
            node = node[part]
    validator = Draft202012Validator(_inline(node, document_name))
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    assert not errors, f"{label} 不符合契约 {document_name}{pointer}: " + "; ".join(
        f"{'/'.join(str(part) for part in error.path)}: {error.message}" for error in errors[:4]
    )


def test_p3_responses_conform_to_contract(cluster):  # A06
    status, view = cluster.post(
        "tool-gateway", "/internal/v1/tool-requests/normalize", _normalize_body(), headers=_headers()
    )
    assert status == 200
    _assert_valid(view, "p3-approval.json", "/$defs/Normalized", "Normalized")
    _assert_valid(view["binding"], "p3-approval.json", "/$defs/Binding", "Binding")

    status, admission = cluster.get(
        "tool-gateway", "/internal/v1/tools/pmc.dispatch.publish/write-admission", headers=_headers()
    )
    assert status == 200
    _assert_valid(admission, "p3-tool-admission.json", "/$defs/Admission", "Admission")
