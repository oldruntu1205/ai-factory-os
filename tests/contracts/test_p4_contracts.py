"""P4 静态契约门禁：仅读取 packages/contracts，无业务实现/设备/网络依赖。

Schema 正反例证明契约可表达并拒绝危险记录；不证明 ACL、签名、持久化或14天运行。
跨记录时间/hash/设备事实由 SPEC 的 L3 断言验证。本交付按指令未运行测试。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .contract_inventory import files_of, missing, unregistered

CONTRACT = Path(__file__).resolve().parents[2] / "packages" / "contracts"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
P4_FILES = {
    "openapi/p4.openapi.json", "schemas/p4-release.json", "schemas/p4-shadow.json",
    "schemas/p4-guard.json", "schemas/p4-spool.json", "schemas/p4-autonomy.json",
    "schemas/p4-fallback.json", "schemas/p4-scenario.json", "schemas/p4-observation.json",
}
# 交付前已读取的34个原文件字节；常量不从运行时文件生成，不导入旧测试的期望值。
BASELINE_SHA256 = {
    "openapi/p0.openapi.json": "54adb747a58653c24bfafefcc41e953e99f53191d9f86a001d5840b31f4599f7",
    "openapi/p1.openapi.json": "d5ccbbf61a092de2fe58d10957858f627f02783cf881d76a4bd44c1b11f9f9ae",
    "openapi/p15.openapi.json": "a06d1653ac4c57f2e3789f3d99211b4d1ef62631692b82357aefe92ef2b5e031",
    "openapi/p3.openapi.json": "6a09955a82d39e811b8db4cedf1d8fcbd76d1e9ce8435b9e374e6addeee8a5ae",
    "schemas/api-models.json": "c9e8ddcde87db8cc91c8779f304aa018fd5b4d53308822caeaee5baf3ed81b98",
    "schemas/command-status.json": "cf095bcd0d4298252a3a60c334a49854d6fa93bbd29ba4197844983a5004f830",
    "schemas/command.json": "f05a98db6030b0003eb3207ca3a57ca46c40d605c06e0f76437e7f44b6ec9840",
    "schemas/common-context.json": "e6914b5b9db71c0f97bc9b88bed0243ed733102679378146d31987be44847061",
    "schemas/decision-result.json": "86a7155cece330eb2da5c50a56a5a5621be7961214b5ed143c65af6db909d12f",
    "schemas/error.json": "8baef3c7db6a63c79fd71fee0694a7b76bac5cf06bb26c620299254591fb781e",
    "schemas/event-envelope.json": "04a6b5639606d269499dc74070b8e7fe790a22f02090a3c3a28d9116dca4db9b",
    "schemas/p1-classification.json": "06675ec042d27c089337b7df71fc6663f9235d102152b54df593650f10d5c98d",
    "schemas/p1-egress-audit.json": "c06904d65016539c2e59129644f79b00c1fc9921e15a2bc838b170f7678e9f08",
    "schemas/p1-governance.json": "1b9c82c79bc471b0869f1991bf473fcd07208f22e17703c6c40cef6c79d8bcf7",
    "schemas/p1-knowledge-source.json": "52c7aa7c48101428f85cabdb1bcce542c62c650e59c12d967213edfa4d0a651b",
    "schemas/p1-readonly.json": "e39d7bb82e81a14a845c9ebb0dd631ac10a9b18eea65e0aef72347d4c7bc4efb",
    "schemas/p1-token-vault.json": "7914da64d1e94a1456722f1647e0a53bbd5fcc03f86cee6ee06cffe17970cadd",
    "schemas/p15-dlq.json": "04cae8884d500445cbfe3b2101186eaa85448e08d22742c701a10a821b4436a8",
    "schemas/p15-event-api.json": "a214d08f5a96795fe2b9a471c0465972d6be56fb27fca777b1df7031fdb55c26",
    "schemas/p15-inbox.json": "0bf9b25acfbdd0b6d20207469747df183220687cac05115b59c44e47f1c1a158",
    "schemas/p15-lifecycle.json": "ef1513dea4c92e9f9df704deebbebd384fd1b499d4e5e26228e8c9080b83a2a7",
    "schemas/p15-outbox.json": "3cc07f388abb3f838b7c6fc27b387a2078dc1d86272000934f6a77b37e1451fc",
    "schemas/p15-receipt.json": "9516c83ef0ad7c5d276c19f7ed7fe68acefce1f8a553011fd26d4e61331bd3c0",
    "schemas/p3-approval.json": "66319a3b164e7bcb5c6c85406b80fd8ac8da144e21878e1345ae96b212491a2f",
    "schemas/p3-cancellation.json": "0e283c63883a40de87841c5bd489d79fd494469bf6cec2557aa2caece6338133",
    "schemas/p3-command.json": "33b9396c64fc572fc42ce30a133cd254fa1786a1499911fe5db70e6e452bd15f",
    "schemas/p3-employee.json": "a12faa7b4dacf9935bf4300ca52fae812259ac114027221f7fdfe7fb7430decc",
    "schemas/p3-execution.json": "182d4bc67dc72d2f3942f887108f22e90a43bce75029756bd8a90670eedf61a0",
    "schemas/p3-saga.json": "0bd360a6d3d80ba6e622ba45e12617d67c83f91f62c2a97cbc2499b8e642782d",
    "schemas/p3-tool-admission.json": "45698635161d105b740bcb2e8dceddd49e137ae3347e94b82452fbf6eb302af2",
    "schemas/p3-unknown.json": "ae491a963815d2e37074794b6aaede6d51e1f16aab931e9859282dae70c5057e",
    "schemas/service-manifest.json": "f406573fdbc6706f661f8ee1c385e0226387a737812687b85217fe27d2522e84",
    "schemas/state-machines.json": "1cc989f9f6119e437678ee14a7d293f9e3183f7b96d49548ae7910641359f273",
    "schemas/task.json": "6e6fa29630e82a6e5c8efedb4288238b03f695e395c0c69e82a8a1eb1f0acd36",
    "schemas/tool-spec.json": "ab130721f780ea1781aa4159332f9db0ccfba55183efddf6e90ff36663718f5b",
}
STATES = ["DRAFT", "SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED", "ACTIVE", "REVOKED"]
ORDER = ["structure", "freshness_timeout", "ood_drift", "version", "permit_policy",
         "device_parameter_allowlist", "interlock", "budget", "confidence"]
COMPONENTS = ["model", "schema", "feature_extractor", "tokenizer", "calibration_parameters", "action_mapping"]
AUTONOMY_CHECKS = ["policy_signature_valid", "policy_unexpired", "permit_signature_valid",
                   "permit_unexpired", "versions_match", "local_persistence_available",
                   "scenario_approved", "site_procedure_valid", "hard_interlocks_passed",
                   "execution_budget_available"]
GATES = ["schema_frozen", "offline_validation_passed", "shadow_passed", "site_approval_valid",
         "permit_valid", "interlock_verified", "rollback_drill_passed", "p3_side_effect_gate_passed"]
SHADOW_CHECKS = ["threshold_frozen_before_start", "threshold_hash_matches", "behavior_unchanged",
                 "continuous_14_days", "production_conditions_covered", "deviation_within_threshold",
                 "latency_within_threshold", "errors_within_threshold", "hardware_matches",
                 "no_real_side_effects", "control_write_physically_or_network_disabled"]
UTC = "2026-10-05T00:00:00Z"
LATER = "2026-10-06T00:00:00Z"
HASH = "0" * 64


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, f"重复 JSON 键: {key}"
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(value)


def load(relative):
    path = (CONTRACT / relative).resolve()
    assert path.is_relative_to(CONTRACT.resolve())
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs,
                      parse_constant=reject_constant)


def walk(node):
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


@pytest.fixture(scope="module")
def registry():
    result = Registry()
    for relative in sorted(set(BASELINE_SHA256) | P4_FILES):
        if relative.startswith("schemas/"):
            result = result.with_resource(Path(relative).name, Resource.from_contents(
                load(relative), default_specification=DRAFT202012))
    return result  # 没有 retrieve 回调，不能回源联网。


def validate(registry, name, definition, value, accepted=True):
    validator = Draft202012Validator({"$ref": name + "#/$defs/" + definition},
                                    registry=registry, format_checker=FormatChecker())
    errors = list(validator.iter_errors(value))
    assert bool(errors) is (not accepted), "; ".join(str(e) for e in errors[:3])


def context():
    return dict(tenant_id="tenant-demo", factory_id="factory-demo", actor_id="user-demo",
                employee_id="employee-planning", task_id="task-demo", trace_id="trace-demo",
                data_class="C2", schema_version="3.0.0", deadline_at=LATER)


def confidence():
    return dict(value=1.0, source="independent_data_calibrated_task_metric",
                calibration_package_ref="trace-demo", calibration_package_hash=HASH,
                aggregation_spec_ref="trace-demo", independent_dataset_ref="trace-demo",
                llm_self_report_used=False)


def guard():
    return dict(context=context(), edge_id="edge-demo", release_id="task-demo",
                decision_ref="trace-demo", mode="shadow", release_status="SHADOW", evaluated_at=UTC,
                checks=[dict(stage=s, passed=True, evidence_ref="trace-demo", evidence_hash=HASH) for s in ORDER],
                first_rejection=None, candidate_eligible=True, confidence=confidence(),
                human_approval_ref="user-demo", human_approval_overrides_hard_checks=False,
                confidence_overrides_hard_checks=False, control_write_disabled=True,
                write_disable_mechanism="network", write_disable_evidence_ref="trace-demo",
                execution_allowed=False, comparison_only=True, comparison_record_ref="trace-demo",
                fallback_definition_ref="trace-demo", fallback_required=False,
                manual_queue_required=False, ai_new_actions_enabled=False)


def artifacts():
    return {k: dict(version="3.0.0", artifact_ref="trace-demo", sha256=HASH) for k in COMPONENTS}


def transition(source="DRAFT", target="SCHEMA_FROZEN"):
    return dict(context=context(), edge_id="edge-demo", release_id="task-demo", expected_version=0,
                **{"from": source, "to": target}, gate_evidence={k: True for k in GATES},
                evidence_refs=["trace-demo"], control_write_disabled=True, ai_new_actions_enabled=False)


def spool(kind="executed_fact"):
    return dict(context=context(), edge_id="edge-demo", boot_id="edge-demo", sequence=1,
                event_id="trace-demo", event_hash=HASH, occurred_at=UTC, aggregate_ref="task-demo",
                aggregate_version=1, kind=kind, payload_ref="trace-demo", payload_hash=HASH,
                action_executed=kind == "executed_fact", command_id="task-demo",
                command_idempotency_key="task-demo", receipt_ref="trace-demo" if kind == "executed_fact" else None,
                receipt_hash=HASH if kind == "executed_fact" else None,
                issued_at=UTC if kind == "pending_action" else None,
                expires_at=LATER if kind == "pending_action" else None,
                ttl_seconds=86400 if kind == "pending_action" else None, center_execution_allowed=False)


def autonomy():
    return dict(context=context(), edge_id="edge-demo", release_id="task-demo", release_status="ACTIVE",
                mode="restricted", center_connected=False, checked_at=UTC, policy_ref="trace-demo",
                policy_hash=HASH, policy_valid_until=LATER, permit_ref="trace-demo", permit_hash=HASH,
                permit_valid_until=LATER, site_procedure_ref="trace-demo",
                checks={k: True for k in AUTONOMY_CHECKS}, autonomy_eligible=True,
                ai_new_actions_enabled=True, return_to_device_baseline=False, manual_queue_required=False,
                fallback_definition_ref="trace-demo", control_safety_requires_center_response=False,
                evidence_refs=["trace-demo"])


@pytest.mark.parametrize("relative,expected", BASELINE_SHA256.items())
def test_prior_contract_bytes_unchanged(relative, expected):
    assert hashlib.sha256((CONTRACT / relative).read_bytes()).hexdigest() == expected


def test_both_directories_are_registered_and_stage_is_complete():
    assert not unregistered(), f"存在未经阶段登记的契约文件: {sorted(unregistered())}"
    assert not missing(), f"清单登记了但磁盘缺失的契约文件: {sorted(missing())}"
    assert files_of("p4") == P4_FILES, "P4 OpenAPI/schema 登记与静态覆盖集合不一致"
    assert set().union(*(files_of(s) for s in ("p0", "p1", "p15", "p3"))) == set(BASELINE_SHA256)


def test_local_refs_have_no_siblings_and_all_fragments_exist():
    for relative in sorted(P4_FILES):
        document = load(relative)
        for node in walk(document):
            if "$ref" not in node:
                continue
            assert set(node) == {"$ref"}, (relative, node)
            url = urlsplit(node["$ref"])
            assert not url.scheme and not url.netloc and not url.query
            target = ((CONTRACT / relative).parent / unquote(url.path)).resolve() if url.path else (CONTRACT / relative).resolve()
            assert target.is_relative_to(CONTRACT.resolve()) and target.is_file()
            value = load(target.relative_to(CONTRACT).as_posix())
            if url.fragment:
                assert url.fragment.startswith("/")
                for part in unquote(url.fragment)[1:].split("/"):
                    key = part.replace("~1", "/").replace("~0", "~")
                    value = value[int(key)] if isinstance(value, list) else value[key]


def test_meta_schema_closed_objects_context_and_utc():
    for relative in sorted(P4_FILES):
        doc = load(relative)
        if relative.startswith("schemas/"):
            assert doc["$schema"] == DIALECT
            Draft202012Validator.check_schema(doc)
        for node in walk(doc):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert node["required"] and set(node["required"]) == set(node["properties"])
            if "context" in node.get("properties", {}):
                assert node["properties"]["context"] == {"$ref": "common-context.json"}
                assert "tenant_id" not in node["properties"]
            if node.get("format") == "date-time":
                assert node.get("pattern", "").endswith("Z$")


def test_openapi_new_only_existing_auth_error_and_layers():
    api = load("openapi/p4.openapi.json")
    assert api["openapi"] == "3.1.0" and len(api["paths"]) == 17
    earlier = set().union(*(set(load(f"openapi/{s}.openapi.json")["paths"]) for s in ("p0", "p1", "p15", "p3")))
    assert not set(api["paths"]) & earlier
    assert api["components"]["schemas"]["Error"] == {"$ref": "p0.openapi.json#/components/schemas/Error"}
    assert api["x-contract-extension"]["new_interface_layer"] is False
    assert {"A09", "A10", "A12"} <= set(api["x-upstream-alignment"])
    for path, methods in api["paths"].items():
        assert path.startswith(("/edge/v1/", "/internal/v1/", "/api/v1/"))
        for method, op in methods.items():
            auth = "EdgeIdentity" if path.startswith("/edge/") else "ExternalIdentity" if path.startswith("/api/") else "InternalIdentity"
            assert op["security"] == [{auth: []}]
            assert api["components"]["securitySchemes"][auth] == {"$ref": f"p0.openapi.json#/components/securitySchemes/{auth}"}
            assert op["x-interface-layer"] in {"L0", "L1", "L2", "L3", "L4", "L5"}
            assert op["x-owner"] in {"edge", "control", "evidence"}
            assert op["x-runtime-unit"] in {"edge", "control-plane", "trajectory-eval"}
            assert "p4_physical_or_network_control_write_disabled" in op["x-required-enforcement"]
            assert set(op["responses"]) == {"200", "400", "401", "403", "409", "422", "429", "503"}
            for code in set(op["responses"]) - {"200"}:
                assert api["components"]["responses"]["Error" + code] == {"$ref": f"p0.openapi.json#/components/responses/Error{code}"}
            assert op["x-operation-kind"] in {"fact", "query"}
            if method == "get":
                assert {"$ref": "#/components/parameters/Context"} in op["parameters"]


def test_guard_order_and_shadow_positive_example(registry):
    doc = load("schemas/p4-guard.json")
    assert doc["x-guard-policy"]["order"] == ORDER
    assert doc["x-guard-policy"]["confidence_overrides_interlock"] is False
    assert doc["x-guard-policy"]["human_approval_overrides_interlock"] is False
    assert doc["x-guard-policy"]["structural_failures"] == ["missing_field", "nan", "out_of_range", "unknown_unit"]
    validate(registry, "p4-guard.json", "Result", guard())


@pytest.mark.parametrize("index", range(9), ids=ORDER)
def test_each_failed_guard_stage_blocks_candidate_even_at_confidence_one(registry, index):
    value = guard()
    value["checks"][index]["passed"] = False
    validate(registry, "p4-guard.json", "Result", value, False)
    value.update(candidate_eligible=False, first_rejection=ORDER[index], fallback_required=True, manual_queue_required=True)
    validate(registry, "p4-guard.json", "Result", value)
    value["first_rejection"] = None
    validate(registry, "p4-guard.json", "Result", value, False)


def test_first_failure_wins_and_guard_order_cannot_be_changed(registry):
    value = guard()
    value["checks"][0]["passed"] = value["checks"][6]["passed"] = False
    value.update(candidate_eligible=False, first_rejection="interlock", fallback_required=True, manual_queue_required=True)
    validate(registry, "p4-guard.json", "Result", value, False)
    value["first_rejection"] = "structure"
    validate(registry, "p4-guard.json", "Result", value)
    value["checks"][0], value["checks"][8] = value["checks"][8], value["checks"][0]
    validate(registry, "p4-guard.json", "Result", value, False)


@pytest.mark.parametrize("field,value", [
    ("execution_allowed", True), ("ai_new_actions_enabled", True), ("control_write_disabled", False),
    ("human_approval_overrides_hard_checks", True), ("confidence_overrides_hard_checks", True),
    ("write_disable_mechanism", "software_flag"), ("action", "task-demo"),
    ("executable_action", {"device_ref": "edge-demo"}), ("device_parameters", {}),
])
def test_shadow_and_hard_checks_cannot_acquire_execution_fields(registry, field, value):
    record = guard()
    record[field] = value
    validate(registry, "p4-guard.json", "Result", record, False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, "1.0"])
def test_confidence_must_be_finite_calibrated_metric(registry, value):
    metric = confidence()
    metric["value"] = value
    # jsonschema 的数值比较不保证拒绝 Python NaN；先验证严格 JSON wire 编码边界。
    if isinstance(value, float) and (value != value or value == float("inf")):
        with pytest.raises(ValueError):
            json.dumps(metric, allow_nan=False)
    else:
        validate(registry, "p4-guard.json", "Confidence", metric, False)


def test_llm_self_report_or_uncalibrated_confidence_rejected(registry):
    for field, value in (("source", "llm_self_report"), ("llm_self_report_used", True), ("calibration_package_ref", None)):
        metric = confidence()
        metric[field] = value
        validate(registry, "p4-guard.json", "Confidence", metric, False)


@pytest.mark.parametrize("source,target", list(zip(STATES, STATES[1:])))
def test_exact_seven_state_adjacent_flow(registry, source, target):
    assert load("schemas/state-machines.json")["$defs"]["EdgeRelease"]["enum"] == STATES
    assert load("schemas/p4-release.json")["x-release-policy"]["core_flow"] == " → ".join(STATES)
    validate(registry, "p4-release.json", "Transition", transition(source, target))


@pytest.mark.parametrize("source,target", [("DRAFT", "SHADOW"), ("SHADOW", "ACTIVE"), ("REVOKED", "ACTIVE"), ("ACTIVE", "DRAFT")])
def test_skip_reverse_or_revoked_transition_rejected(registry, source, target):
    validate(registry, "p4-release.json", "Transition", transition(source, target), False)


@pytest.mark.parametrize("component", COMPONENTS)
def test_behavior_change_invalidates_approval_and_reopens_gates(registry, component):
    value = dict(context=context(), edge_id="edge-demo", previous_release_id="task-demo",
                 replacement_release_id="trace-demo", expected_version=0, changed_components=[component],
                 old_artifacts=artifacts(), new_artifacts=artifacts(), behavior_affecting=True,
                 replacement_status="DRAFT", previous_approval_invalidated=True,
                 offline_validation_required=True, shadow_gate_required=True, schema_freeze_required=True,
                 ai_new_actions_enabled=False, control_write_disabled=True, evidence_refs=["trace-demo"])
    # 两个 hash 的真实差异、归因和新 release 身份由 L2 查权威包，Schema 不伪造跨字段比较。
    value["new_artifacts"][component]["sha256"] = "1" * 64
    validate(registry, "p4-release.json", "Reopen", value)
    for field in ("previous_approval_invalidated", "offline_validation_required", "shadow_gate_required"):
        bad = copy.deepcopy(value)
        bad[field] = False
        validate(registry, "p4-release.json", "Reopen", bad, False)


def test_revoked_has_no_candidate_or_new_actions(registry):
    value = guard()
    value["release_status"] = "REVOKED"
    validate(registry, "p4-guard.json", "Result", value, False)
    value.update(candidate_eligible=False, fallback_required=True, manual_queue_required=True)
    validate(registry, "p4-guard.json", "Result", value)
    value["execution_allowed"] = True
    validate(registry, "p4-guard.json", "Result", value, False)


@pytest.mark.parametrize("kind", ["executed_fact", "pending_action"])
def test_spool_positive_facts_and_pending_ttl(registry, kind):
    validate(registry, "p4-spool.json", "Entry", spool(kind))


@pytest.mark.parametrize("field", ["ttl_seconds", "issued_at", "expires_at"])
def test_pending_action_requires_nonnull_ttl_fields(registry, field):
    value = spool("pending_action")
    value.pop(field)
    validate(registry, "p4-spool.json", "Entry", value, False)
    value[field] = None
    validate(registry, "p4-spool.json", "Entry", value, False)


def test_spool_facts_only_no_central_execution_or_replay(registry):
    policy = load("schemas/p4-spool.json")["x-spool-policy"]
    assert policy["report_executed_actions"] == "facts_only"
    for key in ("center_replay_allowed", "expired_pending_action_execute", "late_event_overwrites_newer_state", "silent_drop"):
        assert policy[key] is False
    assert policy["reconnect_dedupe_by"] == "event_id"
    assert policy["device_command_and_receipt_idempotency"] is True
    for field, item in (("center_execution_allowed", True), ("replay_executed_action", True), ("device_command", {})):
        value = spool()
        value[field] = item
        validate(registry, "p4-spool.json", "Entry", value, False)
    ack = dict(event_id="trace-demo", event_hash=HASH, facts_persisted=True, duplicate=False,
               late=False, expired=True, state_updated=False, action_executed=False,
               center_replay_allowed=False, fact_record_ref="trace-demo", state_confirmation_ref="trace-demo")
    validate(registry, "p4-spool.json", "Acknowledgement", ack)
    for flag in ("duplicate", "late"):
        bad = dict(ack, state_updated=True, **{flag: True})
        validate(registry, "p4-spool.json", "Acknowledgement", bad, False)
    ack["action_executed"] = True
    validate(registry, "p4-spool.json", "Acknowledgement", ack, False)


@pytest.mark.parametrize("check", AUTONOMY_CHECKS)
def test_disconnected_any_failed_requirement_returns_baseline(registry, check):
    value = autonomy()
    validate(registry, "p4-autonomy.json", "Decision", value)
    value["checks"][check] = False
    validate(registry, "p4-autonomy.json", "Decision", value, False)
    value.update(autonomy_eligible=False, ai_new_actions_enabled=False,
                 return_to_device_baseline=True, manual_queue_required=True)
    validate(registry, "p4-autonomy.json", "Decision", value)


def test_shadow_autonomy_cannot_enable_ai_even_if_all_requirements_pass(registry):
    value = autonomy()
    validate(registry, "p4-autonomy.json", "ShadowDecision", value, False)
    value.update(mode="shadow", autonomy_eligible=False, ai_new_actions_enabled=False,
                 return_to_device_baseline=True, manual_queue_required=True)
    validate(registry, "p4-autonomy.json", "ShadowDecision", value)


@pytest.mark.parametrize("check", SHADOW_CHECKS)
def test_shadow_gate_pass_requires_each_preexisting_check(registry, check):
    value = dict(context=context(), report_ref="trace-demo", threshold_ref="task-demo", checked_at=LATER,
                 checks={k: True for k in SHADOW_CHECKS}, passed=True, automatic_approval=False,
                 evidence_refs=["trace-demo"])
    validate(registry, "p4-shadow.json", "Evaluation", value)
    value["checks"][check] = False
    validate(registry, "p4-shadow.json", "Evaluation", value, False)
    value["passed"] = False
    validate(registry, "p4-shadow.json", "Evaluation", value)


def test_thresholds_measurements_separate_and_14_days_not_approval():
    doc = load("schemas/p4-shadow.json")
    threshold = doc["$defs"]["Threshold"]
    report = doc["$defs"]["Report"]
    assert threshold["properties"]["frozen"] == {"const": True}
    assert threshold["properties"]["minimum_continuous_seconds"]["minimum"] == 14 * 24 * 60 * 60
    assert {"threshold_ref", "threshold_hash", "continuous_observation_seconds", "condition_measurements", "latency_ms"} <= set(report["required"])
    assert not set(report["properties"]) & {"thresholds", "minimum_coverage_ratio", "maximum_latency_ms", "maximum_false_positive_rate"}
    assert report["properties"]["real_side_effect_count"] == {"const": 0}
    assert report["properties"]["control_write_disabled"] == {"const": True}
    assert doc["x-shadow-policy"]["threshold_mutable_after_start"] is False
    assert doc["x-shadow-policy"]["fourteen_days_automatically_approve"] is False


def test_unfrozen_threshold_and_report_defined_threshold_are_invalid(registry):
    # 仅测试夹具数值，绝不作为生产默认门槛；生产数值由受控冻结记录提供。
    threshold = dict(context=context(), threshold_id="task-demo", scenario_ref="task-demo",
                     behavior_hash=HASH, frozen=True, frozen_at=UTC, frozen_by_ref="user-demo",
                     threshold_hash=HASH, coverage_spec_ref="trace-demo", coverage_spec_hash=HASH,
                     metrics_spec_ref="trace-demo", metrics_spec_hash=HASH, minimum_continuous_seconds=1209600,
                     required_conditions=[dict(condition_ref="task-demo", minimum_samples=1)],
                     minimum_coverage_ratio=1.0, maximum_deviation=0.0, deviation_unit_ref="trace-demo",
                     maximum_latency_ms=dict(p50=1.0, p95=1.0, p99=1.0), maximum_timeout_rate=0.0,
                     maximum_false_positive_rate=0.0, maximum_false_negative_rate=0.0,
                     maximum_rejection_rate=0.0, calibration_package_ref="trace-demo",
                     calibration_package_hash=HASH, hardware_manifest_ref="trace-demo",
                     hardware_manifest_hash=HASH, approval_evidence_ref="trace-demo")
    validate(registry, "p4-shadow.json", "Threshold", threshold)
    bad = dict(threshold, frozen=False)
    validate(registry, "p4-shadow.json", "Threshold", bad, False)
    bad = dict(threshold, minimum_continuous_seconds=1209599)
    validate(registry, "p4-shadow.json", "Threshold", bad, False)
    percentile = dict(p50=1.0, p95=1.0, p99=1.0)
    latency = dict(percentile, feature_extraction=percentile, queue=percentile,
                   inference=percentile, validation=percentile,
                   protocol_round_trip=dict(p50=None, p95=None, p99=None, read_only=True, applicable=False))
    report = dict(context=context(), report_id="trace-demo", release_id="task-demo", edge_id="edge-demo",
                  scenario_ref="task-demo", threshold_ref="task-demo", threshold_hash=HASH,
                  behavior_hash=HASH, started_at=UTC, ended_at=LATER, continuous_observation_seconds=86400,
                  production_comparison=True,
                  condition_measurements=[dict(condition_ref="task-demo", observed_samples=1,
                                               observation_ref="trace-demo", observation_hash=HASH)],
                  measured_coverage_ratio=1.0, measured_deviation=0.0, deviation_unit_ref="trace-demo",
                  latency_ms=latency, sample_count=1, false_positive_count=0, false_negative_count=0,
                  false_positive_denominator=1, false_negative_denominator=1,
                  false_positive_rate=0.0, false_negative_rate=0.0, rejection_rate=0.0, timeout_rate=0.0,
                  coverage_gaps_refs=[], measurement_spec_ref="trace-demo", measurement_spec_hash=HASH,
                  hardware_manifest_ref="trace-demo", hardware_manifest_hash=HASH,
                  control_write_disabled=True, write_disable_mechanism="physical",
                  write_disable_evidence_ref="trace-demo", real_side_effect_count=0,
                  comparison_records_ref="trace-demo", comparison_records_hash=HASH, created_at=LATER)
    # 不足14天的真实实测可保存，晋级失败另由 Evaluation 判定；避免静默丢弃失败证据。
    validate(registry, "p4-shadow.json", "Report", report)
    for field, value in (("thresholds", threshold), ("real_side_effect_count", 1), ("control_write_disabled", False)):
        validate(registry, "p4-shadow.json", "Report", dict(report, **{field: value}), False)


def test_fixed_per_device_fallback_and_frozen_aggregation(registry):
    value = dict(context=context(), device_ref="edge-demo", definition_ref="trace-demo", definition_hash=HASH,
                 version="3.0.0", action="保持基线", defined_by_ref="user-demo",
                 device_engineer_evidence_ref="trace-demo", trigger_spec_ref="trace-demo",
                 parameter_boundary_spec_ref="trace-demo", baseline_spec_ref="trace-demo",
                 interlock_spec_ref="trace-demo", site_procedure_ref="trace-demo", approved_at=UTC,
                 model_selected=False, frozen=True)
    validate(registry, "p4-fallback.json", "Definition", value)
    for field, item in (("action", "模型即兴回退"), ("model_selected", True), ("device_ref", None)):
        bad = dict(value, **{field: item})
        validate(registry, "p4-fallback.json", "Definition", bad, False)
    aggregate = dict(algorithm_ref="trace-demo", algorithm_version="3.0.0", algorithm_hash=HASH,
                     field_order=["task-demo"], missing_field_rule_ref="trace-demo",
                     field_dependency_spec_ref="trace-demo", independent_calibration_dataset_ref="trace-demo",
                     independent_calibration_dataset_hash=HASH, task_metric_spec_ref="trace-demo",
                     frozen=True, frozen_at=UTC, approved_by_ref="user-demo")
    validate(registry, "p4-scenario.json", "Aggregation", aggregate)
    aggregate["frozen"] = False
    validate(registry, "p4-scenario.json", "Aggregation", aggregate, False)


def test_required_nested_context_no_inline_sensitive_payload_and_utc(registry):
    value = spool()
    value.pop("context")
    validate(registry, "p4-spool.json", "Entry", value, False)
    for field, item in (("payload", "受控引用外的正文"), ("occurred_at", "2026-10-05T08:00:00+08:00")):
        value = spool()
        value[field] = item
        validate(registry, "p4-spool.json", "Entry", value, False)
