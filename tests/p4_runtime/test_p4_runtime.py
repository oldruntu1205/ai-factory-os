"""P4 Edge 运行验收：真实多进程 + 真实 HTTP + 契约形状校验（上游 §8 / §9 / §12）。

与 `tests/p4_edge/test_edge_semantics.py` 的分工：
- 那边验**判定语义**（纯函数，快）；
- 这边验**接口行为**：影子真的不产生动作、守卫真的按顺序拒、
  发布台账真的按七态推进、行为性变化真的让旧批准失效、spool 真的只补报事实。

A09 / A10 证据主要由本文件提供。
"""

from __future__ import annotations

import json
import uuid

from tests.p0_runtime.conftest import context_body


IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def _headers(trace_id: str) -> dict:
    return {"X-Context": json.dumps(context_body(trace_id=trace_id))}


def _body(trace_id: str, **extra) -> dict:
    context = context_body(trace_id=trace_id)
    return {**context, "context": context, "tenant_id": "tenant-demo", **extra}


def _create_release(cluster, *, trace_id: str, edge_id: str = "edge-demo") -> dict:
    release_id = f"release-{uuid.uuid4().hex[:12]}"
    status, payload = cluster.post(
        "control-plane",
        "/internal/v1/edge/releases",
        _body(
            trace_id,
            edge_id=edge_id,
            release_id=release_id,
            artifacts={
                "model_ref": "edge-model-demo",
                "model_version": "1.0.0",
                "schema_ref": "edge-input.json",
                "schema_version": "1.0.0",
                "feature_extractor_ref": "edge-features-demo",
                "tokenizer_ref": "edge-tokenizer-demo",
                "calibration_ref": "edge-calibration-demo",
                "action_mapping_ref": "edge-actions-demo",
            },
            evidence_refs=["edge-evidence-1"],
        ),
        headers=IDENTITY,
    )
    assert status == 200, payload
    return payload


def _advance(cluster, *, trace_id: str, release: dict, target: str) -> dict:
    status, payload = cluster.post(
        "control-plane",
        f"/internal/v1/edge/releases/{release['release_id']}/transitions",
        _body(
            trace_id,
            expected_version=release["record_version"],
            **{"from": release["status"], "to": target},
            gate_evidence=[],
            evidence_refs=[],
        ),
        headers=IDENTITY,
    )
    assert status == 200, payload
    return payload


# ==========================================================================
# 一、发布状态机（§12）
# ==========================================================================

def test_release_advances_only_through_seven_states_in_order(cluster):  # A09
    trace = f"trace-p4-release-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    assert release["status"] == "DRAFT"
    assert release["control_write_disabled"] is True
    assert release["ai_new_actions_enabled"] is False

    for target in ("SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED", "ACTIVE"):
        release = _advance(cluster, trace_id=trace, release=release, target=target)
        assert release["status"] == target
        # **影子/受限阶段都不得放开控制写入**（放开是 P5 的事）
        assert release["control_write_disabled"] is True, target
        assert release["ai_new_actions_enabled"] is False, target

    # 终态：REVOKED 之后不能再出发
    release = _advance(cluster, trace_id=trace, release=release, target="REVOKED")
    assert release["status"] == "REVOKED"
    status, payload = cluster.post(
        "control-plane",
        f"/internal/v1/edge/releases/{release['release_id']}/transitions",
        _body(trace, expected_version=release["record_version"], **{"from": "REVOKED", "to": "ACTIVE"},
              gate_evidence=[], evidence_refs=[]),
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "EDGE_RELEASE_TRANSITION_NOT_ALLOWED"


def test_release_rejects_state_jump_and_stale_version(cluster):  # A09
    trace = f"trace-p4-jump-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)

    status, payload = cluster.post(
        "control-plane",
        f"/internal/v1/edge/releases/{release['release_id']}/transitions",
        _body(trace, expected_version=release["record_version"], **{"from": "DRAFT", "to": "ACTIVE"},
              gate_evidence=[], evidence_refs=[]),
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "EDGE_RELEASE_TRANSITION_NOT_ALLOWED", "跳态必须被拒"

    status, payload = cluster.post(
        "control-plane",
        f"/internal/v1/edge/releases/{release['release_id']}/transitions",
        _body(trace, expected_version=99, **{"from": "DRAFT", "to": "SCHEMA_FROZEN"},
              gate_evidence=[], evidence_refs=[]),
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "EDGE_RELEASE_VERSION_CONFLICT"


def test_behavioral_change_reopens_gate_and_invalidates_old_approval(cluster):  # A09
    """§9：影响行为的变化必须重开门禁，**旧批准立即失效**。"""
    trace = f"trace-p4-reopen-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    for target in ("SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED"):
        release = _advance(cluster, trace_id=trace, release=release, target=target)

    replacement_id = f"release-{uuid.uuid4().hex[:12]}"
    status, reopen = cluster.post(
        "control-plane",
        f"/internal/v1/edge/releases/{release['release_id']}/gate-reopen",
        _body(
            trace,
            edge_id=release["edge_id"],
            previous_release_id=release["release_id"],
            replacement_release_id=replacement_id,
            expected_version=release["record_version"],
            changed_components=["model", "calibration_parameters"],
            old_artifacts=release["artifacts"],
            new_artifacts={**release["artifacts"], "model_version": "1.1.0"},
            evidence_refs=["edge-reopen-1"],
        ),
        headers=IDENTITY,
    )
    assert status == 200, reopen
    assert reopen["behavior_affecting"] is True
    assert reopen["previous_approval_invalidated"] is True
    assert reopen["offline_validation_required"] is True
    assert reopen["shadow_gate_required"] is True
    assert reopen["replacement_status"] == "DRAFT"

    # 旧台账必须**真的**失效：状态撤销且批准不再"当前"
    status, previous = cluster.get(
        "control-plane", f"/api/v1/edge/releases/{release['release_id']}",
        headers=_headers(trace),
    )
    assert status == 200, previous
    assert previous["status"] == "REVOKED"
    assert previous["approval_current"] is False


def test_gate_reopen_requires_behavioral_change(cluster):  # A09
    """非行为性变更不得走重开门禁（否则等于绕过门禁）。"""
    trace = f"trace-p4-reopen-nonbehav-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    status, payload = cluster.post(
        "control-plane",
        f"/internal/v1/edge/releases/{release['release_id']}/gate-reopen",
        _body(
            trace,
            edge_id=release["edge_id"],
            previous_release_id=release["release_id"],
            replacement_release_id=f"release-{uuid.uuid4().hex[:12]}",
            expected_version=release["record_version"],
            changed_components=["log_level"],
            old_artifacts=release["artifacts"],
            new_artifacts=release["artifacts"],
            evidence_refs=[],
        ),
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "GATE_REOPEN_REQUIRES_BEHAVIORAL_CHANGE"


# ==========================================================================
# 二、影子推理与 Guard 判定（§9 / A09）
# ==========================================================================

def _feature_and_inference(cluster, *, trace_id: str, release: dict) -> tuple[dict, dict]:
    status, features = cluster.post(
        "manufacturing-core", "/edge/v1/features/records",
        _body(trace_id, edge_id=release["edge_id"], release_id=release["release_id"],
              scenario_ref="scenario-demo", source_ref="edge-source-demo",
              source_hash="a" * 64, sampled_at="2026-10-05T00:00:00Z"),
        headers=IDENTITY,
    )
    assert status == 200, features
    assert features["mode"] == "shadow"
    assert features["validation_passed"] is True

    status, inference = cluster.post(
        "manufacturing-core", "/edge/v1/inferences/shadow",
        _body(trace_id, edge_id=release["edge_id"], release_id=release["release_id"],
              feature_record_ref=features["feature_record_ref"], scenario_ref="scenario-demo"),
        headers=IDENTITY,
    )
    assert status == 200, inference
    return features, inference


def test_shadow_inference_never_enables_execution(cluster):  # A09
    """A09：影子推理**永不允许执行**，且置信度未校准。"""
    trace = f"trace-p4-shadow-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    _features, inference = _feature_and_inference(cluster, trace_id=trace, release=release)

    assert inference["mode"] == "shadow"
    assert inference["comparison_only"] is True
    assert inference["execution_allowed"] is False
    assert inference["model_backend"] == "local"
    assert inference["confidence"]["calibrated"] is False, (
        "影子置信度未经独立数据校准，不得标为可用（§9）"
    )


def test_guard_evaluation_blocks_and_never_produces_action(cluster):  # A09
    """Guard 判定：影子阶段无许可证据 ⇒ 必然被拒；且**绝不产生动作**。"""
    trace = f"trace-p4-guard-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    features, inference = _feature_and_inference(cluster, trace_id=trace, release=release)

    status, result = cluster.post(
        "manufacturing-core", "/edge/v1/guards/evaluate",
        _body(
            trace,
            edge_id=release["edge_id"],
            release_id=release["release_id"],
            mode="shadow",
            feature_record_ref=features["feature_record_ref"],
            inference_record_ref=inference["inference_record_ref"],
            scenario_ref="scenario-demo",
            local_policy_ref="edge-policy-demo",
            local_permit_ref="edge-permit-demo",
            human_approval_ref=None,
            device_ref="device-demo",
        ),
        headers=IDENTITY,
    )
    assert status == 200, result

    # 三条恒定的安全声明
    assert result["control_write_disabled"] is True
    assert result["execution_allowed"] is False
    assert result["ai_new_actions_enabled"] is False
    assert result["human_approval_overrides_hard_checks"] is False
    assert result["confidence_overrides_hard_checks"] is False
    assert result["comparison_only"] is True
    assert result["write_disable_mechanism"] in ("physical", "network", "physical_and_network")

    # 判定顺序：置信度必须排在最后
    stages = [check["stage"] for check in result["checks"]]
    assert stages[-1] == "confidence", "置信度必须是最后一级（不能覆盖前面的硬检查）"

    # 无许可/互锁证据 ⇒ 不得评为可自动候选
    assert result["candidate_eligible"] is False
    assert result["first_rejection"] is not None


def test_guard_blocks_when_references_missing_before_confidence(cluster):  # A09
    """引用缺失属结构性问题，**优先于置信度**被报告。"""
    trace = f"trace-p4-guard-missing-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)

    status, result = cluster.post(
        "manufacturing-core", "/edge/v1/guards/evaluate",
        _body(
            trace,
            edge_id=release["edge_id"],
            release_id=release["release_id"],
            mode="shadow",
            feature_record_ref=f"features-does-not-exist-{uuid.uuid4().hex[:8]}",
            inference_record_ref=f"inference-does-not-exist-{uuid.uuid4().hex[:8]}",
            scenario_ref="scenario-demo",
            local_policy_ref="edge-policy-demo",
            local_permit_ref="edge-permit-demo",
            human_approval_ref=None,
            device_ref="device-demo",
        ),
        headers=IDENTITY,
    )
    assert status == 200, result
    assert result["candidate_eligible"] is False
    assert str(result["first_rejection"]).startswith("MISSING_REFERENCE"), (
        "结构性问题（引用缺失）必须排在置信度之前"
    )
    structure_check = next(c for c in result["checks"] if c["stage"] == "structure")
    assert structure_check["passed"] is False


# ==========================================================================
# 三、本地 spool（§9 / A10）
# ==========================================================================

def _spool_entry(*, kind: str, event_id: str, **extra) -> dict:
    base = {
        "edge_id": "edge-demo",
        "boot_id": "boot-1",
        "sequence": 1,
        "event_id": event_id,
        "event_hash": "b" * 64,
        "occurred_at": "2026-10-05T00:00:00Z",
        "aggregate_ref": "device-demo",
        "aggregate_version": 1,
        "kind": kind,
        "payload_ref": f"edge-payload-{event_id}",
        "payload_hash": "c" * 64,
    }
    base.update(extra)
    return base


def test_spool_executed_fact_is_never_center_executable(cluster):  # A10
    """A10：spool 里的已执行事实只作补报，**中心不得执行**。"""
    trace = f"trace-p4-spool-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "manufacturing-core", "/edge/v1/spool/entries",
        _body(trace, **_spool_entry(kind="executed_fact", event_id=f"evt-{uuid.uuid4().hex[:8]}",
                                    receipt_ref="edge-receipt-1", receipt_hash="d" * 64)),
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["kind"] == "executed_fact"
    assert payload["action_executed"] is True
    assert payload["center_execution_allowed"] is False, "中心不得执行 spool 中的事实"


def test_spool_pending_action_requires_ttl(cluster):  # A10
    """待执行动作**必须**有 TTL，否则过期判断无从下手（§9）。"""
    trace = f"trace-p4-spool-ttl-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "manufacturing-core", "/edge/v1/spool/entries",
        _body(trace, **_spool_entry(kind="pending_action", event_id=f"evt-{uuid.uuid4().hex[:8]}",
                                    command_id="command-demo", command_idempotency_key="key-demo")),
        headers=IDENTITY,
    )
    assert status == 400, payload
    assert "ttl_seconds" in json.dumps(payload)


def test_spool_entry_is_idempotent_by_event_id(cluster):  # A10
    trace = f"trace-p4-spool-idem-{uuid.uuid4().hex[:8]}"
    event_id = f"evt-{uuid.uuid4().hex[:8]}"
    payload = _spool_entry(kind="pending_action", event_id=event_id, ttl_seconds=600,
                           expires_at="2099-01-01T00:00:00Z", command_id="command-demo",
                           command_idempotency_key="key-demo")
    first = cluster.post("manufacturing-core", "/edge/v1/spool/entries", _body(trace, **payload), headers=IDENTITY)
    second = cluster.post("manufacturing-core", "/edge/v1/spool/entries", _body(trace, **payload), headers=IDENTITY)
    assert first[0] == 200 and second[0] == 200
    assert first[1]["occurred_at"] == second[1]["occurred_at"]
    assert second[1]["center_execution_allowed"] is False


# ==========================================================================
# 四、断连自治（§9）
# ==========================================================================

def _autonomy(cluster, *, trace_id: str, release: dict, **overrides) -> tuple[int, dict]:
    payload = {
        "edge_id": release["edge_id"],
        "release_id": release["release_id"],
        "center_connected": False,
        "local_policy_ref": "edge-policy-demo",
        "local_permit_ref": "edge-permit-demo",
        "site_procedure_ref": "edge-procedure-demo",
        "device_ref": "device-demo",
        "signature_policy_valid": True,
        "license_valid": True,
        "version_consistent": True,
        "local_persistence_available": True,
        "policy_expires_at": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return cluster.post("manufacturing-core", "/edge/v1/autonomy/evaluate", _body(trace_id, **payload), headers=IDENTITY)


def test_disconnected_autonomy_requires_active_release(cluster):  # A10
    """SHADOW 状态的发布**不得**自治（还没到受限闭环）。"""
    trace = f"trace-p4-autonomy-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    release = _advance(cluster, trace_id=trace, release=release, target="SCHEMA_FROZEN")

    status, decision = _autonomy(cluster, trace_id=trace, release=release)
    assert status == 200, decision
    assert decision["autonomy_eligible"] is False
    assert decision["ai_new_actions_enabled"] is False
    assert decision["return_to_device_baseline"] is True
    assert decision["control_safety_requires_center_response"] is False, (
        "控制安全不得依赖中心人工及时响应（§9）"
    )


def test_autonomy_fails_closed_when_local_persistence_unavailable(cluster):  # A10
    """本地持久化不可用 ⇒ 禁用 AI 新动作并回设备基线（§9 明文）。"""
    trace = f"trace-p4-autonomy-persist-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    status, decision = _autonomy(
        cluster, trace_id=trace, release=release, local_persistence_available=False
    )
    assert status == 200, decision
    assert decision["autonomy_eligible"] is False
    assert decision["return_to_device_baseline"] is True
    assert decision["checks"]["local_persistence_available"] is False


def _threshold_body(threshold_id: str, **overrides) -> dict:
    body = {
        "threshold_id": threshold_id,
        "scenario_ref": "scenario-demo",
        "behavior_hash": "e" * 64,
        "threshold_hash": "f" * 64,
        "coverage_spec_ref": "edge-coverage-spec",
        "coverage_spec_hash": "1" * 64,
        "metrics_spec_ref": "edge-metrics-spec",
        "metrics_spec_hash": "2" * 64,
        "minimum_continuous_seconds": 14 * 24 * 3600,
        "required_conditions": ["工况A"],
        "minimum_coverage_ratio": 0.9,
        "maximum_deviation": 0.05,
        "deviation_unit_ref": "edge-unit-demo",
        "maximum_latency_ms": {"p95": 200, "p99": 500},
        "maximum_timeout_rate": 0.01,
        "maximum_false_positive_rate": 0.02,
        "maximum_false_negative_rate": 0.02,
        "maximum_rejection_rate": 0.1,
        "calibration_package_ref": "edge-calibration-demo",
        "calibration_package_hash": "3" * 64,
        "frozen_at": "2026-10-05T00:00:00Z",
        "frozen_by_ref": "user-approver",
    }
    body.update(overrides)
    return body


def test_frozen_threshold_cannot_be_shorter_than_fourteen_days(cluster):  # A09
    """§9：影子观察**最低连续 14 天**；工况不足需延长，**不得缩短门槛**。"""
    trace = f"trace-p4-threshold-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "control-plane",
        "/internal/v1/edge/shadow-thresholds/freeze",
        _body(trace, **_threshold_body(f"threshold-{uuid.uuid4().hex[:12]}",
                                       minimum_continuous_seconds=7 * 24 * 3600)),
        headers=IDENTITY,
    )
    assert status == 400, payload
    assert payload["code"] == "SHADOW_MINIMUM_OBSERVATION_TOO_SHORT"


def test_frozen_threshold_is_immutable_after_freezing(cluster):  # A09
    """门槛一旦冻结就返回既有记录，**不得**被新请求覆盖（否则等于事后改标准）。"""
    trace = f"trace-p4-threshold-immutable-{uuid.uuid4().hex[:8]}"
    threshold_id = f"threshold-{uuid.uuid4().hex[:12]}"

    first = cluster.post(
        "control-plane", "/internal/v1/edge/shadow-thresholds/freeze",
        _body(trace, **_threshold_body(threshold_id)), headers=IDENTITY,
    )
    assert first[0] == 200, first[1]

    # 想"放宽门槛"：必须无效——冻结值原样返回
    relaxed = _threshold_body(threshold_id, minimum_coverage_ratio=0.1, threshold_hash="9" * 64)
    second = cluster.post(
        "control-plane", "/internal/v1/edge/shadow-thresholds/freeze",
        _body(trace, **relaxed), headers=IDENTITY,
    )
    assert second[0] == 200
    assert second[1]["minimum_coverage_ratio"] == 0.9, "已冻结门槛不得被覆盖"
    assert second[1]["threshold_hash"] == "f" * 64


def test_shadow_report_rejects_unfrozen_threshold(cluster):  # A09
    """报告必须引用**已冻结**门槛；不得自带标准（§9）。"""
    trace = f"trace-p4-report-unfrozen-{uuid.uuid4().hex[:8]}"
    release = _create_release(cluster, trace_id=trace)
    status, payload = cluster.post(
        "manufacturing-core", "/edge/v1/shadow-reports",
        _body(
            trace,
            release_id=release["release_id"],
            edge_id=release["edge_id"],
            scenario_ref="scenario-demo",
            threshold_ref=f"threshold-never-frozen-{uuid.uuid4().hex[:8]}",
            threshold_hash="f" * 64,
            behavior_hash="e" * 64,
            started_at="2026-10-01T00:00:00Z",
            ended_at="2026-10-15T00:00:00Z",
            continuous_observation_seconds=14 * 24 * 3600,
            condition_measurements=[],
            measured_coverage_ratio=0.99,
            measured_deviation=0.01,
            deviation_unit_ref="edge-unit-demo",
            latency_ms={"p95": 100},
            sample_count=1000,
            false_positive_count=0,
            false_negative_count=0,
            false_positive_denominator=1000,
            false_negative_denominator=1000,
            false_positive_rate=0.0,
            false_negative_rate=0.0,
            rejection_rate=0.0,
            timeout_rate=0.0,
            measurement_spec_ref="edge-measurement-spec",
            measurement_spec_hash="4" * 64,
            hardware_manifest_ref="edge-hardware-manifest",
            hardware_manifest_hash="5" * 64,
        ),
        headers=IDENTITY,
    )
    assert status == 409, payload
    assert payload["code"] == "SHADOW_THRESHOLD_NOT_FROZEN"


def test_spool_batch_rejects_non_facts_only_report(cluster):  # A10
    """重连补报必须是「只补报事实」；`report_facts_only` 非 true 即拒（§9）。"""
    trace = f"trace-p4-spool-batch-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "trajectory-eval", "/internal/v1/edge/spool/facts",
        _body(
            trace,
            edge_id="edge-demo",
            batch_ref=f"batch-{uuid.uuid4().hex[:8]}",
            entries=[],
            report_facts_only=False,
        ),
        headers=IDENTITY,
    )
    assert status == 400, payload
    assert payload["code"] == "SPOOL_REPORT_MUST_BE_FACTS_ONLY"


def test_device_fallback_must_be_engineer_defined(cluster):  # A09
    """回退动作必须由设备工程师逐设备定义，且**不得**由模型选择（§9）。"""
    trace = f"trace-p4-fallback-{uuid.uuid4().hex[:8]}"
    device_ref = f"device-{uuid.uuid4().hex[:8]}"
    fallback_body = {
        "device_ref": device_ref,
        "definition_ref": f"fallback-def-{device_ref}",
        "version": "1.0.0",
        "action": "受控停机",
        "defined_by_ref": "user-approver",
        "device_engineer_evidence_ref": "edge-engineer-evidence-1",
        "trigger_spec_ref": "edge-trigger-spec",
        "parameter_boundary_spec_ref": "edge-param-spec",
        "baseline_spec_ref": "edge-baseline-spec",
        "interlock_spec_ref": "edge-interlock-spec",
        "site_procedure_ref": "edge-procedure-demo",
        "approved_at": "2026-10-05T00:00:00Z",
    }

    # 未登记 → 404（fail-closed，不得即兴选择）
    status, payload = cluster.get(
        "manufacturing-core", f"/edge/v1/device-fallbacks/{device_ref}",
        headers=_headers(trace),
    )
    assert status == 404, payload
    assert payload["code"] == "DEVICE_FALLBACK_NOT_DEFINED"

    # 不在允许集合的动作 → 拒
    status, payload = cluster.post(
        "control-plane", "/internal/v1/edge/device-fallbacks/freeze",
        _body(trace, **{**fallback_body, "action": "模型自己想的动作"}), headers=IDENTITY,
    )
    assert status == 400, payload
    assert payload["code"] == "FALLBACK_ACTION_NOT_ALLOWED"

    # 合法登记 → 200，且 model_selected 恒为 false
    status, payload = cluster.post(
        "control-plane", "/internal/v1/edge/device-fallbacks/freeze",
        _body(trace, **fallback_body), headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["action"] == "受控停机"
    assert payload["model_selected"] is False
    assert payload["frozen"] is True


# ==========================================================================
# 五、路由表结构自检（防"静默丢路由"）
# ==========================================================================

def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]


def test_route_registry_has_no_duplicate_owner_keys():  # A09
    """路由表不得有重复 owner 键——字典后者覆盖前者，会**静默丢弃整组路由**。

    实测教训：往 `routes.py` 插入 P4 路由时误插进已有键内部、并多出一个同名
    `"edge"` 键，Python 字典直接覆盖 → 整组 edge 路由消失，
    表现是接口 404，而**编译与契约静态门禁全绿**
    （契约文件与运行期路由表是两回事）。这类陷阱只有机械检查防得住。
    """
    import re
    import sys

    repo_root = _repo_root()
    sys.path.insert(0, str(repo_root))
    from services._kernel import routes as routes_module

    source = (repo_root / "services" / "_kernel" / "routes.py").read_text(encoding="utf-8")
    keys = re.findall(r'^\s{4}"([a-z-]+)":\s*\[', source, flags=re.MULTILINE)
    assert len(keys) == len(set(keys)), f"ROUTES_BY_OWNER 存在重复 owner 键：{keys}"

    empty = [
        owner
        for owner, entries in routes_module.ROUTES_BY_OWNER.items()
        if owner != "evidence" and not entries
    ]
    assert not empty, f"以下 owner 的路由表为空，多半被重复键覆盖：{empty}"


def test_p4_contract_paths_are_all_reachable_by_some_service():  # A09
    """P4 契约里的每个路径都必须能被**某个运行单元**实际服务到。

    契约文件齐全 ≠ 接口可达；这一步把"契约 ↔ 运行期路由表"接上，
    避免再出现"契约有 17 条、实际只注册 9 条"却无人发现的情况。
    """
    import json
    import sys

    repo_root = _repo_root()
    sys.path.insert(0, str(repo_root))
    from services._kernel import routes as routes_module

    openapi = json.loads(
        (repo_root / "packages" / "contracts" / "openapi" / "p4.openapi.json").read_text(encoding="utf-8")
    )
    reachable: set[tuple[str, str]] = set()
    for service_id in routes_module.OWNER_BY_SERVICE:
        for route in routes_module.routes_for(service_id):
            reachable.add((route.method.upper(), route.pattern))

    missing = [
        f"{method.upper()} {path}"
        for path, ops in openapi["paths"].items()
        for method in ops
        if (method.upper(), path) not in reachable
    ]
    assert not missing, "以下 P4 契约路径没有任何运行单元能服务：\n  " + "\n  ".join(missing)
