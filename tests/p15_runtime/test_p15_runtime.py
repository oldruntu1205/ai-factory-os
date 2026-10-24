"""P1.5 运行验收：跨 owner 权威读 / 事件一致性 / DLQ / 权威回执。

逐条对应 SPEC `docs/v3-upgrade/03-阶段-P1.5-持久化与事件一致性.md` 的 L3 断言表。
"""

from __future__ import annotations

import uuid

import pytest
from jsonschema import Draft202012Validator

from tests.p0_runtime.conftest import REPO_ROOT, context_body, schema_binding

from .conftest import IDENTITY, deliver, envelope, p15_body, p15_context, read_jsonl, state_file

SCHEMAS = REPO_ROOT / "packages" / "contracts" / "schemas"


# ==========================================================================
# 一、跨 owner 权威读（单一真相源）
# ==========================================================================

def _accept_task(cluster, *, trace_id: str, data_class: str = "C2") -> str:
    status, task = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context_body(data_class=data_class, trace_id=trace_id, task_id=None),
            "idempotency_key": f"p15-{uuid.uuid4().hex[:10]}",
            "input_ref": "schedule-demo",
            "input_schema": schema_binding(),
        },
    )
    assert status == 202, task
    return task["task_id"]


def test_cross_owner_read_is_tenant_scoped(cluster):  # A01
    """跨 owner 权威读必须按租户隔离：另一个租户读不到，且不泄露存在性。

    覆盖"读接口忘了按租户过滤"这类缺陷——只有加这条断言才拦得住（变异测试实测的盲区）。
    """
    trace = f"trace-scope-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, trace_id=trace)

    # 本租户读得到
    ok_ctx = {"X-Context": __import__("json").dumps(context_body(trace_id=trace, task_id=None))}
    assert cluster.get("control-plane", f"/internal/v1/tasks/{task_id}", headers=ok_ctx)[0] == 200

    # 另一个租户（tenant-c3）读同一 task_id：必须 404，不能返回内容
    other = context_body(tenant="tenant-c3", trace_id=trace, task_id=None, data_class="C2")
    status, payload = cluster.get(
        "control-plane", f"/internal/v1/tasks/{task_id}", headers={"X-Context": __import__("json").dumps(other)}
    )
    assert status == 404, payload
    assert payload["code"] == "TASK_NOT_FOUND"
    assert task_id not in __import__("json").dumps(payload), "错误体不得回显对象标识"

    # Run 的权威读同样按租户隔离
    status, run = cluster.post(
        "hermes-runtime",
        "/internal/v1/runs",
        {**context_body(task_id="task-demo"), "input_ref": "input-demo", "runtime_version": "1.0.0-mock"},
    )
    assert status == 202
    status, payload = cluster.get(
        "hermes-runtime",
        f"/internal/v1/runs/{run['run_id']}",
        headers={"X-Context": __import__("json").dumps(other)},
    )
    assert status == 404, payload
    assert payload["code"] == "RUN_NOT_FOUND"


def test_self_declared_tenant_must_match_trusted_identity(cluster):  # A07
    """契约：tenant 必须来自可信认证，**不能采信请求自填的 tenant**。

    覆盖"自填 tenant 不再与可信身份核对"这类缺陷：去掉该校验后，任何服务身份
    都能通过改 `X-V3-Tenant` 把请求打到别的租户数据域（变异测试实测的盲区）。
    """
    trace = f"trace-ident-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, trace_id=trace)
    xcontext = {"X-Context": __import__("json").dumps(context_body(trace_id=trace, task_id=None))}

    # 1) 服务身份声明一个它**未获授权**的租户 → 拒绝
    status, payload = cluster.get(
        "control-plane",
        f"/internal/v1/tasks/{task_id}",
        headers={**xcontext, "X-V3-Identity": "svc-hermes-runtime", "X-V3-Tenant": "tenant-other"},
    )
    assert status == 403, payload
    assert payload["code"] in ("TENANT_IDENTITY_MISMATCH", "TENANT_NOT_REGISTERED")

    # 2) 声明一个获授权的租户，但 Context 写的是另一个租户 → 同样拒绝
    status, payload = cluster.get(
        "control-plane",
        f"/internal/v1/tasks/{task_id}",
        headers={**xcontext, "X-V3-Identity": "svc-hermes-runtime", "X-V3-Tenant": "tenant-c3"},
    )
    assert status == 403, payload

    # 3) 声明与 Context 一致的租户 → 放行（证明拒绝不是"一律拒绝"的假阳性）
    status, payload = cluster.get(
        "control-plane",
        f"/internal/v1/tasks/{task_id}",
        headers={**xcontext, "X-V3-Identity": "svc-hermes-runtime", "X-V3-Tenant": "tenant-demo"},
    )
    assert status == 200, payload
    assert payload["status_source"] == "control-plane"


def test_cross_owner_task_read_returns_same_fact(cluster):  # A07
    """manufacturing-core 通过 P1.5 契约读 Task，与 control-plane 自己读到的一致。"""
    trace = f"trace-p15-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, trace_id=trace)
    xcontext = {"X-Context": __import__("json").dumps(context_body(trace_id=trace, task_id=None))}

    status, owner_view = cluster.get("control-plane", f"/internal/v1/tasks/{task_id}", headers=xcontext)
    assert status == 200, owner_view
    status, consumer_view = cluster.get(
        "manufacturing-core", f"/internal/v1/tasks/{task_id}", headers=xcontext
    )
    assert status == 200, consumer_view
    assert consumer_view["status"] == owner_view["status"]
    assert consumer_view["status_source"] == "control-plane"
    assert consumer_view["business_execution_source"] == "transaction-service"
    assert consumer_view["is_posting_evidence"] is False


def test_task_view_never_claims_posting_evidence(cluster):  # A07
    """编排/受理状态绝不能作为过账证据（上游 §12：受理 ≠ 过账）。"""
    trace = f"trace-post-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, trace_id=trace)
    status, view = cluster.get(
        "control-plane",
        f"/internal/v1/tasks/{task_id}",
        headers={"X-Context": __import__("json").dumps(context_body(trace_id=trace, task_id=None))},
    )
    assert status == 200
    assert view["is_posting_evidence"] is False
    assert view["status"] == "ACCEPTED"


def test_cross_owner_run_read_includes_checkpoint_version(cluster):  # A07
    status, run = cluster.post(
        "hermes-runtime",
        "/internal/v1/runs",
        {**context_body(task_id="task-demo"), "input_ref": "input-demo", "runtime_version": "1.0.0-mock"},
    )
    assert status == 202, run
    run_id = run["run_id"]

    status, view = cluster.get(
        "hermes-runtime",
        f"/internal/v1/runs/{run_id}",
        headers={"X-Context": __import__("json").dumps(context_body())},
    )
    assert status == 200, view
    assert view["status_source"] == "hermes-runtime"
    assert view["record_version"] == 0
    assert view["is_posting_evidence"] is False

    resume = {
        **context_body(),
        "run_id": run_id,
        "expected_checkpoint_version": 0,
        "checkpoint_ref": "checkpoint-demo",
        "fencing_token": "fence-demo",
        "idempotency_key": "resume-demo",
    }
    assert cluster.post("hermes-runtime", f"/internal/v1/runs/{run_id}/resume", resume)[0] == 200
    status, view = cluster.get(
        "hermes-runtime",
        f"/internal/v1/runs/{run_id}",
        headers={"X-Context": __import__("json").dumps(context_body())},
    )
    assert status == 200
    assert view["record_version"] == 1, "检查点版本必须反映到权威读视图"


# ==========================================================================
# 二、事件一致性
# ==========================================================================

def test_duplicate_delivery_is_deduplicated(cluster):  # A07,A14
    evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}")
    status, first = deliver(cluster, service_id="manufacturing-core", evt=evt)
    assert status == 200, first
    assert first["disposition"] == "APPLIED"
    assert first["business_success"] is False, "ACK 不代表业务成功"

    status, second = deliver(cluster, service_id="manufacturing-core", evt=evt)
    assert status == 200
    assert second["disposition"] == "DUPLICATE"

    applied = [
        item
        for item in read_jsonl(state_file(cluster, "projection/manufacturing-core"))
        if item["aggregate_id"] == evt["aggregate_id"]
    ]
    assert len(applied) == 1, "重复投递不得把投影推进两次"


def test_version_gap_queues_then_drains(cluster):  # A07
    aggregate = f"command-{uuid.uuid4().hex[:8]}"
    base = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}", aggregate_id=aggregate, aggregate_version=0)
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=base)
    assert status == 200 and ack["disposition"] == "APPLIED"

    # 跳过 v1 直接投 v2
    gap_evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}", aggregate_id=aggregate, aggregate_version=2)
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=gap_evt)
    assert status == 200
    assert ack["disposition"] == "GAP"
    assert ack["pull_queue_ref"], "缺版本必须给出补拉队列引用"

    # 补上 v1
    fill = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}", aggregate_id=aggregate, aggregate_version=1)
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=fill)
    assert status == 200 and ack["disposition"] == "APPLIED"

    # 重投 v2：必须能应用，不能被误判为"已见过"
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=gap_evt)
    assert status == 200
    assert ack["disposition"] == "APPLIED", "补拉到位后重投必须能应用"

    projection = [
        item
        for item in read_jsonl(state_file(cluster, "projection/manufacturing-core"))
        if item["aggregate_id"] == aggregate
    ]
    assert projection[-1]["aggregate_version"] == 2


def test_late_event_does_not_overwrite_newer_fact(cluster):  # A07
    aggregate = f"command-{uuid.uuid4().hex[:8]}"
    for version in (0, 1):
        evt = envelope(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            aggregate_id=aggregate,
            aggregate_version=version,
            event_type="command.accepted" if version == 0 else "command.succeeded",
        )
        status, ack = deliver(cluster, service_id="manufacturing-core", evt=evt)
        assert status == 200 and ack["disposition"] == "APPLIED"

    late = envelope(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        aggregate_id=aggregate,
        aggregate_version=0,
        event_type="command.unknown",
    )
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=late)
    assert status == 200
    assert ack["disposition"] == "LATE"

    projection = [
        item
        for item in read_jsonl(state_file(cluster, "projection/manufacturing-core"))
        if item["aggregate_id"] == aggregate
    ][-1]
    assert projection["aggregate_version"] == 1, "迟到事件不得把投影拉回旧版本"
    assert projection["last_event_type"] == "command.succeeded", "迟到事件不得改写最后事实"


def test_event_requires_publication_evidence_and_valid_envelope(cluster):  # A07
    evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}")
    # 缺发布证据引用
    status, payload = cluster.post(
        "manufacturing-core",
        "/internal/v1/events",
        {**p15_body(), "event": evt},
        headers=IDENTITY,
    )
    assert status == 400, payload

    # 结构非法（缺必填）→ 进 DLQ，不静默丢弃
    broken = dict(evt)
    broken.pop("aggregate_version")
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=broken)
    assert status == 200, ack
    assert ack["disposition"] == "DLQ"
    assert ack["dlq_ref"]


def test_payload_must_not_carry_body_text(cluster):  # A07
    """事件正文只允许受控引用：带原文的 payload 必须被拒。"""
    evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}")
    evt["payload"] = "这里是原始业务正文"
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=evt)
    assert status == 200
    assert ack["disposition"] == "DLQ", "带正文的事件必须被挡在 DLQ，而不是被应用"


# ==========================================================================
# 三、DLQ 与重放
# ==========================================================================

def _make_dlq_entry(cluster) -> str:
    evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}")
    broken = dict(evt)
    broken.pop("occurred_at")
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=broken)
    assert status == 200 and ack["disposition"] == "DLQ"
    return ack["dlq_ref"]


def test_dlq_entry_exposes_reason_attempts_and_handling_status(cluster):  # A13
    dlq_id = _make_dlq_entry(cluster)
    status, entry = cluster.get(
        "manufacturing-core",
        f"/internal/v1/events/dlq/{dlq_id}",
        headers={"X-Context": __import__("json").dumps(p15_context())},
    )
    assert status == 200, entry
    assert entry["dlq_id"] == dlq_id
    assert entry["attempt_count"] >= 1
    assert entry["replay_count"] == 0
    assert entry["handling_status"] in ("OPEN", "MANUAL_REVIEW")
    assert entry["error"]["code"] and entry["error"]["message"]
    assert set(entry["error"]) == {"code", "message", "trace_id", "retryable", "details"}


def test_dlq_replay_obeys_dedup_and_version_rules(cluster):  # A07
    """重放仍走原去重规则：把 DLQ 里的事件补成合法后再投，去重不得被绕过。"""
    # 先正常投递 v0 并应用
    aggregate = f"command-{uuid.uuid4().hex[:8]}"
    evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}", aggregate_id=aggregate, aggregate_version=0)
    assert deliver(cluster, service_id="manufacturing-core", evt=evt)[1]["disposition"] == "APPLIED"

    # 把同一个事件投成"非法"制造 DLQ 条目（同 event_id，缺字段）
    broken = dict(evt)
    broken.pop("received_at")
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=broken)
    assert ack["disposition"] == "DLQ"
    dlq_id = ack["dlq_ref"]

    # 重放：该 event_id 已应用过 → 去重必须生效（不产生第二次副作用）
    status, replay = cluster.post(
        "manufacturing-core",
        f"/internal/v1/events/dlq/{dlq_id}/replay",
        {**p15_body(), "dlq_id": dlq_id, "idempotency_key": "replay-demo",
         "expected_dlq_version": 0, "reason_code": "reason-demo"},
        headers=IDENTITY,
    )
    assert status == 202, replay
    assert replay["status"] == "accepted"
    assert replay["business_success"] is False, "重放受理不代表业务成功"
    assert replay["posting_status"] == "NOT_CONFIRMED"
    assert replay["status_url"].endswith(dlq_id)

    status, entry = cluster.get(
        "manufacturing-core",
        f"/internal/v1/events/dlq/{dlq_id}",
        headers={"X-Context": __import__("json").dumps(p15_context())},
    )
    assert status == 200
    assert entry["replay_count"] == 1
    assert entry["handling_status"] in ("RESOLVED", "REPLAY_QUEUED")

    projection = [
        item
        for item in read_jsonl(state_file(cluster, "projection/manufacturing-core"))
        if item["aggregate_id"] == aggregate
    ]
    assert len(projection) == 1, "重放已应用事件不得产生第二次投影写入"


def test_dlq_replay_requires_optimistic_version(cluster):  # A07
    dlq_id = _make_dlq_entry(cluster)
    status, payload = cluster.post(
        "manufacturing-core",
        f"/internal/v1/events/dlq/{dlq_id}/replay",
        {**p15_body(), "dlq_id": dlq_id, "idempotency_key": "replay-demo",
         "expected_dlq_version": 99, "reason_code": "reason-demo"},
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "DLQ_VERSION_CONFLICT"


# ==========================================================================
# 四、权威回执驱动成功
# ==========================================================================

def _create_command(cluster, *, idempotency_key: str) -> dict:
    status, result = cluster.post(
        "tool-gateway",
        "/internal/v1/tool-requests",
        {
            **context_body(),
            "tool_id": "pmc.dispatch.publish",
            "tool_version": "1.0.0-mock",
            "input_ref": "schedule-demo",
            "input_schema": schema_binding(),
            "idempotency_key": idempotency_key,
            "expected_version": 6,
        },
    )
    assert status == 202, result
    return result["command"]


def test_receipt_drives_business_success(cluster):  # A14
    command = _create_command(cluster, idempotency_key=f"p15-{uuid.uuid4().hex[:8]}")
    command_id = command["command_id"]

    # 权威回执前：命令不得是成功
    status, before = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}",
        headers={"X-Context": __import__("json").dumps(context_body())},
    )
    assert status == 200
    assert before["status"] != "SUCCEEDED"

    receipts = read_jsonl(state_file(cluster, "commands"))
    stored_hash = [item for item in receipts if item["command_id"] == command_id][-1]["payload_hash"]

    status, receipt = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/receipts",
        {
            **p15_body(),
            "command_id": command_id,
            "idempotency_key": f"p15-{uuid.uuid4().hex[:8]}",
            "command_payload_hash": stored_hash,
            "receipt_id": f"receipt-{uuid.uuid4().hex[:8]}",
            "source_system": "pmc-mock",
            "authority_receipt_ref": f"pmc-receipt-{command_id}",
            "authority_receipt_hash": "sha256:authority",
        },
        headers=IDENTITY,
    )
    assert status == 200, receipt
    assert receipt["business_success"] is True
    assert receipt["command_status"] == "SUCCEEDED"
    assert receipt["authority_verified"] is True
    # 业务成功 ≠ 账务已过账
    assert receipt["posting_status"] == "NOT_CONFIRMED"

    status, after = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{command_id}",
        headers={"X-Context": __import__("json").dumps(context_body())},
    )
    assert status == 200
    assert after["status"] == "SUCCEEDED"
    assert after["receipt_refs"] == [f"pmc-receipt-{command_id}"]


def test_receipt_payload_hash_must_match_command(cluster):  # A07
    command = _create_command(cluster, idempotency_key=f"p15-{uuid.uuid4().hex[:8]}")
    status, payload = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command['command_id']}/receipts",
        {
            **p15_body(),
            "command_id": command["command_id"],
            "idempotency_key": "k",
            "command_payload_hash": "sha256:wrong",
            "receipt_id": "receipt-wrong",
            "source_system": "pmc-mock",
            "authority_receipt_ref": "ref",
            "authority_receipt_hash": "hash",
        },
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "RECEIPT_PAYLOAD_MISMATCH"


def test_receipt_is_idempotent(cluster):  # A07
    command = _create_command(cluster, idempotency_key=f"p15-{uuid.uuid4().hex[:8]}")
    receipts = read_jsonl(state_file(cluster, "commands"))
    stored_hash = [item for item in receipts if item["command_id"] == command["command_id"]][-1]["payload_hash"]
    body = {
        **p15_body(),
        "command_id": command["command_id"],
        "idempotency_key": "k",
        "command_payload_hash": stored_hash,
        "receipt_id": f"receipt-{uuid.uuid4().hex[:8]}",
        "source_system": "pmc-mock",
        "authority_receipt_ref": "ref-x",
        "authority_receipt_hash": "hash-x",
    }
    first = cluster.post(
        "transaction-service", f"/internal/v1/commands/{command['command_id']}/receipts", body, headers=IDENTITY
    )
    second = cluster.post(
        "transaction-service", f"/internal/v1/commands/{command['command_id']}/receipts", body, headers=IDENTITY
    )
    assert first[0] == 200 and second[0] == 200
    assert first[1]["receipt_id"] == second[1]["receipt_id"]
    assert first[1]["confirmed_at"] == second[1]["confirmed_at"], "同回执重复提交必须返回同一结果"


def test_unknown_command_cannot_be_receipted_without_query(cluster):  # A07
    """契约：UNKNOWN 必须先查源系统确认，不能靠"收到回执"直接改判。"""
    command = _create_command(cluster, idempotency_key=f"p15-{uuid.uuid4().hex[:8]}")
    command_id = command["command_id"]
    from services._kernel import routes as kernel_routes

    kernel_routes.advance_command("tenant-demo", command_id, status="UNKNOWN")

    receipts = read_jsonl(state_file(cluster, "commands"))
    stored_hash = [item for item in receipts if item["command_id"] == command_id][-1]["payload_hash"]
    status, payload = cluster.post(
        "transaction-service",
        f"/internal/v1/commands/{command_id}/receipts",
        {
            **p15_body(),
            "command_id": command_id,
            "idempotency_key": "k",
            "command_payload_hash": stored_hash,
            "receipt_id": "receipt-unknown",
            "source_system": "pmc-mock",
            "authority_receipt_ref": "ref",
            "authority_receipt_hash": "hash",
        },
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "COMMAND_STATE_UNKNOWN_REQUIRES_QUERY"


# ==========================================================================
# 五、响应体符合 P1.5 契约
# ==========================================================================

def _inline(node, current_document):
    import json as _json

    if isinstance(node, dict):
        if "$ref" in node:
            reference = node["$ref"]
            file_part, _, pointer = reference.partition("#")
            document_name = file_part or current_document
            document = _json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
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
    import json as _json

    document = _json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
    node = document
    for part in pointer.strip("/").split("/"):
        if part:
            node = node[part]
    validator = Draft202012Validator(_inline(node, document_name))
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    assert not errors, f"{label} 不符合契约 {document_name}{pointer}: " + "; ".join(
        f"{'/'.join(str(part) for part in error.path)}: {error.message}" for error in errors[:4]
    )


def test_p15_responses_conform_to_contract(cluster):  # A07
    import json as _json

    trace = f"trace-schema-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, trace_id=trace)
    xcontext = {"X-Context": _json.dumps(context_body(trace_id=trace, task_id=None))}

    status, task_view = cluster.get("control-plane", f"/internal/v1/tasks/{task_id}", headers=xcontext)
    assert status == 200
    _assert_valid(task_view, "p15-lifecycle.json", "/$defs/TaskView", "TaskView")

    status, run = cluster.post(
        "hermes-runtime",
        "/internal/v1/runs",
        {**context_body(task_id="task-demo"), "input_ref": "input-demo", "runtime_version": "1.0.0-mock"},
    )
    assert status == 202
    status, run_view = cluster.get(
        "hermes-runtime", f"/internal/v1/runs/{run['run_id']}", headers={"X-Context": _json.dumps(context_body())}
    )
    assert status == 200
    _assert_valid(run_view, "p15-lifecycle.json", "/$defs/RunView", "RunView")

    evt = envelope(event_id=f"evt-{uuid.uuid4().hex[:12]}")
    status, ack = deliver(cluster, service_id="manufacturing-core", evt=evt)
    assert status == 200
    _assert_valid(ack, "p15-event-api.json", "/$defs/DeliveryAck", "DeliveryAck")

    dlq_id = _make_dlq_entry(cluster)
    status, entry = cluster.get(
        "manufacturing-core",
        f"/internal/v1/events/dlq/{dlq_id}",
        headers={"X-Context": _json.dumps(p15_context())},
    )
    assert status == 200
    _assert_valid(entry, "p15-dlq.json", "", "DeadLetter")

    status, replay = cluster.post(
        "manufacturing-core",
        f"/internal/v1/events/dlq/{dlq_id}/replay",
        {**p15_body(), "dlq_id": dlq_id, "idempotency_key": "replay-schema",
         "expected_dlq_version": 0, "reason_code": "reason-demo"},
        headers=IDENTITY,
    )
    assert status == 202
    _assert_valid(replay, "p15-event-api.json", "/$defs/ReplayAccepted", "ReplayAccepted")
