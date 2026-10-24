"""P1.5 静态契约门禁：仅读取 packages/contracts，零业务实现依赖。

使用既有 requirements-dev 的 jsonschema / referencing / pytest，不访问网络、数据库或服务。
数据库唯一性和事务原子性只能检查契约声明；运行证明由 SPEC 的 L3 断言承担。
EXPECTED_EVENTS 从 P0 门禁 EXPECTED_EVENTS 逐项抄录，测试不读取 tests 或上位文档。
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
LOCKED_SHA256 = {
    "openapi/p0.openapi.json": "54adb747a58653c24bfafefcc41e953e99f53191d9f86a001d5840b31f4599f7",
    "openapi/p1.openapi.json": "d5ccbbf61a092de2fe58d10957858f627f02783cf881d76a4bd44c1b11f9f9ae",
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
    "schemas/service-manifest.json": "f406573fdbc6706f661f8ee1c385e0226387a737812687b85217fe27d2522e84",
    "schemas/state-machines.json": "1cc989f9f6119e437678ee14a7d293f9e3183f7b96d49548ae7910641359f273",
    "schemas/task.json": "6e6fa29630e82a6e5c8efedb4288238b03f695e395c0c69e82a8a1eb1f0acd36",
    "schemas/tool-spec.json": "ab130721f780ea1781aa4159332f9db0ccfba55183efddf6e90ff36663718f5b"
}
NEW_SCHEMAS = (
    "p15-outbox.json", "p15-inbox.json", "p15-dlq.json", "p15-receipt.json",
    "p15-lifecycle.json", "p15-event-api.json",
)
EXPECTED_EVENTS = {
    "task.accepted", "run.waiting_approval", "approval.decided",
    "command.accepted", "command.succeeded", "command.unknown",
    "saga.compensation_required", "erp.demand.released", "pmc.schedule.published",
    "mes.operation.completed", "wms.stock.changed", "edge.decision.observed",
    "edge.action.executed", "release.evaluated",
}
EXPECTED_REQUIRED = {
    "event_id", "event_type", "schema_version", "tenant_id", "factory_id",
    "source_system", "aggregate_type", "aggregate_id", "aggregate_version",
    "occurred_at", "received_at", "trace_id", "causation_id", "correlation_id",
    "data_class", "payload",
}
EXPECTED_INTERFACES = {
    ("get", "/internal/v1/tasks/{task_id}"): ("control", "L1", "query"),
    ("get", "/internal/v1/runs/{run_id}"): ("runtime", "L2", "query"),
    ("post", "/internal/v1/events"): ("owner-local", "L3", "fact"),
    ("get", "/internal/v1/events/dlq/{dlq_id}"): ("owner-local", "L3", "query"),
    ("post", "/internal/v1/events/dlq/{dlq_id}/replay"): ("owner-local", "L3", "command"),
    ("post", "/internal/v1/commands/{command_id}/receipts"): ("transaction", "L3", "fact"),
}
ERROR_CODES = {"400", "401", "403", "404", "409", "422", "429", "503"}
OWNERS = {
    "security-gateway", "control-plane", "decision-service", "hermes-runtime",
    "manufacturing-core", "tool-gateway", "transaction-service", "trajectory-eval",
}
UTC = "2026-10-04T00:00:00Z"


def pairs(items):
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
                      object_pairs_hook=pairs, parse_constant=reject_constant)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@pytest.fixture(scope="module")
def docs():
    documents = {name: load("schemas/" + name) for name in NEW_SCHEMAS}
    documents["api"] = load("openapi/p15.openapi.json")
    registry = Registry()
    dependencies = files_of("p0") | files_of("p1") | files_of("p15")
    for relative in sorted(p for p in dependencies if p.startswith("schemas/")):
        file = CONTRACT / relative
        assert file.resolve().is_relative_to(CONTRACT.resolve())
        document = load(relative)
        registry = registry.with_resource(
            file.name, Resource.from_contents(document, default_specification=DRAFT202012))
    documents["registry"] = registry
    return documents


def errors(docs, name, value, definition=None):
    pointer = "#/$defs/" + definition if definition else ""
    validator = Draft202012Validator({"$ref": name + pointer},
                                     registry=docs["registry"], format_checker=FormatChecker())
    return list(validator.iter_errors(value))


def context():
    return dict(tenant_id="tenant-demo", factory_id="factory-demo", actor_id="user-demo",
                employee_id="employee-planning", task_id="task-demo", trace_id="trace-demo",
                data_class="C2", schema_version="1.0.0", deadline_at=UTC)


def event(version=0):
    return dict(event_id="task-demo", event_type="task.accepted", schema_version="1.0.0",
                tenant_id="tenant-demo", factory_id="factory-demo", source_system="employee-planning",
                aggregate_type="Task", aggregate_id="task-demo", aggregate_version=version,
                occurred_at=UTC, received_at=UTC, trace_id="trace-demo", causation_id="task-demo",
                correlation_id="task-demo", data_class="C2",
                payload=dict(payload_ref="task-demo", payload_hash="trace-demo"))


def error():
    return dict(code="trace-demo", message="task-demo", trace_id="trace-demo",
                retryable=False, details={})


def outbox():
    return dict(context=context(), producer_unit="control-plane", event=event(), state="PENDING",
                attempt_count=0, created_at=UTC, sent_at=None, broker_ack_ref=None,
                last_error=None, dlq_ref=None, publication_evidence_ref="task-demo")


def inbox():
    return dict(context=context(), consumer_unit="manufacturing-core", event=event(), state="RECEIVED",
                applied_version_before=-1, applied_version_after=-1, projection_applied=False,
                side_effect_emitted=False, attempt_count=0, received_at=UTC, processed_at=None,
                pull_queue_ref=None, dlq_ref=None, last_error=None, publication_evidence_ref="task-demo")


def dead_letter():
    return dict(context=context(), dlq_id="task-demo", owner_unit="manufacturing-core", origin="INBOX",
                event=event(), error=error(), attempt_count=1, replay_count=0, handling_status="OPEN",
                created_at=UTC, updated_at=UTC, operator_ref=None, handling_reason_code=None,
                resolution_ref=None, record_version=0, publication_evidence_ref="task-demo")


def receipt(outcome="UNKNOWN"):
    return dict(context=context(), command_id="task-demo", idempotency_key="task-demo",
                command_payload_hash="trace-demo", receipt_id="task-demo", source_system="employee-planning",
                adapter_unit="manufacturing-core", outcome=outcome, authority_receipt_ref=None,
                authority_receipt_hash=None, authority_verified=False, confirmed_at=None, recorded_at=UTC,
                command_status="UNKNOWN", business_success=False, posting_status="NOT_CONFIRMED",
                posting_evidence_ref=None, status_source="transaction-service")


@pytest.mark.parametrize("relative,expected", LOCKED_SHA256.items())
def test_p0_p1_byte_hashes_are_locked(relative, expected):
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert hashlib.sha256((CONTRACT / relative).read_bytes()).hexdigest() == expected


def test_locked_inventory_is_complete():
    assert len(LOCKED_SHA256) == 19  # P0 12 文件 + P1 7 文件
    assert files_of("p0") | files_of("p1") == set(LOCKED_SHA256), "P0/P1 文件清单与哈希锁定集合不一致"
    assert files_of("p15") == {"openapi/p15.openapi.json"} | {
        "schemas/" + name for name in NEW_SCHEMAS
    }, "P1.5 文件清单与门禁覆盖集合不一致"
    assert unregistered() == set(), f"存在未经阶段登记的契约文件: {sorted(unregistered())}"
    assert missing() == set(), f"清单登记了但磁盘缺失的契约文件: {sorted(missing())}"


@pytest.mark.parametrize("name", NEW_SCHEMAS)
def test_meta_schema_closed_objects_and_no_semantic_ref_siblings(docs, name):
    document = docs[name]
    assert document["$schema"] == DIALECT
    Draft202012Validator.check_schema(document)
    for node in walk(document):
        if "$ref" in node:
            assert set(node) == {"$ref"}  # 新文件采用更严格的纯引用形式
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            assert not set(load("schemas/common-context.json")["required"]) & node["properties"].keys()  # Context 不平铺


def test_all_new_refs_resolve_inside_contracts(docs):
    roots = [CONTRACT / "schemas" / n for n in NEW_SCHEMAS] + [CONTRACT / "openapi/p15.openapi.json"]
    def resolve(source, reference):
        parsed = urlsplit(reference)
        assert not parsed.scheme and not parsed.netloc and not parsed.query
        relative = unquote(parsed.path)
        assert not relative.startswith("/") and ":" not in relative and "\\" not in relative
        target = (source.parent / relative).resolve() if relative else source.resolve()
        assert target.is_relative_to(CONTRACT.resolve())
        document = load(target.relative_to(CONTRACT.resolve()).as_posix())
        fragment = unquote(parsed.fragment)
        assert not fragment or fragment.startswith("/")
        for token in fragment.split("/")[1:] if fragment else ():
            assert not re.search(r"~(?![01])", token)
            document = document[token.replace("~1", "/").replace("~0", "~")]
        return document
    for source in roots:
        document = load(source.relative_to(CONTRACT).as_posix())
        for node in walk(document):
            if "$ref" in node:
                assert set(node) == {"$ref"}
                assert isinstance(resolve(source, node["$ref"]), (dict, bool))


def test_openapi_inventory_auth_error_context_and_action_semantics(docs):
    api = docs["api"]
    assert api["openapi"] == "3.1.0" and api["jsonSchemaDialect"] == DIALECT
    assert api["info"]["version"] == "3.0.0"
    actual = {(method, path): op for path, item in api["paths"].items() for method, op in item.items()}
    assert actual.keys() == EXPECTED_INTERFACES.keys()
    old_paths = set(load("openapi/p0.openapi.json")["paths"]) | set(load("openapi/p1.openapi.json")["paths"])
    assert not set(api["paths"]) & old_paths
    assert len({op["operationId"] for op in actual.values()}) == len(actual)
    for key, (owner, layer, kind) in EXPECTED_INTERFACES.items():
        op = actual[key]
        assert key[1].startswith(("/api/v1/", "/internal/v1/"))
        assert (op["x-owner"], op["x-interface-layer"], op["x-operation-kind"]) == (owner, layer, kind)
        assert op["security"] == [{"InternalIdentity": []}]
        assert "tenant_matches_context_and_authenticated_identity" in op["x-required-enforcement"]
        assert {code for code in op["responses"] if code.startswith("2")} == ({"202"} if kind == "command" else {"200"})
        for code in ERROR_CODES:
            assert op["responses"][code] == {"$ref": "#/components/responses/Error" + code}
        if key[0] == "get":
            assert {"$ref": "#/components/parameters/Context"} in op["parameters"]
        else:
            assert op["requestBody"]["required"] is True
        path_params = set(re.findall(r"\{([^}]+)\}", key[1]))
        assert {p["name"] for p in op.get("parameters", []) if p.get("in") == "path"} == path_params
        if owner == "owner-local":
            assert set(op["x-runtime-units"]) == OWNERS
    for name, target in {"CommonContext": "schemas/CommonContext", "Error": "schemas/Error",
                         "EventEnvelope": "schemas/EventEnvelope"}.items():
        assert api["components"]["schemas"][name] == {"$ref": "p0.openapi.json#/components/" + target}
    for name in ("ExternalIdentity", "InternalIdentity"):
        assert api["components"]["securitySchemes"][name] == {
            "$ref": "p0.openapi.json#/components/securitySchemes/" + name}
    assert api["components"]["parameters"]["Context"] == {"$ref": "p0.openapi.json#/components/parameters/Context"}
    assert api["x-contract-extension"]["new_interface_layer"] is False
    assert api["x-delivery"]["global_exactly_once"] is False


def test_catalog_and_envelope_are_p0_authority(docs):
    envelope = load("schemas/event-envelope.json")
    assert len(EXPECTED_EVENTS) == 14
    assert set(envelope["properties"]["event_type"]["enum"]) == EXPECTED_EVENTS
    assert set(envelope["required"]) == EXPECTED_REQUIRED
    assert envelope["x-delivery"]["semantics"] == "at_least_once"
    catalog = docs["p15-event-api.json"]
    assert catalog["x-event-catalog-source"] == "event-envelope.json#/properties/event_type/enum"
    assert catalog["x-envelope-source"] == "event-envelope.json"
    assert set(catalog["x-event-authorities"]) == EXPECTED_EVENTS
    assert catalog["x-event-authorities"]["command.succeeded"]["evidence"] == "verified_authority_receipt"
    for name in ("p15-outbox.json", "p15-inbox.json", "p15-dlq.json"):
        assert docs[name]["properties"]["event"] == {"$ref": "event-envelope.json"}
    assert catalog["$defs"]["DeliveryRequest"]["properties"]["event"] == {"$ref": "event-envelope.json"}
    for name in NEW_SCHEMAS:
        for node in walk(docs[name]):
            assert not ("properties" in node and "event_type" in node["properties"])
    assert set(envelope["$defs"]["MinimalPayload"]["properties"]) == {"payload_ref", "payload_hash"}


@pytest.mark.parametrize("mutation", ["missing_field", "extra_payload", "unknown_event", "missing_version"])
def test_new_event_wrappers_reject_invalid_envelope(docs, mutation):
    value = outbox()
    if mutation == "missing_field":
        del value["event"]["trace_id"]
    elif mutation == "extra_payload":
        value["event"]["payload"]["content"] = "task-demo"
    elif mutation == "unknown_event":
        value["event"]["event_type"] = "task-demo"
    else:
        del value["event"]["aggregate_version"]
    assert errors(docs, "p15-outbox.json", value)


def test_outbox_unique_logical_record_atomic_boundary_and_ack(docs):
    schema = docs["p15-outbox.json"]
    policy = schema["x-storage-policy"]
    assert policy["unique_key"] == ["context.tenant_id", "event.event_id"]
    assert policy["state_and_outbox_same_transaction"] is True
    assert policy["retry_creates_new_record"] is False
    assert policy["mark_sent_after"] == "durable_broker_ack"
    assert policy["cross_database_transaction"] is False
    assert policy["delivery_semantics"] == "at_least_once"
    value = outbox()
    assert not errors(docs, "p15-outbox.json", value)
    value["state"] = "SENT"
    assert errors(docs, "p15-outbox.json", value)
    value.update(sent_at=UTC, broker_ack_ref="trace-demo", attempt_count=1)
    assert not errors(docs, "p15-outbox.json", value)
    value["broker_ack_ref"] = None
    assert errors(docs, "p15-outbox.json", value)
    value = outbox()
    value.update(state="DLQ", attempt_count=1, last_error=error(), dlq_ref="task-demo")
    assert not errors(docs, "p15-outbox.json", value)
    value["last_error"] = None
    assert errors(docs, "p15-outbox.json", value)


def test_inbox_unique_event_atomic_projection_and_failure_not_consumed(docs):
    policy = docs["p15-inbox.json"]["x-storage-policy"]
    assert policy["unique_key"] == ["context.tenant_id", "event.event_id"]
    assert policy["unique_scope"] == "per_consumer"
    assert policy["projection_watermark_inbox_and_derived_outbox_same_transaction"] is True
    assert policy["ingress_durable_before_ack"] is True
    assert policy["cross_database_transaction"] is False
    assert policy["failed_or_gap_is_not_consumed"] is True
    assert policy["identity_conflict"] == policy["same_version_different_event"] == "dlq"
    value = inbox()
    assert not errors(docs, "p15-inbox.json", value)
    value.update(state="APPLIED", projection_applied=True, applied_version_after=0, processed_at=UTC)
    assert not errors(docs, "p15-inbox.json", value)
    value["projection_applied"] = False
    assert errors(docs, "p15-inbox.json", value)


@pytest.mark.parametrize("state", ["RECEIVED", "LATE", "GAP", "DLQ"])
def test_unapplied_inbox_states_cannot_claim_projection_or_side_effect(docs, state):
    value = inbox()
    value["state"] = state
    if state == "GAP":
        value["pull_queue_ref"] = "task-demo"
    elif state == "LATE":
        value["processed_at"] = UTC
    elif state == "DLQ":
        value.update(dlq_ref="task-demo", last_error=error(), attempt_count=1)
    assert not errors(docs, "p15-inbox.json", value)
    value["projection_applied"] = True
    assert errors(docs, "p15-inbox.json", value)
    value["projection_applied"] = False
    value["side_effect_emitted"] = True
    assert errors(docs, "p15-inbox.json", value)


@pytest.mark.parametrize("before,version,result,apply,after", [
    (-1, 0, "APPLIED", True, 0), (3, 4, "APPLIED", True, 4),
    (3, 2, "LATE", False, 3), (3, 3, "DLQ", False, 3),
    (3, 5, "GAP", False, 3), (-1, 1, "GAP", False, -1),
])
def test_declared_version_decision_table(docs, before, version, result, apply, after):
    # 有限声明表的解释器；不模拟消费者、存储或锁，也不作为 L3 原子性证据。
    policy = docs["p15-inbox.json"]["x-version-policy"]
    assert policy["aggregate_key"] == load("schemas/event-envelope.json")["x-delivery"]["aggregate_key"]
    assert policy["aggregate_key"] == ["tenant_id", "aggregate_id"]
    assert policy["initial_applied_version"] == -1 and policy["first_event_version"] == 0
    assert policy["precedence"] == ["identity_conflict", "already_applied_event_id", "version_comparison"]
    assert policy["duplicate_event_id"] == "DUPLICATE"
    assert policy["gap_action"] == "durable_pull_queue"
    assert policy["unrecoverable_gap"] == "dlq_or_manual"
    assert policy["late_event_overwrite"] is policy["watermark_decrease"] is False
    relation = "lt" if version < before else "eq" if version == before else "next" if version == before + 1 else "gap"
    rules = {item["relation"]: item for item in policy["comparison_rules"]}
    assert len(rules) == 4
    rule = rules[relation]
    assert (rule["result"], rule["apply"], rule["advance"]) == (result, apply, apply)
    assert (version if rule["advance"] else before) == after
    assert after >= before
    value = inbox()
    value.update(event=event(version), state=result, applied_version_before=before,
                 applied_version_after=after, projection_applied=apply)
    if result in {"APPLIED", "LATE"}:
        value["processed_at"] = UTC
    elif result == "GAP":
        value["pull_queue_ref"] = "task-demo"
    else:
        value.update(dlq_ref="task-demo", last_error=error(), attempt_count=1)
    assert not errors(docs, "p15-inbox.json", value)
    if result == "GAP":
        value["pull_queue_ref"] = None
        assert errors(docs, "p15-inbox.json", value)


def test_dlq_retains_diagnostics_and_replay_uses_original_rules(docs):
    schema = docs["p15-dlq.json"]
    assert {"error", "attempt_count", "handling_status", "event", "publication_evidence_ref"} <= set(schema["required"])
    policy = schema["x-replay-policy"]
    for field in ("same_event_id", "same_tenant", "same_aggregate_version", "same_payload_hash",
                  "same_publication_evidence_ref", "original_inbox_rules", "append_only_attempt_history"):
        assert policy[field] is True
    for field in ("replay_is_business_retry", "executed_edge_action_replay", "direct_projection_write",
                  "close_means_business_success"):
        assert policy[field] is False
    assert schema["x-storage-policy"]["purge_on_replay"] is False
    value = dead_letter()
    assert not errors(docs, "p15-dlq.json", value)
    for field in ("error", "attempt_count", "event", "handling_status"):
        bad = copy.deepcopy(value)
        del bad[field]
        assert errors(docs, "p15-dlq.json", bad)
    value["handling_status"] = "RESOLVED"
    assert errors(docs, "p15-dlq.json", value)
    value.update(operator_ref="user-demo", handling_reason_code="employee-planning", resolution_ref="trace-demo")
    assert not errors(docs, "p15-dlq.json", value)


@pytest.mark.parametrize("outcome", ["accepted", "UNKNOWN"])
@pytest.mark.parametrize("field,value", [
    ("business_success", True), ("posting_status", "POSTED"),
    ("command_status", "SUCCEEDED"), ("command_status", "FAILED"),
])
def test_a14_accepted_and_unknown_never_success_or_posted(docs, outcome, field, value):
    record = receipt(outcome)
    assert not errors(docs, "p15-receipt.json", record)
    record[field] = value
    assert errors(docs, "p15-receipt.json", record)


def test_success_requires_verified_authority_and_posting_specific_evidence(docs):
    value = receipt("SUCCEEDED")
    value.update(command_status="SUCCEEDED", business_success=True)
    assert errors(docs, "p15-receipt.json", value)
    value.update(authority_receipt_ref="task-demo", authority_receipt_hash="trace-demo",
                 authority_verified=True, confirmed_at=UTC)
    assert not errors(docs, "p15-receipt.json", value)
    for field, invalid in (("authority_verified", False), ("authority_receipt_ref", None),
                           ("authority_receipt_hash", None), ("confirmed_at", None)):
        bad = copy.deepcopy(value)
        bad[field] = invalid
        assert errors(docs, "p15-receipt.json", bad)
    value["posting_status"] = "POSTED"
    assert errors(docs, "p15-receipt.json", value)
    value["posting_evidence_ref"] = "trace-demo"
    assert not errors(docs, "p15-receipt.json", value)
    policy = docs["p15-receipt.json"]["x-success-policy"]
    assert policy["only_authority_receipt"] is True
    assert policy["accepted_is_posted"] is policy["unknown_is_success"] is False
    assert policy["not_executed_retry_key"] == "original" and policy["missing_receipt"] == "UNKNOWN"
    assert policy["timeout_means_failed"] is policy["task_run_terminal_is_posting_evidence"] is False
    assert set(policy["success_requires_verified_binding"]) == {
        "tenant_id", "command_id", "idempotency_key", "command_payload_hash", "source_system", "source_version"}
    assert docs["p15-receipt.json"]["x-storage-policy"]["receipt_command_status_and_outbox_same_transaction"] is True
    request = docs["p15-receipt.json"]["$defs"]["Submission"]
    assert not {"outcome", "command_status", "business_success", "authority_verified"} & request["properties"].keys()


@pytest.mark.parametrize("outcome,status", [("FAILED", "FAILED"), ("NOT_EXECUTED", "UNKNOWN")])
def test_negative_authority_receipts_require_proof_and_do_not_succeed(docs, outcome, status):
    value = receipt(outcome)
    value.update(command_status=status)
    assert errors(docs, "p15-receipt.json", value)
    value.update(authority_receipt_ref="task-demo", authority_receipt_hash="trace-demo",
                 authority_verified=True, confirmed_at=UTC)
    assert not errors(docs, "p15-receipt.json", value)
    value["business_success"] = True
    assert errors(docs, "p15-receipt.json", value)


def test_acks_and_replay_acceptance_do_not_claim_business_success(docs):
    value = dict(context=context(), event_id="task-demo", consumer_unit="manufacturing-core",
                 disposition="DUPLICATE", durable=True, business_success=False,
                 pull_queue_ref=None, dlq_ref=None)
    assert not errors(docs, "p15-event-api.json", value, "DeliveryAck")
    for field in ("business_success", "durable"):
        bad = copy.deepcopy(value)
        bad[field] = not bad[field]
        assert errors(docs, "p15-event-api.json", bad, "DeliveryAck")
    value = dict(context=context(), dlq_id="task-demo", replay_request_id="task-demo",
                 status="accepted", business_success=False, posting_status="NOT_CONFIRMED",
                 status_url="/internal/v1/events/dlq/task-demo")
    assert not errors(docs, "p15-event-api.json", value, "ReplayAccepted")
    for field, wrong in (("business_success", True), ("posting_status", "POSTED"), ("status", "SUCCEEDED")):
        bad = copy.deepcopy(value)
        bad[field] = wrong
        assert errors(docs, "p15-event-api.json", bad, "ReplayAccepted")
    policy = docs["p15-event-api.json"]["$defs"]["ReplayAccepted"]["x-idempotency"]
    assert policy["same_key_different_payload"] == "409"
    assert policy["request_and_replay_queue_same_transaction"] is True


def test_single_truth_owner_reads_and_checkpoint_recovery(docs):
    lifecycle = docs["p15-lifecycle.json"]
    policy = lifecycle["x-ownership-policy"]
    assert policy["Task"]["owner"] == "control-plane" and policy["Task"]["store"] == "control"
    assert policy["Run"]["owner"] == "hermes-runtime" and policy["Run"]["store"] == "runtime"
    assert policy["Command"]["owner"] == "transaction-service"
    assert policy["Task"]["manufacturing_journal_is_authority"] is False
    assert policy["Run"]["control_projection_is_authority"] is False
    for field in ("cross_owner_write", "cross_owner_join", "cross_owner_foreign_key", "in_memory_is_authority"):
        assert policy[field] is False
    assert policy["owner_unavailable"] == "fail_closed_503" and policy["restart_preserves_state"] is True
    recovery = lifecycle["x-recovery-policy"]
    for field in ("checkpoint_and_run_same_transaction", "cas_required", "lease_and_fencing_required",
                  "query_existing_commands_before_resume"):
        assert recovery[field] is True
    assert recovery["successful_steps_reexecuted"] is recovery["cancel_inflight_means_rollback"] is False
    for kind, source, status in (("Task", "control-plane", "ACCEPTED"), ("Run", "hermes-runtime", "CREATED")):
        definition = kind + "View"
        assert lifecycle["$defs"][definition]["properties"]["status"] == {
            "$ref": "state-machines.json#/$defs/" + kind}
        assert policy[kind]["cross_owner_read"] == "GET /internal/v1/" + kind.lower() + "s/{" + kind.lower() + "_id}"
        value = dict(context=context(), status=status, status_source=source, updated_at=UTC,
                     record_version=0, command_refs=[], business_execution_source="transaction-service",
                     is_posting_evidence=False)
        if kind == "Run":
            value["run_id"] = "task-demo"
        assert not errors(docs, "p15-lifecycle.json", value, definition)
        value["is_posting_evidence"] = True
        assert errors(docs, "p15-lifecycle.json", value, definition)


@pytest.mark.parametrize("name,definition,value", [
    ("p15-outbox.json", None, outbox()), ("p15-inbox.json", None, inbox()),
    ("p15-dlq.json", None, dead_letter()), ("p15-receipt.json", None, receipt()),
])
def test_context_required_nested_and_utc_only(docs, name, definition, value):
    assert not errors(docs, name, value, definition)
    for field in context():
        bad = copy.deepcopy(value)
        del bad["context"][field]
        assert errors(docs, name, bad, definition)
    bad = copy.deepcopy(value)
    bad["tenant_id"] = "tenant-demo"
    assert errors(docs, name, bad, definition)
    for wrong in ("2026-10-04T08:00:00+08:00", "2026-10-04T00:00:00", "2026-10-04T00:00:00+0800"):
        # 注意：契约用 `pattern` 表达时间格式，因此它保证的是**格式**合法，
        # 不保证**日历真实性**。实测 `2026-13-45T99:99:99Z` 也能通过该 pattern，
        # 所以"2 月 30 日"这类值不属于本断言的范围（要断言它需要契约改用
        # `format: date-time` 并在校验器上启用 format 检查，那是契约变更，须走 ADR）。
        # 实现侧的 `context._is_real_utc_timestamp` 做了更严格的日历校验，属加严而非违约。
        bad = copy.deepcopy(value)
        bad["context"]["deadline_at"] = wrong
        assert errors(docs, name, bad, definition)
