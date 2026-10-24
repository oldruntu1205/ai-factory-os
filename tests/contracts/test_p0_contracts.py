"""P0 静态契约门禁：只读取 openapi/ 与 schemas/，不导入实现或联网。

文件采用 JSON（亦为 YAML 1.2 的合法子集）序列化。这里的实例检查器只覆盖
本契约使用的关键字，不是通用 JSON Schema / OpenAPI 校验器。
x-registry / x-required-enforcement 是 L2 必须执行的语义约束；本测试检查声明，
不能证明真实身份认证、登记查询、服务启动或 Mock 运行已经实现。
"""
import copy
import datetime as dt
import json
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest

from .contract_inventory import files_of, missing, unregistered


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "packages" / "contracts"
READ_ROOTS = (CONTRACT / "openapi", CONTRACT / "schemas")
# P0 只加载登记处中的本阶段文件；原始字节由 P1 门禁的固定哈希保护。
DIALECT = "https://json-schema.org/draft/2020-12/schema"

# 从 v3.0.0 §10.1 逐行抄录；不从 OpenAPI 或实现生成期望值。
EXPECTED_INTERFACES = (
    ("control", "POST", "/api/v1/tasks"),
    ("control", "GET", "/api/v1/tasks/{task_id}"),
    ("control", "POST", "/api/v1/approvals/{approval_id}/decisions"),
    ("control", "POST", "/internal/v1/policy/evaluate"),
    ("decision", "POST", "/internal/v1/decisions"),
    ("runtime", "POST", "/internal/v1/runs"),
    ("runtime", "POST", "/internal/v1/runs/{run_id}/resume"),
    ("security", "POST", "/internal/v1/model-invocations"),
    ("manufacturing", "POST", "/internal/v1/contexts/query"),
    ("tool", "POST", "/internal/v1/tool-requests"),
    ("transaction", "POST", "/internal/v1/commands"),
    ("transaction", "GET", "/internal/v1/commands/{command_id}"),
    ("tool", "POST", "/internal/v1/tool-executions"),
    ("edge", "POST", "/edge/v1/decisions"),
    ("edge", "POST", "/edge/v1/releases/activate"),
)
# §14.2 的运维路径是明确例外；不发明版本化 health 别名。
EXPECTED_HEALTH = {("GET", "/health/live"), ("GET", "/health/ready")}
ERROR_STATUSES = {"400", "401", "403", "404", "409", "422", "429", "503"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
CONTEXT_FIELDS = {
    "tenant_id", "factory_id", "actor_id", "employee_id", "task_id",
    "trace_id", "data_class", "schema_version", "deadline_at",
}
EXPECTED_STATES = {
    "Task": ["ACCEPTED", "RUNNING", "WAITING_APPROVAL", "WAITING_HUMAN", "SUCCEEDED", "FAILED", "CANCELLED"],
    "Run": ["CREATED", "RUNNING", "CHECKPOINTED", "WAITING", "COMPLETED", "FAILED", "CANCELLED"],
    "Command": ["ACCEPTED", "AUTHORIZED", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN", "MANUAL_REVIEW"],
    "Saga": ["RUNNING", "SUCCEEDED", "COMPENSATING", "COMPENSATED", "MANUAL_REVIEW"],
    "EdgeRelease": ["DRAFT", "SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED", "ACTIVE", "REVOKED"],
}
EXPECTED_FLOWS = {
    "Task": "ACCEPTED → RUNNING → WAITING_APPROVAL/WAITING_HUMAN → SUCCEEDED/FAILED/CANCELLED",
    "Run": "CREATED → RUNNING → CHECKPOINTED/WAITING → COMPLETED/FAILED/CANCELLED",
    "Command": "ACCEPTED → AUTHORIZED → EXECUTING → SUCCEEDED/FAILED/UNKNOWN；可进入MANUAL_REVIEW",
    "Saga": "RUNNING → SUCCEEDED；失败后COMPENSATING → COMPENSATED或MANUAL_REVIEW",
    "EdgeRelease": "DRAFT → SCHEMA_FROZEN → CALIBRATED → SHADOW → APPROVED → ACTIVE → REVOKED",
}
EXPECTED_OWNERS = {
    "security-gateway": "security", "control-plane": "control",
    "decision-service": "decision", "hermes-runtime": "runtime",
    "manufacturing-core": "manufacturing", "tool-gateway": "tools",
    "transaction-service": "transaction", "trajectory-eval": "evidence",
}
EXPECTED_EVENTS = {
    "task.accepted", "run.waiting_approval", "approval.decided",
    "command.accepted", "command.succeeded", "command.unknown",
    "saga.compensation_required", "erp.demand.released", "pmc.schedule.published",
    "mes.operation.completed", "wms.stock.changed", "edge.decision.observed",
    "edge.action.executed", "release.evaluated",
}
# 白名单只有4个对象；嵌套辅助结构继承所属对象的 tenant/global 作用域。
GLOBAL_OBJECTS = {
    ("error.json", ""),
    ("error.json", "/$defs/Details"),
    ("service-manifest.json", ""),
    ("api-models.json", "/$defs/Health"),
}
REQUIRED_SCHEMAS = {
    "common-context.json", "error.json", "command.json", "decision-result.json",
    "event-envelope.json", "tool-spec.json", "task.json", "command-status.json",
    "service-manifest.json", "state-machines.json", "api-models.json",
}


def reject_constant(value):
    raise ValueError(f"非法 JSON 常量: {value}")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, f"重复 JSON 键: {key}"
        result[key] = value
    return result


def pointer_token(value):
    return value.replace("~", "~0").replace("/", "~1")


def nodes(value, pointer="", include_examples=False):
    if isinstance(value, dict):
        yield pointer, value
        for key, child in value.items():
            if key in {"examples", "example"} and not include_examples:
                continue
            yield from nodes(child, pointer + "/" + pointer_token(key), include_examples)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from nodes(child, pointer + f"/{index}", include_examples)


class Documents:
    def __init__(self):
        self.files = {}
        for relative in sorted(files_of("p0")):
            file = CONTRACT / relative
            directory = file.parent
            assert directory.is_dir(), f"契约目录缺失: {directory}"
            assert file.is_file(), f"P0 契约文件缺失: {file}"
            assert file.suffix.lower() in {".json", ".yaml", ".yml"}, f"非契约文件: {file}"
            real = file.resolve()
            assert real.is_relative_to(directory.resolve()), f"目录外符号链接: {file}"
            # P0 选用 JSON 编码；若使用 YAML 后缀也须是合法 JSON 子集，无 PyYAML 依赖。
            try:
                self.files[real] = json.loads(
                    file.read_text(encoding="utf-8-sig"),
                    object_pairs_hook=unique_pairs, parse_constant=reject_constant,
                )
            except (ValueError, AssertionError) as exc:
                pytest.fail(f"契约不是合法的 JSON 编码（YAML 1.2 子集）: {file}: {exc}")
        assert self.files, "没有契约文件"
        self.schemas = {p: d for p, d in self.files.items() if p.is_relative_to(READ_ROOTS[1].resolve())}
        self.apis = {p: d for p, d in self.files.items() if p.is_relative_to(READ_ROOTS[0].resolve())}

    def schema(self, name):
        path = (READ_ROOTS[1] / name).resolve()
        assert path in self.files, f"Schema 缺失: {name}"
        return path, self.files[path]

    def resolve(self, source, reference):
        assert isinstance(reference, str) and reference, "空引用"
        parsed = urlsplit(reference)
        assert not parsed.scheme and not parsed.netloc and not parsed.query, f"非本地引用: {reference}"
        assert "\\" not in reference and not parsed.path.startswith("/"), f"绝对引用: {reference}"
        relative = unquote(parsed.path)
        assert not Path(relative).is_absolute() and ":" not in relative and "\\" not in relative, reference
        target = (source.parent / relative).resolve() if relative else source
        assert target in self.files, f"悬空或目录外引用: {source.name} -> {reference}"
        value = self.files[target]
        fragment = unquote(parsed.fragment)
        assert not fragment or fragment.startswith("/"), f"仅支持 JSON Pointer: {reference}"
        for raw in fragment.split("/")[1:] if fragment else ():
            assert not re.search(r"~(?![01])", raw), f"非法 Pointer 转义: {reference}"
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                assert token in value, f"悬空 Pointer: {reference}"
                value = value[token]
            elif isinstance(value, list):
                assert re.fullmatch(r"0|[1-9][0-9]*", token), reference
                index = int(token)
                assert index < len(value), f"悬空数组 Pointer: {reference}"
                value = value[index]
            else:
                pytest.fail(f"Pointer 穿过标量: {reference}")
        return target, value

    def deref(self, source, value):
        seen = set()
        while isinstance(value, dict) and "$ref" in value:
            key = (source, value["$ref"])
            assert key not in seen, f"循环直接引用: {key}"
            seen.add(key)
            siblings = {k: v for k, v in value.items() if k != "$ref"}
            source, value = self.resolve(source, value["$ref"])
            assert isinstance(value, dict)
            value = {**value, **siblings}
        return source, value

    def instance_errors(self, value, source, schema, location="$"):
        """只实现本契约实例用到的关键字；$ref siblings 同时生效。"""
        if schema is True:
            return []
        if schema is False:
            return [location + ": false schema"]
        assert isinstance(schema, dict)
        errors = []
        if "$ref" in schema:
            target, resolved = self.resolve(source, schema["$ref"])
            errors.extend(self.instance_errors(value, target, resolved, location))
        for member in schema.get("allOf", []):
            errors.extend(self.instance_errors(value, source, member, location))
        for keyword, exactly_one in (("anyOf", False), ("oneOf", True)):
            if keyword in schema:
                matches = sum(not self.instance_errors(value, source, member, location) for member in schema[keyword])
                if not matches or (exactly_one and matches != 1):
                    errors.append(location + ": " + keyword)
        if "if" in schema:
            condition = not self.instance_errors(value, source, schema["if"], location)
            branch = schema.get("then" if condition else "else", True)
            errors.extend(self.instance_errors(value, source, branch, location))
        if "not" in schema and not self.instance_errors(value, source, schema["not"], location):
            errors.append(location + ": not")
        if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
            errors.append(location + ": const")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(location + ": enum")
        types = schema.get("type", [])
        types = [types] if isinstance(types, str) else types
        checks = {
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": isinstance(value, bool),
            "null": value is None, "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        }
        if types and not any(checks[t] for t in types):
            return errors + [location + ": type"]
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(location + ": missing " + key)
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                for key in value.keys() - properties.keys():
                    errors.append(location + ": additional " + key)
            for key in value.keys() & properties.keys():
                errors.extend(self.instance_errors(value[key], source, properties[key], location + "." + key))
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
                errors.append(location + ": item count")
            if schema.get("uniqueItems"):
                encoded = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
                if len(set(encoded)) != len(encoded):
                    errors.append(location + ": uniqueItems")
            if "items" in schema:
                for index, item in enumerate(value):
                    errors.extend(self.instance_errors(item, source, schema["items"], location + f"[{index}]"))
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                errors.append(location + ": minLength")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append(location + ": pattern")
            if schema.get("format") == "date-time":
                try:
                    parsed_time = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    assert parsed_time.utcoffset() == dt.timedelta(0)
                except (ValueError, AssertionError):
                    errors.append(location + ": UTC date-time")
        if checks["number"]:
            if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
                errors.append(location + ": range")
        return errors


@pytest.fixture(scope="module")
def docs():
    return Documents()


def operations(docs):
    for source, api in docs.apis.items():
        for path, item in api["paths"].items():
            for method, op in item.items():
                if method in HTTP_METHODS:
                    yield source, path, method.upper(), op


def test_stage_inventory_is_complete():
    assert unregistered() == set(), f"存在未经阶段登记的契约文件: {sorted(unregistered())}"
    assert missing() == set(), f"清单登记了但磁盘缺失的契约文件: {sorted(missing())}"


def test_documents_and_schema_dialects(docs):
    assert {p.name for p in docs.schemas} >= REQUIRED_SCHEMAS
    assert docs.apis
    for source, document in docs.schemas.items():
        assert document["$schema"] == DIALECT, source.name
        assert isinstance(document, dict)
    for source, api in docs.apis.items():
        assert re.fullmatch(r"3\.1\.\d+", api["openapi"]), source.name
        assert api["jsonSchemaDialect"] == DIALECT
        assert api["info"]["version"] == "3.0.0"


def test_all_references_are_local_and_resolve(docs):
    count = 0
    for source, document in docs.files.items():
        for pointer, node in nodes(document, include_examples=True):
            if "$ref" in node:
                _, target = docs.resolve(source, node["$ref"])
                assert isinstance(target, (dict, bool)), (source.name, pointer)
                count += 1
            if isinstance(node.get("schema_ref"), str):
                docs.resolve(source, node["schema_ref"])
    assert count > 0


def test_exact_interface_inventory_and_owners(docs):
    actual = {}
    operation_ids = set()
    for _, path, method, op in operations(docs):
        key = (method, path)
        assert key not in actual, f"重复接口: {key}"
        actual[key] = op
        assert op["operationId"] not in operation_ids
        operation_ids.add(op["operationId"])
    expected = {(method, path) for _, method, path in EXPECTED_INTERFACES} | EXPECTED_HEALTH
    assert actual.keys() == expected
    for owner, method, path in EXPECTED_INTERFACES:
        assert actual[(method, path)]["x-owner"] == owner
        assert path.startswith(("/api/v1/", "/internal/v1/", "/edge/v1/"))
        assert actual[(method, path)]["x-source"] == "§10.1"


def test_request_response_refs_and_uniform_explicit_errors(docs):
    for source, path, method, op in operations(docs):
        if method == "POST":
            assert op["requestBody"]["required"] is True
            body = op["requestBody"]["content"]["application/json"]["schema"]
            assert body["$ref"].startswith("#/components/schemas/")
            docs.resolve(source, body["$ref"])
        if method == "GET" and (method, path) not in EXPECTED_HEALTH:
            params = [docs.deref(source, p)[1] for p in op["parameters"]]
            context = next(p for p in params if p["name"] == "X-Context")
            assert context["required"] is True
            assert context["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/CommonContext"
        path_params = re.findall(r"\{([^}]+)\}", path)
        params = [docs.deref(source, p)[1] for p in op.get("parameters", [])]
        assert {p["name"] for p in params if p.get("in") == "path"} == set(path_params)
        assert all(p["required"] for p in params if p.get("in") == "path")
        assert ERROR_STATUSES <= op["responses"].keys(), path
        assert any(code.startswith("4") for code in op["responses"])
        for code, response in op["responses"].items():
            response_source, response = docs.deref(source, response)
            schema = response["content"]["application/json"]["schema"]
            assert schema["$ref"].startswith("#/components/schemas/"), (path, code)
            if code in ERROR_STATUSES:
                assert schema["$ref"] == "#/components/schemas/Error"
                error_source, error = docs.deref(response_source, schema)
                assert error_source.name == "error.json"
                assert set(error["required"]) == {"code", "message", "trace_id", "retryable", "details"}
                assert error["additionalProperties"] is False


def test_authentication_and_execution_boundary(docs):
    for _, path, method, op in operations(docs):
        if (method, path) in EXPECTED_HEALTH:
            assert op["x-global-object"] is True
            continue
        scheme = "ExternalIdentity" if path.startswith("/api/") else "EdgeIdentity" if path.startswith("/edge/") else "InternalIdentity"
        assert op["security"] == [{scheme: []}]
        assert {
            "authenticated_tenant", "registered_tenant", "deny_unknown_tenant",
            "deny_unknown_tool", "deny_unknown_data_class", "match_nested_tenant",
            "deadline", "registry_unavailable_reject",
        } <= set(op["x-required-enforcement"])
        if path == "/internal/v1/tool-executions":
            assert op["x-required-caller"] == "transaction-service"
        if path == "/internal/v1/model-invocations":
            assert {"deny_sensitive_nonlocal_model", "inherit_highest_data_class", "no_public_fallback_on_local_failure"} <= set(op["x-required-enforcement"])


def test_objects_closed_and_tenant_scopes_explicit(docs):
    objects = {}
    inherited = set()
    actual_globals = set()
    for source, document in docs.schemas.items():
        for pointer, node in nodes(document):
            if node.get("type") != "object":
                continue
            key = (source, pointer)
            objects[key] = node
            assert node["additionalProperties"] is False, (source.name, pointer)
            assert "properties" in node and "required" in node
            assert len(node["required"]) == len(set(node["required"]))
            assert set(node["required"]) <= node["properties"].keys()
            scope = node.get("x-tenant-scope")
            assert scope in {"self", "inherited", "global"}, (source.name, pointer)
            if scope == "self":
                assert "tenant_id" in node["properties"] and "tenant_id" in node["required"]
            elif scope == "global":
                actual_globals.add((source.name, pointer))
                assert node.get("x-global-reason")
            else:
                inherited.add(key)
    assert actual_globals == GLOBAL_OBJECTS

    # 跟随引用验证所有 inherited 辅助对象都位于 tenant 或全局对象的图内。
    reachable = set()
    visited = set()

    def visit(source, node, pointer, owner):
        marker = (source, pointer, owner)
        if marker in visited:
            return
        visited.add(marker)
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            target, value = docs.resolve(source, node["$ref"])
            fragment = unquote(urlsplit(node["$ref"]).fragment)
            visit(target, value, fragment, owner)
        scope = node.get("x-tenant-scope")
        if scope in {"self", "global"}:
            owner = (source, pointer)
        if node.get("type") == "object":
            assert owner is not None, (source.name, pointer)
            reachable.add((source, pointer))
        for key, child in node.get("properties", {}).items():
            visit(source, child, pointer + "/properties/" + pointer_token(key), owner)
        if isinstance(node.get("items"), dict):
            visit(source, node["items"], pointer + "/items", owner)
        for keyword in ("allOf", "anyOf", "oneOf"):
            for index, child in enumerate(node.get(keyword, [])):
                visit(source, child, pointer + f"/{keyword}/{index}", owner)
        for keyword in ("if", "then", "else", "not"):
            if isinstance(node.get(keyword), dict):
                visit(source, node[keyword], pointer + "/" + keyword, owner)

    for (source, pointer), node in objects.items():
        if node["x-tenant-scope"] in {"self", "global"}:
            visit(source, node, pointer, None)
    assert inherited <= reachable, f"无归属辅助对象: {inherited - reachable}"


def test_context_and_registry_denial_are_mandatory(docs):
    source, context = docs.schema("common-context.json")
    assert set(context["required"]) == CONTEXT_FIELDS
    policy = context["x-admission-policy"]
    assert all(policy[k] == "reject" for k in (
        "unknown_tenant", "unknown_tool", "unknown_data_class", "sensitive_nonlocal_model", "missing_registry",
    ))
    assert policy["tenant_source"] == "authenticated_identity"
    assert policy["model_output_is_execution_permission"] is False
    for file, document in docs.schemas.items():
        for pointer, node in nodes(document):
            for field, registry in (("tenant_id", "tenants"), ("tool_id", "tools")):
                prop = node.get("properties", {}).get(field)
                if prop is None:
                    continue
                assert field in node["required"], (file.name, pointer, field)
                assert prop["x-registry"] == registry
                assert prop["x-on-unknown"] == prop["x-on-unavailable"] == "reject"
                assert "default" not in prop
                _, resolved = docs.deref(file, prop)
                assert resolved["type"] == "string" and resolved["minLength"] >= 1
    _, models = docs.schema("api-models.json")
    for name, model in models["$defs"].items():
        if model.get("x-tenant-scope") == "self" and name != "FactSnapshot":
            assert CONTEXT_FIELDS <= set(model["required"]), name


def test_exact_lifecycle_states_and_usage(docs):
    source, machine = docs.schema("state-machines.json")
    assert set(machine["$defs"]) == set(EXPECTED_STATES)
    for name, expected in EXPECTED_STATES.items():
        actual = machine["$defs"][name]
        assert actual["enum"] == expected
        assert actual["x-core-flow"] == EXPECTED_FLOWS[name]
        assert actual["x-source"] == "§12"
    uses = [
        ("task.json", None, "Task"), ("command-status.json", None, "Command"),
        ("api-models.json", "RunResult", "Run"), ("api-models.json", "Saga", "Saga"),
        ("api-models.json", "EdgeRelease", "EdgeRelease"),
    ]
    for file, definition, expected in uses:
        source, schema = docs.schema(file)
        if definition:
            schema = schema["$defs"][definition]
        _, status = docs.deref(source, schema["properties"]["status"])
        assert status["enum"] == EXPECTED_STATES[expected]


def test_data_and_risk_enums_are_closed(docs):
    _, common = docs.schema("common-context.json")
    assert common["$defs"]["DataClass"]["enum"] == ["C0", "C1", "C2", "C3", "C4"]
    assert common["$defs"]["RiskClass"]["enum"] == ["R0", "R1", "R2", "R3"]
    for source, document in docs.schemas.items():
        for pointer, node in nodes(document):
            for field, allowed in (
                ("data_class", {"C0", "C1", "C2", "C3", "C4"}),
                ("risk_class", {"R0", "R1", "R2", "R3"}),
            ):
                if field in node.get("properties", {}):
                    _, prop = docs.deref(source, node["properties"][field])
                    assert prop.get("enum") and set(prop["enum"]) <= allowed, (source.name, pointer)


def test_command_required_fields_and_trusted_hash(docs):
    source, command = docs.schema("command.json")
    # §10.2 完整示例的全部字段，再加公共 schema_version 与可信绑定字段。
    upstream_example = {
        "tenant_id", "factory_id", "actor_id", "employee_id", "task_id", "trace_id",
        "command_type", "object_id", "payload_version", "payload", "idempotency_key",
        "expected_version", "policy_version", "approval_ref", "data_class", "deadline_at",
    }
    assert upstream_example | {"schema_version", "payload_hash", "tool_id", "tool_version", "policy_evaluation_ref"} <= set(command["required"])
    description = command["properties"]["payload_hash"]["description"]
    assert all(text in description for text in ("可信服务", "canonical", "不得信任 Agent", "工具版本", "tenant/actor", "approval"))
    constraints = command["x-execution-constraints"]
    assert constraints["trusted_payload_hash"] and constraints["reauthorize_before_execution"]
    assert constraints["persist_before_accepted"] and constraints["approval_binds_payload"]
    assert constraints["same_key_different_payload_http_status"] == 409
    assert constraints["unknown_query_before_retry"] and constraints["unknown_new_key_resend"] is False
    example = command["examples"][0]
    for field in ("idempotency_key", "expected_version", "payload_version", "payload_hash", "tenant_id", "tool_id"):
        modified = copy.deepcopy(example)
        del modified[field]
        assert docs.instance_errors(modified, source, command), field
    modified = copy.deepcopy(example)
    modified["payload"]["unexpected"] = "value-demo"
    assert docs.instance_errors(modified, source, command)
    modified = copy.deepcopy(example)
    modified["data_class"] = "C5"
    assert docs.instance_errors(modified, source, command)


def test_decision_event_tool_and_checkpoint_fields(docs):
    expected = {
        "decision-result.json": {
            "decision_id", "route", "action", "category", "confidence", "schema_version", "model_version",
            "calibration_version", "rule_version", "reason_code", "evidence_refs", "fallback",
        },
        "event-envelope.json": {
            "event_id", "event_type", "schema_version", "tenant_id", "factory_id", "source_system",
            "aggregate_type", "aggregate_id", "aggregate_version", "occurred_at", "received_at",
            "trace_id", "causation_id", "correlation_id", "data_class", "payload",
        },
        "tool-spec.json": {
            "tool_id", "version", "input_schema", "output_schema", "read_write", "risk_class",
            "required_scopes", "timeout", "retry_policy", "idempotency_support", "status_query",
            "compensation", "data_class_rules", "adapter_owner",
        },
    }
    for file, fields in expected.items():
        _, schema = docs.schema(file)
        assert fields <= set(schema["required"]), file
    source, decision = docs.schema("decision-result.json")
    for name in ("route", "action", "category", "reason_code"):
        _, field = docs.deref(source, decision["properties"][name])
        assert field["x-catalog-required"] is True and field["type"] == "string"
    assert decision["x-decision-authority"] == "suggestion_only"
    _, event = docs.schema("event-envelope.json")
    assert set(event["properties"]["event_type"]["enum"]) == EXPECTED_EVENTS
    assert event["x-delivery"]["outbox"] and event["x-delivery"]["inbox"]
    assert event["x-delivery"]["late_event_overwrite"] is False
    _, tool = docs.schema("tool-spec.json")
    for capability in ("idempotency_support", "status_query", "compensation"):
        assert tool["properties"][capability]["type"] == "boolean"
        assert "default" not in tool["properties"][capability]
    _, models = docs.schema("api-models.json")
    assert {
        "run_id", "version", "program_position", "context_refs", "pending_command_ids",
        "completed_tool_receipt_refs", "model_version", "policy_version",
    } <= set(models["$defs"]["Checkpoint"]["required"])


def test_sensitive_nonlocal_backends_and_shadow_writes_rejected(docs):
    source, document = docs.schema("api-models.json")
    model = document["$defs"]["ModelInvocationRequest"]
    example = model["examples"][0]
    assert not docs.instance_errors(example, source, model)
    for level in ("C3", "C4"):
        for backend in ("enterprise_cloud", "public", "unknown"):
            modified = copy.deepcopy(example)
            modified.update(data_class=level, model_backend=backend)
            assert docs.instance_errors(modified, source, model), (level, backend)
    for field in ("tenant_id", "data_class", "model_backend"):
        modified = copy.deepcopy(example)
        del modified[field]
        assert docs.instance_errors(modified, source, model), field
    modified = copy.deepcopy(example)
    modified["data_class"] = "UNKNOWN"
    assert docs.instance_errors(modified, source, model)
    edge = document["$defs"]["EdgeDecisionResult"]
    modified = copy.deepcopy(edge["examples"][0])
    modified["action_executed"] = True
    assert docs.instance_errors(modified, source, edge)
    modified = copy.deepcopy(edge["examples"][0])
    modified["receipt_refs"] = ["receipt-demo"]
    assert docs.instance_errors(modified, source, edge)
    decision_source, decision = docs.schema("decision-result.json")
    modified = copy.deepcopy(decision["examples"][0])
    modified["confidence"] = 0.99
    assert docs.instance_errors(modified, decision_source, decision)


def test_examples_are_structurally_valid_and_objects_reject_extension(docs):
    count = 0
    for source, document in docs.schemas.items():
        for pointer, schema in nodes(document):
            for example in schema.get("examples", []):
                assert not docs.instance_errors(example, source, schema), (source.name, pointer, docs.instance_errors(example, source, schema))
                count += 1
                if isinstance(example, dict) and schema.get("type") == "object":
                    modified = copy.deepcopy(example)
                    modified["unexpected_contract_field"] = True
                    assert docs.instance_errors(modified, source, schema), (source.name, pointer)
    assert count >= 8


def test_service_manifest_all_eight_owners_and_health(docs):
    source, manifest = docs.schema("service-manifest.json")
    examples = manifest["examples"]
    assert len({e["identity_ref"] for e in examples}) == 8
    assert all(e["required_dependencies"] for e in examples)
    assert {e["service_id"]: e["owned_schema"] for e in examples} == EXPECTED_OWNERS
    for example in examples:
        assert example["independent_process"] is True
        assert example["migration_directory"] == "migrations/" + example["owned_schema"] + "/"
        assert example["environment"] == "mock"
        assert example["ready_includes_dependencies"] and example["ready_includes_policy_version"] and example["ready_includes_backlog"]
        modified = copy.deepcopy(example)
        modified["owned_schema"] = next(owner for owner in EXPECTED_OWNERS.values() if owner != example["owned_schema"])
        assert docs.instance_errors(modified, source, manifest)
        modified = copy.deepcopy(example)
        modified["environment"] = "production"
        assert docs.instance_errors(modified, source, manifest), "生产 SLO 不可为空"
        for field in ("cross_owner_database_write", "cross_owner_join", "cross_owner_foreign_key"):
            modified = copy.deepcopy(example)
            modified[field] = True
            assert docs.instance_errors(modified, source, manifest), field
    _, document = docs.schema("api-models.json")
    health = document["$defs"]["Health"]
    assert {"dependencies", "policy_version", "backlog"} <= set(health["required"])


def test_utc_and_decimal_contracts(docs):
    source, common = docs.schema("common-context.json")
    timestamp = common["$defs"]["UtcTimestamp"]
    assert not docs.instance_errors("2026-10-05T00:00:00Z", source, timestamp)
    for invalid in ("2026-10-05T08:00:00+08:00", "2026-10-05T00:00:00", "2026-02-30T00:00:00Z"):
        assert docs.instance_errors(invalid, source, timestamp), invalid
    decimal = common["$defs"]["Decimal"]
    assert decimal["type"] == "string" and decimal["pattern"]
    assert not docs.instance_errors("123.4500", source, decimal)
    for invalid in (123.45, "1e3", "NaN", "1,000.00"):
        assert docs.instance_errors(invalid, source, decimal), invalid
    # 此包不含未冻结的业务数量/金额字段；序号是 integer，confidence 是无量纲 number。
    for file, document in docs.schemas.items():
        for pointer, schema in nodes(document):
            for field, prop in schema.get("properties", {}).items():
                if field in {"quantity", "amount", "price", "cost", "money"}:
                    _, resolved = docs.deref(file, prop)
                    assert resolved["type"] == "string" and "pattern" in resolved, (file.name, pointer, field)


def test_accepted_does_not_claim_success_and_error_semantics_stay_unfrozen(docs):
    inventory = {(method, path): op for _, path, method, op in operations(docs)}
    for path in ("/api/v1/tasks", "/internal/v1/commands"):
        response = inventory[("POST", path)]["responses"]["202"]
        assert "不表示" in response["description"]
    tool = inventory[("POST", "/internal/v1/tool-requests")]
    assert tool["x-read-write-responses"] == {"read": 200, "write": 202}
    _, error = docs.schema("error.json")
    assert "待冻结" in error["properties"]["code"]["description"]
    assert "enum" not in error["properties"]["code"], "不得编造上位文档没有的业务错误码"
    for _, api in docs.apis.items():
        assert "待冻结" in api["components"]["responses"]["Error404"]["description"]


def test_health_readiness_dependency_failure_is_not_ready(docs):
    source, document = docs.schema("api-models.json")
    health = document["$defs"]["Health"]
    modified = copy.deepcopy(health["examples"][0])
    modified["dependencies"] = [{"dependency_id": "dependency-demo", "ready": False}]
    assert docs.instance_errors(modified, source, health)
    modified["ready"] = False
    assert not docs.instance_errors(modified, source, health)
    modified = copy.deepcopy(health["examples"][0])
    modified["policy_version"] = None
    assert docs.instance_errors(modified, source, health)


def test_gate_checker_rejects_reference_escape(docs):
    source, _ = docs.schema("command.json")
    # 只针对读取范围内的缓存解析；即使引用失败也不读取范围外文件。
    for reference in ("../outside.json", "../../outside.json", "/outside.json", "#/$defs/Missing"):
        with pytest.raises(AssertionError):
            docs.resolve(source, reference)
