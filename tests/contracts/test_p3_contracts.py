"""P3 静态契约门禁：只读 packages/contracts；无需业务实现、服务、DB 或网络。

规则声明与 JSON Schema 正反例是契约证据，不构成并发、持久化、权限或故障收敛的运行证明。
依赖沿用 contracts requirements-dev；本交付按要求未运行测试。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .contract_inventory import files_of, missing, unregistered

CONTRACT = Path(__file__).resolve().parents[2] / "packages" / "contracts"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASELINE_SHA256 = {
    "openapi/p0.openapi.json": "54adb747a58653c24bfafefcc41e953e99f53191d9f86a001d5840b31f4599f7",
    "openapi/p1.openapi.json": "d5ccbbf61a092de2fe58d10957858f627f02783cf881d76a4bd44c1b11f9f9ae",
    "openapi/p15.openapi.json": "a06d1653ac4c57f2e3789f3d99211b4d1ef62631692b82357aefe92ef2b5e031",
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
    "schemas/service-manifest.json": "f406573fdbc6706f661f8ee1c385e0226387a737812687b85217fe27d2522e84",
    "schemas/state-machines.json": "1cc989f9f6119e437678ee14a7d293f9e3183f7b96d49548ae7910641359f273",
    "schemas/task.json": "6e6fa29630e82a6e5c8efedb4288238b03f695e395c0c69e82a8a1eb1f0acd36",
    "schemas/tool-spec.json": "ab130721f780ea1781aa4159332f9db0ccfba55183efddf6e90ff36663718f5b",
}
P3_SHA256 = {
    "schemas/p3-approval.json": "66319a3b164e7bcb5c6c85406b80fd8ac8da144e21878e1345ae96b212491a2f",
    "schemas/p3-cancellation.json": "0e283c63883a40de87841c5bd489d79fd494469bf6cec2557aa2caece6338133",
    "schemas/p3-command.json": "33b9396c64fc572fc42ce30a133cd254fa1786a1499911fe5db70e6e452bd15f",
    "schemas/p3-employee.json": "a12faa7b4dacf9935bf4300ca52fae812259ac114027221f7fdfe7fb7430decc",
    "schemas/p3-execution.json": "182d4bc67dc72d2f3942f887108f22e90a43bce75029756bd8a90670eedf61a0",
    "schemas/p3-saga.json": "0bd360a6d3d80ba6e622ba45e12617d67c83f91f62c2a97cbc2499b8e642782d",
    "schemas/p3-tool-admission.json": "45698635161d105b740bcb2e8dceddd49e137ae3347e94b82452fbf6eb302af2",
    "schemas/p3-unknown.json": "ae491a963815d2e37074794b6aaede6d51e1f16aab931e9859282dae70c5057e",
    "openapi/p3.openapi.json": "6a09955a82d39e811b8db4cedf1d8fcbd76d1e9ce8435b9e374e6addeee8a5ae",
}
EXPECTED_INTERFACES = {
    "post /internal/v1/tool-requests/normalize": ["tool","L3","fact","tool-gateway"],
    "get /internal/v1/tools/{tool_id}/write-admission": ["tool","L1","query","tool-gateway"],
    "post /internal/v1/approvals/bindings": ["control","L1","fact","control-plane"],
    "get /api/v1/approvals/{approval_id}/fact": ["control","L0","query","control-plane"],
    "post /internal/v1/approvals/pre-execution-review": ["control","L1","fact","control-plane"],
    "post /internal/v1/commands/controlled": ["transaction","L3","command","transaction-service"],
    "get /internal/v1/commands/{command_id}/execution-record": ["transaction","L3","query","transaction-service"],
    "post /internal/v1/commands/{command_id}/leases": ["transaction","L3","fact","transaction-service"],
    "post /internal/v1/commands/{command_id}/execute": ["transaction","L3","command","transaction-service"],
    "post /internal/v1/tool-executions/controlled": ["tool","L3","command","tool-gateway"],
    "post /internal/v1/commands/{command_id}/reconcile": ["transaction","L3","fact","transaction-service"],
    "get /internal/v1/commands/{command_id}/reconciliation": ["transaction","L3","query","transaction-service"],
    "post /internal/v1/sagas": ["transaction","L3","command","transaction-service"],
    "get /internal/v1/sagas/{saga_id}": ["transaction","L3","query","transaction-service"],
    "post /internal/v1/sagas/{saga_id}/compensations": ["transaction","L3","command","transaction-service"],
    "post /api/v1/tasks/{task_id}/cancellation-requests": ["control","L0","command","control-plane"],
    "get /api/v1/tasks/{task_id}/cancellation-requests/{request_id}": ["control","L0","query","control-plane"],
}

ERROR_CODES = {"400", "401", "403", "409", "422", "429", "503"}
UTC = "2026-10-05T00:00:00Z"
LATER = "2026-10-06T00:00:00Z"
HASH = "0" * 64
CHECKS = (
    "approval_valid", "payload_hash_matches", "policy_version_matches",
    "action_scope_matches", "object_version_matches", "current_permission_valid",
    "delegation_valid", "duties_separated", "capabilities_admitted", "lease_current",
    "fencing_current", "object_lock_held", "not_cancelled_before_start",
)


def unique_pairs(items):
    result = {}
    for key, value in items:
        assert key not in result, f"重复 JSON 键: {key}"
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(value)


def load(relative):
    file = (CONTRACT / relative).resolve()
    assert file.is_relative_to(CONTRACT.resolve())
    return json.loads(file.read_text(encoding="utf-8-sig"),
                      object_pairs_hook=unique_pairs, parse_constant=reject_constant)


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
    dependencies = files_of("p0") | files_of("p1") | files_of("p15") | files_of("p3")
    for relative in sorted(p for p in dependencies if p.startswith("schemas/")):
        file = CONTRACT / relative
        document = load(relative)
        result = result.with_resource(file.name, Resource.from_contents(
            document, default_specification=DRAFT202012))
    return result


def errors(registry, name, definition, value):
    validator = Draft202012Validator(
        {"$ref": name + "#/$defs/" + definition},
        registry=registry, format_checker=FormatChecker())
    return list(validator.iter_errors(value))


def valid(registry, name, definition, value):
    found = errors(registry, name, definition, value)
    assert not found, "; ".join(str(error) for error in found[:4])


def invalid(registry, name, definition, value):
    assert errors(registry, name, definition, value), "非法实例被契约接受"


def context():
    return dict(tenant_id="tenant-demo", factory_id="factory-demo", actor_id="user-demo",
                employee_id="employee-planning", task_id="task-demo", trace_id="trace-demo",
                data_class="C2", schema_version="3.0.0", deadline_at=LATER)


def binding():
    return dict(context=context(), object_id="task-demo", expected_version=0,
                tool_id="employee-planning", tool_version="3.0.0", command_type="employee-planning",
                action_scope=["employee-planning"], payload_ref="task-demo", payload_hash=HASH,
                payload_schema=dict(schema_ref="task.json", schema_version="3.0.0", schema_hash=HASH),
                normalization_ref="trace-demo", policy_version="3.0.0")


def approval():
    return dict(context=context(), approval_id="task-demo", binding=binding(), approved=True,
                approver_ref="user-demo", decided_at=UTC, valid_from=UTC, valid_until=LATER,
                record_version=0, decision_ref="trace-demo", policy_evaluation_ref="trace-demo",
                audit_intent_ref="trace-demo", status_source="control-plane")


def review():
    result = dict(context=context(), review_ref="trace-demo", command_id="task-demo",
                  binding=binding(), approval_ref="task-demo", approval_record_version=0,
                  approval_required=True, policy_evaluation_ref="trace-demo", checked_at=UTC,
                  valid_until=LATER, command_record_version=0, actual_object_version=0,
                  permission_evidence_ref="trace-demo", lease_ref="trace-demo", fencing_token=1,
                  allowed=True, reason_ref=None, status_source="control-plane")
    result.update({check: True for check in CHECKS})
    return result


def lease():
    return dict(context=context(), command_id="task-demo", object_id="task-demo",
                worker_ref="user-demo", lease_ref="trace-demo", fencing_token=1,
                issued_at=UTC, expires_at=LATER, object_lock_ref="trace-demo", record_version=0)


def credential():
    return dict(context=context(), command_id="task-demo", idempotency_key="task-demo",
                binding=binding(), execution_ref="trace-demo", pre_state_ref="trace-demo",
                pre_state_hash=HASH, review_ref="trace-demo", lease=lease(),
                execution_permit_ref="trace-demo", audit_intent_ref="trace-demo",
                started_at=UTC, record_version=0, status_source="transaction-service")


def receipt(outcome="SUCCEEDED", posted=False):
    confirmed = outcome in {"SUCCEEDED", "FAILED", "NOT_EXECUTED"}
    return dict(context=context(), command_id="task-demo", idempotency_key="task-demo",
                command_payload_hash=HASH, receipt_id="trace-demo", source_system="employee-planning",
                adapter_unit="employee-planning", outcome=outcome,
                authority_receipt_ref="trace-demo" if confirmed else None,
                authority_receipt_hash=HASH if confirmed else None, authority_verified=confirmed,
                confirmed_at=UTC if confirmed else None, recorded_at=UTC,
                command_status="UNKNOWN" if outcome == "NOT_EXECUTED" else
                ("ACCEPTED" if outcome == "accepted" else outcome),
                business_success=outcome == "SUCCEEDED",
                posting_status="POSTED" if posted else "NOT_CONFIRMED",
                posting_evidence_ref="trace-demo" if posted else None, status_source="transaction-service")


def command(status="ACCEPTED"):
    success = status == "SUCCEEDED"
    return dict(context=context(), command_id="task-demo", idempotency_key="task-demo",
                binding=binding(), policy_evaluation_ref="trace-demo", approval_ref="task-demo",
                status=status, record_version=0, status_source="transaction-service", updated_at=UTC,
                execution_credential=credential() if status in {"EXECUTING", "UNKNOWN", "SUCCEEDED"} else None,
                authority_receipt=receipt() if success else None, business_success=success,
                posting_status="NOT_CONFIRMED", posting_evidence_ref=None,
                reconciliation_ref="trace-demo" if status == "UNKNOWN" else None,
                manual_queue_ref="trace-demo" if status == "MANUAL_REVIEW" else None)


def convergence(outcome="UNKNOWN", queried=False):
    proved = outcome in {"NOT_EXECUTED", "SUCCEEDED", "FAILED"}
    actions = {
        "NOT_EXECUTED": ["RETRY_SAME_KEY", "MANUAL_REVIEW"],
        "SUCCEEDED": [], "FAILED": [],
        "UNKNOWN": ["MANUAL_REVIEW"] if queried else ["QUERY_SOURCE"],
    }[outcome]
    return dict(context=context(), command_id="task-demo", reconciliation_ref="trace-demo",
                idempotency_key="task-demo", command_payload_hash=HASH, source_system="employee-planning",
                query_attempt_count=1 if queried or proved else 0,
                query_evidence_ref="trace-demo" if proved else None,
                query_evidence_hash=HASH if proved else None, query_verified=proved, source_outcome=outcome,
                command_status="UNKNOWN" if outcome == "NOT_EXECUTED" else
                ("MANUAL_REVIEW" if queried and outcome == "UNKNOWN" else outcome),
                candidate_actions=actions, prohibited_actions=["RESEND_NEW_KEY", "DIRECT_COMPENSATION"],
                next_action=actions[0] if actions else None,
                retry_idempotency_key="task-demo" if outcome == "NOT_EXECUTED" else None,
                retry_requires_fresh_review=True, recorded_receipt=receipt(outcome) if proved else None,
                manual_queue_ref="trace-demo" if queried and outcome == "UNKNOWN" else None,
                last_error=None, recorded_at=UTC, status_source="transaction-service",
                business_success=outcome == "SUCCEEDED", posting_status="NOT_CONFIRMED")


def permission():
    return dict(policy_evaluation_ref="trace-demo", approval_ref="task-demo",
                action_scope=["employee-planning"], valid_until=LATER, permission_evidence_ref="trace-demo")


def compensation(status="SUCCEEDED"):
    result = dict(context=context(), original_command_id="task-demo", command_id="trace-demo",
                  idempotency_key="trace-demo", action_ref="employee-planning", permission=permission(),
                  review_ref="trace-demo", command_status=status,
                  authority_receipt=receipt(status) if status in {"SUCCEEDED", "FAILED"} else None,
                  manual_queue_ref="trace-demo" if status in {"FAILED", "MANUAL_REVIEW"} else None)
    if result["authority_receipt"]:
        result["authority_receipt"].update(command_id="trace-demo", idempotency_key="trace-demo")
    return result


def step(domain="PHYSICAL", started=False):
    br = dict(action_ref=None, permission=None, manual_required=True, evidence_ref="trace-demo")
    return dict(context=context(), step_id="task-demo", domain=domain, object_id="task-demo",
                expected_version=0, pre_state_ref="trace-demo", pre_state_hash=HASH,
                command_id="task-demo", command_key="task-demo",
                command_status="SUCCEEDED" if started else "ACCEPTED", started=started,
                execution_credential=credential() if started else None,
                authority_receipt=receipt() if started else None, compensation_supported=False,
                compensable_action_ref=None, compensation_permission=None, is_shipped=False, is_posted=False,
                stage_evidence_ref="trace-demo", branches={key: copy.deepcopy(br) for key in
                ("shipped", "not_shipped", "posted", "not_posted")},
                selected_branch="not_shipped" if domain == "PHYSICAL" else "not_posted",
                compensation=None, compensation_required=False, manual_queue_ref=None)


def saga():
    return dict(context=context(), saga_id="task-demo", status="RUNNING", steps=[step()],
                recovery_order=[dict(domain="PHYSICAL", step_refs=["task-demo"]),
                                dict(domain="ACCOUNTING", step_refs=[])],
                physical_verified_and_restored=False, physical_evidence_refs=[],
                accounting_recovery_started=False, all_compensation_receipts_verified=False,
                manual_queue_ref=None, record_version=0, updated_at=UTC, status_source="transaction-service")


def cancellation():
    return dict(context=context(), task_id="task-demo", request_id="trace-demo", task_status="RUNNING",
                steps=[dict(step_id="task-demo", command_id="task-demo", started=False,
                            stopped_before_start=True, command_status="ACCEPTED", confirmation_required=False,
                            authority_receipt_ref=None, compensation_command_ref=None, manual_queue_ref=None)],
                prevents_new_steps=True, side_effects_reconciled=False, applicable_compensations_verified=False,
                task_cancelled_is_reversal=False, business_success=False, posting_status="NOT_CONFIRMED",
                manual_queue_ref=None, record_version=0, updated_at=UTC,
                status_source="control-plane", execution_status_source="transaction-service")


@pytest.mark.parametrize("relative,expected", list(BASELINE_SHA256.items()) + list(P3_SHA256.items()))
def test_contract_sha256_locked(relative, expected):
    assert hashlib.sha256((CONTRACT / relative).read_bytes()).hexdigest() == expected


def test_new_stage_complete_and_no_baseline_overwrite():
    assert len(BASELINE_SHA256) == 26
    assert len(P3_SHA256) == 9
    assert not BASELINE_SHA256.keys() & P3_SHA256.keys()
    assert files_of("p0") | files_of("p1") | files_of("p15") == set(BASELINE_SHA256), "历史阶段文件清单与哈希锁定集合不一致"
    assert files_of("p3") == set(P3_SHA256), "P3 文件清单与哈希锁定集合不一致"
    assert unregistered() == set(), f"存在未经阶段登记的契约文件: {sorted(unregistered())}"
    assert missing() == set(), f"清单登记了但磁盘缺失的契约文件: {sorted(missing())}"


def resolve(source, reference):
    parsed = urlsplit(reference)
    assert not parsed.scheme and not parsed.netloc and not parsed.query
    target = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source.resolve()
    assert target.is_relative_to(CONTRACT.resolve()) and target.is_file()
    document = load(target.relative_to(CONTRACT.resolve()).as_posix())
    fragment = unquote(parsed.fragment)
    assert not fragment or fragment.startswith("/")
    for token in fragment.split("/")[1:] if fragment else ():
        assert not re.search(r"~(?![01])", token)
        document = document[token.replace("~1", "/").replace("~0", "~")]
    return document


def test_all_new_refs_local_resolvable_and_without_siblings():
    for relative in P3_SHA256:
        source = CONTRACT / relative
        for node in walk(load(relative)):
            if "$ref" in node:
                assert set(node) == {"$ref"}
                assert isinstance(resolve(source, node["$ref"]), (dict, bool))


def test_meta_schema_closed_objects_nested_context_and_utc():
    for relative in P3_SHA256:
        document = load(relative)
        if relative.startswith("schemas/"):
            assert document["$schema"] == DIALECT
            Draft202012Validator.check_schema(document)
        for node in walk(document):
            if node.get("type") == "object" and "additionalProperties" in node:
                assert node["additionalProperties"] is False
                assert node["required"]
                assert not ({"tenant_id", "actor_id", "trace_id"} & node["properties"].keys())
            if "context" in node.get("properties", {}):
                assert node["properties"]["context"] == {"$ref": "common-context.json"}
                assert "context" in node["required"]
    assert load("schemas/common-context.json")["$defs"]["UtcTimestamp"]["pattern"].endswith("Z$")


def test_openapi_only_new_interfaces_auth_layers_owners_and_error():
    api = load("openapi/p3.openapi.json")
    assert api["openapi"] == "3.1.0" and api["jsonSchemaDialect"] == DIALECT
    assert api["info"]["version"] == "3.0.0"
    old_paths = set().union(*(set(load(f"openapi/{phase}.openapi.json")["paths"])
                              for phase in ("p0", "p1", "p15")))
    assert not old_paths & api["paths"].keys()
    actual = {f"{method} {url}": operation for url, item in api["paths"].items()
              for method, operation in item.items()}
    assert actual.keys() == EXPECTED_INTERFACES.keys()
    assert len({op["operationId"] for op in actual.values()}) == len(actual)
    for key, expected in EXPECTED_INTERFACES.items():
        method, url = key.split(" ", 1)
        op = actual[key]
        assert [op["x-owner"], op["x-interface-layer"], op["x-operation-kind"],
                op["x-runtime-unit"]] == expected
        assert op["x-interface-layer"] in {"L0", "L1", "L2", "L3", "L4", "L5"}
        assert url.startswith(("/api/v1/", "/internal/v1/"))
        audience = "ExternalIdentity" if url.startswith("/api/") else "InternalIdentity"
        assert op["security"] == [{audience: []}]
        assert "tenant_matches_context_and_authenticated_identity" in op["x-required-enforcement"]
        for code in ERROR_CODES:
            assert op["responses"][code] == {"$ref": "#/components/responses/Error" + code}
        params = op.get("parameters", [])
        assert {p["name"] for p in params if p.get("in") == "path"} == set(re.findall(r"\{([^}]+)\}", url))
        if method == "get":
            assert {"$ref": "#/components/parameters/Context"} in params
        else:
            assert op["requestBody"]["required"] is True
        success = {code for code in op["responses"] if code.startswith("2")}
        assert success == ({"202"} if op["x-operation-kind"] == "command" else {"200"})
    for name in ("ExternalIdentity", "InternalIdentity"):
        assert api["components"]["securitySchemes"][name] == {
            "$ref": "p0.openapi.json#/components/securitySchemes/" + name}
    assert api["components"]["schemas"]["Error"] == {"$ref": "p0.openapi.json#/components/schemas/Error"}
    assert api["components"]["schemas"]["AuthorityReceipt"] == {"$ref": "../schemas/p15-receipt.json"}
    assert api["x-contract-extension"]["new_interface_layer"] is False
    for key in ("cross_system_atomicity", "timeout_means_failed", "UNKNOWN_is_success",
                "accepted_is_posted", "AUTHORIZED_is_posted", "p0_write_route_bypass"):
        assert api["x-execution-constraints"][key] is False
    assert api["x-execution-constraints"]["mock_before_sandbox"] is True
    assert {"A05", "A06", "A08", "A14"} <= api["x-upstream-alignment"].keys()


def test_lifecycle_exactly_upstream_and_receipt_separate_from_posting():
    states = load("schemas/state-machines.json")["$defs"]
    assert states["Command"]["enum"] == [
        "ACCEPTED", "AUTHORIZED", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN", "MANUAL_REVIEW"]
    assert states["Saga"]["enum"] == [
        "RUNNING", "SUCCEEDED", "COMPENSATING", "COMPENSATED", "MANUAL_REVIEW"]
    command_schema = load("schemas/p3-command.json")
    saga_schema = load("schemas/p3-saga.json")
    assert command_schema["$defs"]["View"]["properties"]["status"] == {
        "$ref": "state-machines.json#/$defs/Command"}
    assert saga_schema["$defs"]["View"]["properties"]["status"] == {
        "$ref": "state-machines.json#/$defs/Saga"}
    assert command_schema["x-command-policy"]["core_flow"] == states["Command"]["x-core-flow"]
    assert saga_schema["x-saga-policy"]["core_flow"] == states["Saga"]["x-core-flow"]
    policy = command_schema["x-command-policy"]
    assert policy["only_verified_authority_receipt_drives_success"] is True
    assert policy["idempotency_scope"] == ["context.tenant_id", "idempotency_key"]
    assert policy["same_key_different_payload"] == "409"
    assert policy["same_key_same_payload"] == "same_command_and_current_result"
    assert command_schema["x-posting-policy"]["business_success_and_posting_status_separate"] is True


@pytest.mark.parametrize("status", ["ACCEPTED", "AUTHORIZED", "EXECUTING", "UNKNOWN", "MANUAL_REVIEW"])
@pytest.mark.parametrize("field,value", [("business_success", True), ("posting_status", "POSTED")])
def test_no_nonterminal_claims_success_or_posted(registry, status, field, value):
    record = command(status)
    valid(registry, "p3-command.json", "View", record)
    record[field] = value
    invalid(registry, "p3-command.json", "View", record)


def test_success_and_posting_require_authoritative_receipt(registry):
    value = command("SUCCEEDED")
    valid(registry, "p3-command.json", "View", value)
    bad = copy.deepcopy(value)
    bad["authority_receipt"] = None
    invalid(registry, "p3-command.json", "View", bad)
    value.update(posting_status="POSTED", posting_evidence_ref="trace-demo", authority_receipt=receipt(posted=True))
    valid(registry, "p3-command.json", "View", value)
    for field in ("authority_receipt", "posting_evidence_ref"):
        bad = copy.deepcopy(value)
        bad[field] = None
        invalid(registry, "p3-command.json", "View", bad)
    bad = copy.deepcopy(value)
    bad["authority_receipt"]["authority_verified"] = False
    invalid(registry, "p3-command.json", "View", bad)


def test_approval_independent_binding_and_execution_review(registry):
    value = approval()
    valid(registry, "p3-approval.json", "Fact", value)
    for field in ("approver_ref", "decided_at", "valid_from", "valid_until", "record_version", "audit_intent_ref"):
        bad = copy.deepcopy(value)
        del bad[field]
        invalid(registry, "p3-approval.json", "Fact", bad)
    for field in ("object_id", "payload_hash", "expected_version", "policy_version", "action_scope", "normalization_ref"):
        bad = copy.deepcopy(value)
        del bad["binding"][field]
        invalid(registry, "p3-approval.json", "Fact", bad)
    policy = load("schemas/p3-approval.json")["x-approval-policy"]
    assert policy["trusted_normalizer"] == "tool-gateway"
    assert policy["trusted_hash_recomputed"] is True
    assert policy["payload_change_requires_new_approval"] is True
    assert policy["concurrent_decision"] == "one_effective_decision_via_cas"
    assert policy["authorized_is_perpetual"] is False
    assert policy["natural_language_is_authorization"] is False
    assert policy["model_confidence_is_authorization"] is False


@pytest.mark.parametrize("check", CHECKS)
def test_a06_review_fails_closed_on_any_failed_check(registry, check):
    value = review()
    valid(registry, "p3-approval.json", "Review", value)
    value[check] = False
    invalid(registry, "p3-approval.json", "Review", value)
    value.update(allowed=False, reason_ref="trace-demo")
    valid(registry, "p3-approval.json", "Review", value)
    value["reason_ref"] = None
    invalid(registry, "p3-approval.json", "Review", value)


def test_lease_fencing_and_checkpoint_contract(registry):
    valid(registry, "p3-execution.json", "Lease", lease())
    valid(registry, "p3-execution.json", "Credential", credential())
    for field in ("fencing_token", "expires_at", "object_lock_ref", "worker_ref"):
        value = lease()
        del value[field]
        invalid(registry, "p3-execution.json", "Lease", value)
    value = lease()
    value["fencing_token"] = 0
    invalid(registry, "p3-execution.json", "Lease", value)
    schema = load("schemas/p3-execution.json")
    policy = schema["x-execution-policy"]
    for field in ("lease_durable", "fencing_monotonic", "intent_before_external_dispatch",
                  "audit_intent_before_side_effect", "new_owner_increments_generation"):
        assert policy[field] is True
    assert policy["fencing_scope"] == policy["lock_scope"] == [
        "context.tenant_id", "context.factory_id", "object_id"]
    assert policy["stale_worker_commit"] == "reject"
    assert policy["object_update"] == "CAS_expected_version"
    assert policy["expired_lease_proves_not_executed"] is False
    assert schema["x-checkpoint-policy"]["query_existing_command_before_resume"] is True
    assert schema["x-checkpoint-policy"]["reexecute_succeeded_step"] is False


@pytest.mark.parametrize("outcome,queried", [("UNKNOWN", False), ("UNKNOWN", True),
                                           ("NOT_EXECUTED", True), ("SUCCEEDED", True), ("FAILED", True)])
def test_unknown_decision_table_representable(registry, outcome, queried):
    valid(registry, "p3-unknown.json", "Record", convergence(outcome, queried))


@pytest.mark.parametrize("action", ["RESEND_NEW_KEY", "DIRECT_COMPENSATION",
                                    "换新幂等键重发", "直接补偿"])
def test_unknown_forbidden_action_cannot_be_candidate_or_next(registry, action):
    for field in ("candidate_actions", "next_action"):
        value = convergence()
        value[field] = [action] if field == "candidate_actions" else action
        invalid(registry, "p3-unknown.json", "Record", value)
    policy = load("schemas/p3-unknown.json")["x-convergence-policy"]
    assert policy["candidate_actions"] == ["QUERY_SOURCE", "RETRY_SAME_KEY", "MANUAL_REVIEW"]
    assert policy["prohibited_actions"] == ["RESEND_NEW_KEY", "DIRECT_COMPENSATION"]
    assert policy["new_key_retry"] is policy["direct_compensation"] is False
    assert policy["retry_key_must_equal"] == "idempotency_key"
    assert policy["retry_only_after"] == "verified_NOT_EXECUTED"
    assert policy["ledger_absence_is_not_NOT_EXECUTED"] is True


@pytest.mark.parametrize("field,value", [("business_success", True), ("posting_status", "POSTED"),
                                       ("command_status", "SUCCEEDED"), ("next_action", "RETRY_SAME_KEY")])
def test_a05_unconfirmed_unknown_cannot_succeed_or_retry(registry, field, value):
    record = convergence()
    record[field] = value
    invalid(registry, "p3-unknown.json", "Record", record)


def test_same_key_retry_requires_confirmed_nonexecution(registry):
    record = convergence("NOT_EXECUTED", True)
    for field, value in (("query_verified", False), ("query_evidence_ref", None),
                         ("query_evidence_hash", None), ("retry_idempotency_key", None),
                         ("recorded_receipt", None), ("query_attempt_count", 0)):
        bad = copy.deepcopy(record)
        bad[field] = value
        invalid(registry, "p3-unknown.json", "Record", bad)
    # 同键相等属于跨字段约束，x-policy 与 L3 验证；JSON Schema 不伪装可证明它。


def test_a08_saga_physical_before_accounting_and_explicit_branches(registry):
    value = saga()
    valid(registry, "p3-saga.json", "View", value)
    bad = copy.deepcopy(value)
    bad["recovery_order"].reverse()
    invalid(registry, "p3-saga.json", "View", bad)
    bad = copy.deepcopy(value)
    bad["accounting_recovery_started"] = True
    invalid(registry, "p3-saga.json", "View", bad)
    bad.update(physical_verified_and_restored=True, physical_evidence_refs=["trace-demo"])
    valid(registry, "p3-saga.json", "View", bad)
    for key in ("shipped", "not_shipped", "posted", "not_posted"):
        bad = copy.deepcopy(value)
        del bad["steps"][0]["branches"][key]
        invalid(registry, "p3-saga.json", "View", bad)
    for domain, flag, selected in (("PHYSICAL", "is_shipped", "shipped"),
                                   ("ACCOUNTING", "is_posted", "posted")):
        record = step(domain)
        record[flag] = True
        invalid(registry, "p3-saga.json", "Step", record)
        record["selected_branch"] = selected
        valid(registry, "p3-saga.json", "Step", record)
    policy = load("schemas/p3-saga.json")["x-saga-policy"]
    assert policy["recovery_domain_order"] == ["PHYSICAL", "ACCOUNTING"]
    assert policy["compensation_is_new_business_action"] is True
    assert policy["new_compensation_command_and_key"] is True
    assert policy["compensation_is_database_rollback"] is False
    assert policy["compensation_uses_same_controlled_pipeline"] is True


def test_compensation_failed_or_unknown_never_COMPENSATED(registry):
    value = saga()
    current = step(started=True)
    current.update(compensation_supported=True, compensation_required=True,
                   compensable_action_ref="employee-planning", compensation_permission=permission(),
                   compensation=compensation("FAILED"))
    current["branches"]["not_shipped"].update(action_ref="employee-planning", permission=permission(), manual_required=False)
    value.update(steps=[current], status="MANUAL_REVIEW", manual_queue_ref="trace-demo")
    valid(registry, "p3-saga.json", "View", value)
    bad = copy.deepcopy(value)
    bad.update(status="COMPENSATED", all_compensation_receipts_verified=True,
               physical_verified_and_restored=True, physical_evidence_refs=["trace-demo"], manual_queue_ref=None)
    invalid(registry, "p3-saga.json", "View", bad)
    bad["steps"][0]["compensation"] = compensation("UNKNOWN")
    invalid(registry, "p3-saga.json", "View", bad)
    bad["steps"][0]["compensation"] = compensation()
    valid(registry, "p3-saga.json", "View", bad)
    bad["steps"][0]["compensation"] = None
    invalid(registry, "p3-saga.json", "View", bad)
    current["command_status"] = "UNKNOWN"
    current["authority_receipt"] = None
    invalid(registry, "p3-saga.json", "Step", current)
    assert load("schemas/p3-saga.json")["x-saga-policy"]["compensation_failure"] == "MANUAL_REVIEW"


def test_cancellation_distinguishes_started_and_not_started(registry):
    value = cancellation()
    valid(registry, "p3-cancellation.json", "Result", value)
    value["steps"][0]["stopped_before_start"] = False
    invalid(registry, "p3-cancellation.json", "Result", value)
    value = cancellation()
    value["steps"][0].update(started=True, stopped_before_start=False,
                             command_status="UNKNOWN", confirmation_required=True)
    valid(registry, "p3-cancellation.json", "Result", value)
    bad = copy.deepcopy(value)
    bad["steps"][0]["compensation_command_ref"] = "trace-demo"
    invalid(registry, "p3-cancellation.json", "Result", bad)
    bad = copy.deepcopy(value)
    bad.update(task_status="CANCELLED", side_effects_reconciled=True, applicable_compensations_verified=True)
    invalid(registry, "p3-cancellation.json", "Result", bad)
    bad["steps"][0].update(command_status="SUCCEEDED", authority_receipt_ref="trace-demo")
    valid(registry, "p3-cancellation.json", "Result", bad)
    bad["task_cancelled_is_reversal"] = True
    invalid(registry, "p3-cancellation.json", "Result", bad)


@pytest.mark.parametrize("capability", ["idempotency_support", "status_query", "compensation"])
def test_false_toolspec_capability_denies_unattended_high_risk(registry, capability):
    tool = copy.deepcopy(load("schemas/tool-spec.json")["examples"][0])
    tool.update(tenant_id="tenant-demo", tool_id="employee-planning", version="3.0.0",
                risk_class="R3", idempotency_support=True, status_query=True, compensation=True,
                required_scopes=["employee-planning"], adapter_owner="employee-planning")
    for field in ("input_schema", "output_schema"):
        tool[field] = dict(schema_ref="task.json", schema_version="3.0.0", schema_hash=HASH)
    tool[capability] = False
    value = dict(context=context(), tool_spec=tool, unattended_high_risk_write_allowed=False,
                 adapter_ledger_required=True, serial_object_lock_required=True,
                 manual_reconciliation_required=True, reason_ref="trace-demo")
    valid(registry, "p3-tool-admission.json", "Admission", value)
    value["unattended_high_risk_write_allowed"] = True
    invalid(registry, "p3-tool-admission.json", "Admission", value)
    value["unattended_high_risk_write_allowed"] = False
    for field in ("adapter_ledger_required", "serial_object_lock_required", "manual_reconciliation_required"):
        bad = copy.deepcopy(value)
        bad[field] = False
        invalid(registry, "p3-tool-admission.json", "Admission", bad)
    del value["tool_spec"][capability]
    invalid(registry, "p3-tool-admission.json", "Admission", value)


@pytest.mark.parametrize("schema,definition,make", [
    ("p3-approval.json", "Fact", approval), ("p3-execution.json", "Credential", credential),
    ("p3-command.json", "View", command), ("p3-unknown.json", "Record", convergence),
    ("p3-saga.json", "View", saga), ("p3-cancellation.json", "Result", cancellation),
])
def test_nested_context_required_and_sensitive_inline_payload_rejected(registry, schema, definition, make):
    original = make()
    valid(registry, schema, definition, original)
    for field in context():
        bad = copy.deepcopy(original)
        del bad["context"][field]
        invalid(registry, schema, definition, bad)
    for field in ("tenant_id", "payload", "prompt", "confidence", "approved_by_model"):
        bad = copy.deepcopy(original)
        bad[field] = "task-demo"
        invalid(registry, schema, definition, bad)
    bad = copy.deepcopy(original)
    bad["context"]["deadline_at"] = "2026-10-05T08:00:00+08:00"
    invalid(registry, schema, definition, bad)


@pytest.mark.parametrize("schema,definition,id_field,url", [
    ("p3-command.json", "Accepted", "command_id", "/internal/v1/commands/task-demo/execution-record"),
    ("p3-saga.json", "Accepted", "saga_id", "/internal/v1/sagas/task-demo"),
    ("p3-cancellation.json", "Accepted", "request_id",
     "/api/v1/tasks/task-demo/cancellation-requests/trace-demo"),
])
def test_202_acceptance_never_claims_effect_or_posting(registry, schema, definition, id_field, url):
    value = dict(context=context(), status="accepted", business_success=False,
                 posting_status="NOT_CONFIRMED", status_url=url)
    value[id_field] = "trace-demo" if id_field == "request_id" else "task-demo"
    if schema == "p3-cancellation.json":
        value["task_id"] = "task-demo"
    valid(registry, schema, definition, value)
    for field, changed in (("status", "SUCCEEDED"), ("business_success", True), ("posting_status", "POSTED")):
        bad = copy.deepcopy(value)
        bad[field] = changed
        invalid(registry, schema, definition, bad)


def test_manual_queue_entry_durable_evidence_contract(registry):
    value = dict(context=context(), manual_queue_ref="trace-demo", command_ref="task-demo",
                 saga_ref=None, status="MANUAL_REVIEW", reason_ref="trace-demo", last_error=None,
                 evidence_refs=["trace-demo"], created_at=UTC, attempt_count=1,
                 status_source="transaction-service")
    valid(registry, "p3-unknown.json", "ManualReviewEntry", value)
    for field in ("manual_queue_ref", "reason_ref", "created_at", "evidence_refs"):
        bad = copy.deepcopy(value)
        del bad[field]
        invalid(registry, "p3-unknown.json", "ManualReviewEntry", bad)
