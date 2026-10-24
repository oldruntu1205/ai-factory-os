"""P4 Edge 现场判定核心语义验收（上游 §8 / §9 / §12）。

覆盖可以完全用 Mock 验证的部分：
Guard 判定顺序与不可覆盖项、发布状态机与行为性变化重开门禁、
spool 重连规则（去重 / TTL / 只补报事实）、断连自治、安全回退、影子处置。

与 `tests/p3_transactions` 的分工：那边是"受控写入"，这边是"现场判定与门禁"。
"""

from __future__ import annotations

import pytest

from services._kernel import edge

NOW = "2026-10-05T00:00:00Z"
FRESH = "2026-10-04T23:59:58Z"     # 2 秒前（阈值 5 秒内）
STALE = "2026-10-04T23:00:00Z"     # 1 小时前
FUTURE_POLICY = "2099-01-01T00:00:00Z"
EXPIRED_POLICY = "2020-01-01T00:00:00Z"


def healthy_input(**overrides) -> edge.GuardInput:
    base = dict(
        features={"pressure": 12.5, "speed": 120.0},
        required_features=("pressure", "speed"),
        feature_bounds={"pressure": (0.0, 100.0), "speed": (0.0, 200.0)},
        feature_units={"pressure": "kPa", "speed": "rpm"},
        expected_units={"pressure": "kPa", "speed": "rpm"},
        input_at=FRESH,
        max_age_seconds=5,
        now=NOW,
        in_applicability_domain=True,
        drift_within_threshold=True,
        model_version="1.0.0",
        expected_model_version="1.0.0",
        schema_version="1.0.0",
        expected_schema_version="1.0.0",
        calibration_valid=True,
        signature_policy_valid=True,
        license_valid=True,
        policy_expires_at=FUTURE_POLICY,
        device_id="device-demo",
        allowed_devices=("device-demo",),
        parameters={"setpoint": 12.0},
        parameter_boundaries={"setpoint": (0.0, 20.0)},
        interlock_passed=True,
        budget_exceeded=False,
        confidence=0.95,
        confidence_threshold=0.80,
        confidence_is_calibrated=True,
    )
    base.update(overrides)
    return edge.GuardInput(**base)


def evaluate(**overrides) -> dict:
    return edge.evaluate_guard(healthy_input(**overrides))


# ==========================================================================
# 一、Guard 判定优先级（§9 硬顺序）
# ==========================================================================

def test_healthy_input_is_allowed():  # A09
    result = evaluate()
    assert result["verdict"] == edge.Verdict.ALLOW
    assert result["reasons"] == []
    assert result["requires_device_baseline"] is False


def test_confidence_is_the_last_priority():  # A09
    """§9：结构性/安全类问题**全部优先于置信度**。"""
    assert result_stage_order_last_is_confidence()
    # 同时存在结构性问题与低置信度 → 原因必须指向结构性问题
    result = evaluate(features={"pressure": None, "speed": 120.0}, confidence=0.01)
    assert result["top_stage"] == "structure"
    assert result["top_reason"].startswith("MISSING_FIELD")
    assert not result["top_reason"].startswith("CONFIDENCE")


def result_stage_order_last_is_confidence() -> bool:
    return edge.GUARD_STAGES[-1] == "confidence"


@pytest.mark.parametrize(
    "overrides,expected_stage,expected_prefix",
    [
        (dict(features={"pressure": None, "speed": 120.0}), "structure", "MISSING_FIELD"),
        (dict(features={"pressure": float("nan"), "speed": 120.0}), "structure", "NAN_VALUE"),
        (dict(features={"pressure": 999.0, "speed": 120.0}), "structure", "OUT_OF_RANGE"),
        (dict(feature_units={"pressure": "psi", "speed": "rpm"}), "structure", "UNIT_UNKNOWN"),
        (dict(input_at=STALE), "freshness_timeout", "INPUT_STALE"),
        (dict(in_applicability_domain=False), "ood_drift", "OOD"),
        (dict(drift_within_threshold=False), "ood_drift", "DRIFT_EXCEEDED"),
        (dict(model_version="2.0.0"), "version", "VERSION_MISMATCH"),
        (dict(license_valid=False), "permit_policy", "LICENSE_EXPIRED"),
        (dict(policy_expires_at=EXPIRED_POLICY), "permit_policy", "POLICY_EXPIRED"),
        (dict(device_id="device-other"), "device_parameter_allowlist", "DEVICE_NOT_IN_LICENSE"),
        (dict(parameters={"setpoint": 999.0}), "device_parameter_allowlist", "PARAMETER_OUT_OF_BOUNDS"),
        (dict(interlock_passed=False), "interlock", "INTERLOCK_DENIED"),
        (dict(budget_exceeded=True), "budget", "BUDGET_EXCEEDED"),
        (dict(confidence=0.10), "confidence", "CONFIDENCE_BELOW_THRESHOLD"),
        (dict(confidence_is_calibrated=False), "confidence", "CONFIDENCE_NOT_CALIBRATED"),
    ],
)
def test_each_guard_stage_blocks_with_locatable_reason(overrides, expected_stage, expected_prefix):  # A11
    result = evaluate(**overrides)
    assert result["verdict"] in (edge.Verdict.DENY, edge.Verdict.FALLBACK)
    assert result["top_stage"] == expected_stage
    assert result["top_reason"].startswith(expected_prefix)


def test_confidence_cannot_override_any_earlier_stage():  # A09
    """把置信度拉到最高，前面任一阶段失败仍必须拒。"""
    for overrides in (
        dict(features={"pressure": None, "speed": 120.0}),
        dict(in_applicability_domain=False),
        dict(interlock_passed=False),
        dict(license_valid=False),
        dict(model_version="9.9.9"),
    ):
        result = evaluate(confidence=1.0, **overrides)
        assert result["verdict"] != edge.Verdict.ALLOW, overrides


def test_hard_interlock_cannot_be_overridden_by_human_approval():  # A09
    """§9 明文：人工批准**也不能覆盖硬互锁**。"""
    result = evaluate(interlock_passed=False, approval_present=True, confidence=1.0)
    assert result["verdict"] == edge.Verdict.FALLBACK
    assert "INTERLOCK_DENIED" in result["reasons"]
    assert result["interlock_overrides_approval"] is True
    assert "interlock" in result["approval_cannot_override"]


def test_llm_reported_confidence_is_not_acceptable():  # A09
    """§9：置信度必须是**经独立数据校准**的任务级指标，不能取 LLM 自报数字。"""
    result = evaluate(confidence=0.99, confidence_is_calibrated=False)
    assert result["verdict"] == edge.Verdict.DENY
    assert result["top_reason"] == "CONFIDENCE_NOT_CALIBRATED"


def test_structural_failures_take_precedence_within_stage():  # A09
    """同一阶段内多个失败也要稳定：缺失字段优先于越界（发现顺序稳定）。"""
    result = evaluate(features={"speed": 999.0})  # pressure 缺失 + speed 越界
    assert result["top_stage"] == "structure"
    assert result["top_reason"] == "MISSING_FIELD:pressure"


# ==========================================================================
# 二、发布状态机与行为性变化（§12 / §9）
# ==========================================================================

def test_release_states_match_upstream_contract():  # A09
    assert edge.EDGE_RELEASE_STATES == (
        "DRAFT", "SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED", "ACTIVE", "REVOKED",
    )


def test_release_can_only_advance_one_step_at_a_time():  # A09
    for index, state in enumerate(edge.EDGE_RELEASE_STATES[:-1]):
        nxt = edge.EDGE_RELEASE_STATES[index + 1]
        assert edge.release_transition_allowed(state, nxt) is True
    # 跳态被拒
    assert edge.release_transition_allowed("DRAFT", "SHADOW") is False
    assert edge.release_transition_allowed("DRAFT", "ACTIVE") is False
    # REVOKED 是终态
    assert edge.release_transition_allowed("REVOKED", "ACTIVE") is False
    assert edge.release_transition_allowed("REVOKED", "DRAFT") is False
    # 任意非终态可撤销
    for state in edge.EDGE_RELEASE_STATES[:-1]:
        assert edge.release_transition_allowed(state, "REVOKED") is True


@pytest.mark.parametrize(
    "kind,expected_stage",
    [
        ("model", "CALIBRATED"),
        ("calibration_parameters", "CALIBRATED"),
        ("action_mapping", "CALIBRATED"),
        ("schema", "SCHEMA_FROZEN"),
        ("feature_extractor", "SCHEMA_FROZEN"),
        ("tokenizer", "SCHEMA_FROZEN"),
    ],
)
def test_behavioral_change_reopens_gate_and_invalidates_approval(kind, expected_stage):  # A09
    """§9：影响行为的变化必须重开门禁，**不能替换文件继续用旧批准**。"""
    impact = edge.behavioral_change_impact([kind])
    assert impact["behavioral"] is True
    assert impact["requires_reapproval"] is True
    assert impact["invalidates_approval"] is True
    assert impact["required_stage"] == expected_stage


def test_multiple_behavioral_changes_take_earliest_required_stage():  # A09
    """多项同时变：必须回到**最靠前**的那个阶段（不能只回退到最后一项）。"""
    impact = edge.behavioral_change_impact(["model", "schema"])
    assert impact["required_stage"] == "SCHEMA_FROZEN"


def test_non_behavioral_change_does_not_invalidate_approval():  # A09
    impact = edge.behavioral_change_impact(["log_level"])
    assert impact["behavioral"] is False
    assert impact["invalidates_approval"] is False


# ==========================================================================
# 三、Spool 重连规则（§9 / A10）
# ==========================================================================

def entry(event_id: str, *, executed: bool, sequence: int, created_at: str = FRESH,
          boot_id: str = "boot-1", ttl: int = 3600) -> edge.SpoolEntry:
    return edge.SpoolEntry(
        edge_id="edge-demo", boot_id=boot_id, sequence=sequence, event_id=event_id,
        status="EXECUTED" if executed else "PENDING", created_at=created_at,
        ttl_seconds=ttl, executed=executed, payload_ref=f"payload-{event_id}",
    )


def test_executed_actions_are_reported_as_facts_never_replayed():  # A10
    """A10：已执行动作**只补报事实**，绝不进入待执行集合。"""
    result = edge.replayable_entries([entry("evt-done", executed=True, sequence=1)], now=NOW)
    assert len(result["facts_to_report"]) == 1
    assert result["facts_to_report"][0]["fact_only"] is True
    assert result["facts_to_report"][0]["replay_forbidden"] is True
    assert result["to_execute"] == [], "已执行动作不得进入待执行集合"
    assert result["replay_forbidden_event_ids"] == ["evt-done"]


def test_replay_of_executed_action_is_rejected():  # A10
    result = edge.reject_replay(entry("evt-done", executed=True, sequence=1))
    assert result["accepted"] is False
    assert result["reason"] == "REPLAY_OF_EXECUTED_ACTION_FORBIDDEN"


def test_pending_action_after_ttl_is_not_executed():  # A10
    """§9：历史待执行动作过 TTL 后**不执行**。"""
    old = entry("evt-old", executed=False, sequence=1, created_at="2026-10-04T20:00:00Z", ttl=3600)
    result = edge.replayable_entries([old], now=NOW)
    assert result["to_execute"] == []
    assert result["dropped_expired"][0]["reason"] == "TTL_EXPIRED"


def test_pending_action_within_ttl_is_executed():  # A09
    fresh = entry("evt-new", executed=False, sequence=1, created_at=FRESH)
    result = edge.replayable_entries([fresh], now=NOW)
    assert [item["event_id"] for item in result["to_execute"]] == ["evt-new"]
    assert result["dropped_expired"] == []


def test_reconnect_dedups_by_event_id():  # A10
    same = [entry("evt-dup", executed=False, sequence=1), entry("evt-dup", executed=False, sequence=2)]
    result = edge.replayable_entries(same, now=NOW)
    assert len(result["to_execute"]) == 1
    assert len(result["duplicates"]) == 1
    assert result["dedup_key"] == "event_id"


def test_reconnect_orders_by_boot_and_sequence():  # A09
    items = [
        entry("evt-b2", executed=True, sequence=2, boot_id="boot-2"),
        entry("evt-a1", executed=True, sequence=1, boot_id="boot-1"),
        entry("evt-b1", executed=True, sequence=1, boot_id="boot-2"),
    ]
    result = edge.replayable_entries(items, now=NOW)
    assert [item["event_id"] for item in result["facts_to_report"]] == ["evt-a1", "evt-b1", "evt-b2"]


# ==========================================================================
# 四、断连自治（§9）
# ==========================================================================

def _autonomy(**overrides):
    base = dict(
        release_state="ACTIVE",
        signature_policy_valid=True,
        license_valid=True,
        policy_expires_at=FUTURE_POLICY,
        version_consistent=True,
        local_persistence_available=True,
        now=NOW,
    )
    base.update(overrides)
    return edge.evaluate_disconnected_autonomy(**base)


def test_disconnected_autonomy_allowed_when_policy_and_license_valid():  # A09
    result = _autonomy()
    assert result["autonomy_allowed"] is True
    assert result["requires_device_baseline"] is False
    assert result["depends_on_center_human"] is False, "控制安全不得依赖中心人工及时响应"


@pytest.mark.parametrize(
    "overrides,blocker",
    [
        (dict(signature_policy_valid=False), "SIGNATURE_POLICY_INVALID"),
        (dict(license_valid=False), "LICENSE_EXPIRED"),
        (dict(policy_expires_at=EXPIRED_POLICY), "POLICY_EXPIRED"),
        (dict(version_consistent=False), "VERSION_MISMATCH"),
        (dict(local_persistence_available=False), "LOCAL_PERSISTENCE_UNAVAILABLE"),
        (dict(release_state="SHADOW"), "RELEASE_NOT_ACTIVE"),
    ],
)
def test_disconnected_autonomy_disabled_blocks_and_requires_baseline(overrides, blocker):  # A10
    result = _autonomy(**overrides)
    assert result["autonomy_allowed"] is False
    assert blocker in result["blockers"]
    assert result["requires_device_baseline"] is True


# ==========================================================================
# 五、安全回退动作：逐设备定义（§9）
# ==========================================================================

def test_fallback_action_comes_from_device_table_not_from_model():  # A09
    table = {"device-demo": "受控停机"}
    result = edge.resolve_fallback_action(
        device_id="device-demo", device_fallback_table=table, model_proposed="降速"
    )
    assert result["resolved"] is True
    assert result["action"] == "受控停机", "必须用登记值，不用模型建议"
    assert result["model_proposal_ignored"] is True


def test_missing_device_fallback_fails_closed():  # A09
    """未逐设备定义回退动作 → fail-closed 转人工，**不得即兴选择**。"""
    result = edge.resolve_fallback_action(
        device_id="device-unknown", device_fallback_table={}, model_proposed="受控停机"
    )
    assert result["resolved"] is False
    assert result["action"] is None
    assert result["requires_manual"] is True
    assert result["reason"] == "DEVICE_FALLBACK_NOT_DEFINED"


def test_fallback_table_rejects_action_outside_allowed_set():  # A09
    result = edge.resolve_fallback_action(
        device_id="device-demo", device_fallback_table={"device-demo": "模型自己想的动作"}
    )
    assert result["resolved"] is False, "不在允许集合内的动作不得被采用"


# ==========================================================================
# 六、影子模式（§9 / A09）
# ==========================================================================

def test_shadow_never_enables_control_write_even_with_full_confidence():  # A09
    """A09：影子模式无法写控制口；高置信也不能绕过门禁产生动作。"""
    guard = evaluate()
    assert guard["verdict"] == edge.Verdict.ALLOW
    shadow = edge.shadow_disposition(confidence=1.0, confidence_threshold=0.5, guard=guard)
    assert shadow["control_write_enabled"] is False
    assert shadow["physical_write_disabled"] is True
    assert shadow["network_write_disabled"] is True
    assert shadow["action_dispatched"] is False, "影子不得派发任何动作"
    assert shadow["comparison_recorded"] is True


def test_shadow_records_would_have_acted_for_comparison():  # A09
    """影子需要记录"本会采取的动作"用于比对，但不能真的执行。"""
    shadow = edge.shadow_disposition(confidence=1.0, confidence_threshold=0.5, guard=evaluate())
    assert shadow["would_have_acted"] is True
    assert shadow["action_dispatched"] is False


def test_shadow_with_failing_guard_records_no_would_have_acted():  # A09
    guard = evaluate(interlock_passed=False)
    shadow = edge.shadow_disposition(confidence=1.0, confidence_threshold=0.5, guard=guard)
    assert shadow["would_have_acted"] is False
    assert shadow["action_dispatched"] is False
