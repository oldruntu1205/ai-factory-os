"""P1 静态门禁：纯标准库 + pytest，只读取 packages/contracts。
不导入 services，不联网/连接 DB，不执行运行验收。
有限实例检查器仅覆盖本包实际使用的关键字，不是通用规范实现。
ACL、注册表、引用归属、时效与真实内容扫描检查声明，运行效果由 L3 验收。
"""
import copy
import datetime as dt
import hashlib
import itertools
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest

from .contract_inventory import files_of, missing, unregistered

CONTRACT = Path(__file__).resolve().parents[2] / "packages" / "contracts"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
CLASSES = ("C0", "C1", "C2", "C3", "C4")
# 开工时磁盘原始字节的固定哈希，不在运行时生成期望值。
P0_SHA256 = {
    "openapi/p0.openapi.json": "54adb747a58653c24bfafefcc41e953e99f53191d9f86a001d5840b31f4599f7",
    "schemas/api-models.json": "c9e8ddcde87db8cc91c8779f304aa018fd5b4d53308822caeaee5baf3ed81b98",
    "schemas/command-status.json": "cf095bcd0d4298252a3a60c334a49854d6fa93bbd29ba4197844983a5004f830",
    "schemas/command.json": "f05a98db6030b0003eb3207ca3a57ca46c40d605c06e0f76437e7f44b6ec9840",
    "schemas/common-context.json": "e6914b5b9db71c0f97bc9b88bed0243ed733102679378146d31987be44847061",
    "schemas/decision-result.json": "86a7155cece330eb2da5c50a56a5a5621be7961214b5ed143c65af6db909d12f",
    "schemas/error.json": "8baef3c7db6a63c79fd71fee0694a7b76bac5cf06bb26c620299254591fb781e",
    "schemas/event-envelope.json": "04a6b5639606d269499dc74070b8e7fe790a22f02090a3c3a28d9116dca4db9b",
    "schemas/service-manifest.json": "f406573fdbc6706f661f8ee1c385e0226387a737812687b85217fe27d2522e84",
    "schemas/state-machines.json": "1cc989f9f6119e437678ee14a7d293f9e3183f7b96d49548ae7910641359f273",
    "schemas/task.json": "6e6fa29630e82a6e5c8efedb4288238b03f695e395c0c69e82a8a1eb1f0acd36",
    "schemas/tool-spec.json": "ab130721f780ea1781aa4159332f9db0ccfba55183efddf6e90ff36663718f5b"
}
P1_FILES = {
    "openapi/p1.openapi.json", "schemas/p1-classification.json",
    "schemas/p1-governance.json", "schemas/p1-egress-audit.json",
    "schemas/p1-token-vault.json", "schemas/p1-knowledge-source.json",
    "schemas/p1-readonly.json",
}
EXPECTED = {
    ("POST", "/internal/v1/classifications/evaluate"): "security",
    ("POST", "/internal/v1/egress/evaluate"): "security",
    ("POST", "/internal/v1/token-vault/tokenize"): "security",
    ("POST", "/internal/v1/token-vault/restore"): "security",
    ("POST", "/internal/v1/knowledge/query"): "manufacturing",
    ("GET", "/api/v1/tasks/{task_id}/knowledge-answer"): "control",
}
STAGES = (
    ("identity_tenant", "身份与租户校验"), ("access_scope", "工具/检索访问范围"),
    ("classification", "输入及派生上下文分类"), ("minimization", "最小化与脱敏"),
    ("dlp_egress", "DLP/出域策略"), ("model_allowlist", "模型白名单"),
    ("invocation", "调用"), ("output_scan", "输出Schema/泄露扫描"),
    ("local_token_restore", "本地授权的 Token 恢复"),
    ("consumption", "用户/工具消费"), ("graded_audit", "分级审计"),
)
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, f"重复 JSON 键: {key}"
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"非法 JSON 常量: {value}")


def walk(value):
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key not in {"example", "examples"} and not key.startswith("x-"):
                yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class Documents:
    def __init__(self):
        self.files = {}
        for relative in sorted(files_of("p0") | files_of("p1")):
            candidate = CONTRACT / relative
            real = candidate.resolve()
            assert real.is_relative_to(candidate.parent.resolve())
            self.files[real] = json.loads(
                candidate.read_text(encoding="utf-8-sig"),
                object_pairs_hook=unique_pairs, parse_constant=reject_constant,
            )

    def schema(self, name):
        source = (CONTRACT / "schemas" / name).resolve()
        return source, self.files[source]

    def api(self):
        return self.files[(CONTRACT / "openapi/p1.openapi.json").resolve()]

    def resolve(self, source, reference):
        parsed = urlsplit(reference)
        assert not parsed.scheme and not parsed.netloc and not parsed.query
        relative = unquote(parsed.path)
        assert not relative.startswith("/") and ":" not in relative and "\\" not in relative
        target = (source.parent / relative).resolve() if relative else source
        assert target in self.files, f"悬空或越界引用: {reference}"
        node = self.files[target]
        fragment = unquote(parsed.fragment)
        assert not fragment or fragment.startswith("/")
        for token in fragment.split("/")[1:] if fragment else ():
            assert not re.search(r"~(?![01])", token)
            token = token.replace("~1", "/").replace("~0", "~")
            node = node[token] if isinstance(node, dict) else node[int(token)]
        return target, node

    def deref(self, source, node):
        seen = set()
        while isinstance(node, dict) and "$ref" in node:
            key = (source, node["$ref"])
            assert key not in seen
            seen.add(key)
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            source, node = self.resolve(source, node["$ref"])
            node = {**node, **siblings}
        return source, node

    def errors(self, value, source, schema):
        if isinstance(schema, bool):
            return [] if schema else ["false schema"]
        supported = {
            "$schema", "$ref", "$defs", "title", "description", "type", "properties",
            "required", "additionalProperties", "items", "contains", "minContains",
            "maxContains", "minItems", "maxItems", "uniqueItems", "enum", "const",
            "allOf", "anyOf", "oneOf", "if", "then", "else", "not", "pattern",
            "minLength", "maxLength", "format", "minimum", "maximum", "examples",
        }
        assert not {k for k in schema if k not in supported and not k.startswith("x-")}
        errors = []
        if "$ref" in schema:
            target, node = self.resolve(source, schema["$ref"])
            errors += self.errors(value, target, node)
        for child in schema.get("allOf", []):
            errors += self.errors(value, source, child)
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                matches = sum(not self.errors(value, source, child) for child in schema[keyword])
                if matches == 0 or (keyword == "oneOf" and matches != 1):
                    errors.append(keyword)
        if "if" in schema:
            branch = "then" if not self.errors(value, source, schema["if"]) else "else"
            errors += self.errors(value, source, schema.get(branch, True))
        if "not" in schema and not self.errors(value, source, schema["not"]):
            errors.append("not")
        if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
            errors.append("const")
        if "enum" in schema and value not in schema["enum"]:
            errors.append("enum")
        types = schema.get("type", [])
        types = [types] if isinstance(types, str) else types
        checks = {
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": type(value) is bool,
            "integer": type(value) is int, "number": type(value) in (int, float),
            "null": value is None,
        }
        if types and not any(checks[t] for t in types):
            return errors + ["type"]
        if isinstance(value, dict):
            errors += ["required:" + k for k in schema.get("required", []) if k not in value]
            props = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                errors += ["additional:" + k for k in value.keys() - props.keys()]
            for key in value.keys() & props.keys():
                errors += self.errors(value[key], source, props[key])
        if isinstance(value, list):
            if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
                errors.append("items length")
            if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
                errors.append("uniqueItems")
            for item in value:
                errors += self.errors(item, source, schema.get("items", True))
            if "contains" in schema:
                count = sum(not self.errors(item, source, schema["contains"]) for item in value)
                if not schema.get("minContains", 1) <= count <= schema.get("maxContains", float("inf")):
                    errors.append("contains")
        if isinstance(value, str):
            if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
                errors.append("string length")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append("pattern")
            if schema.get("format") == "date-time":
                try:
                    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    assert value.endswith("Z") and stamp.utcoffset() == dt.timedelta(0)
                except (ValueError, AssertionError):
                    errors.append("date-time")
        if type(value) in (int, float):
            if not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
                errors.append("range")
        return errors


@pytest.fixture(scope="module")
def docs():
    return Documents()


def context(level="C0"):
    return dict(tenant_id="tenant-demo", factory_id="factory-demo", actor_id="user-demo",
                employee_id="employee-planning", task_id="task-demo", trace_id="trace-demo",
                data_class=level, schema_version="1.0.0", deadline_at="2026-10-05T00:00:00Z")


def classification(levels=("C0",), output=None, anonymized=False):
    highest = max(levels, key=CLASSES.index)
    return {
        "tenant_id": "tenant-demo", "context": context(highest),
        "inputs": [dict(content_ref="task-demo", data_class=c, origin="rag_chunk",
                        anonymized=anonymized, transformation_rule_ref=None, review_ref=None)
                   for c in levels],
        "data_class": highest, "output_data_class": output or highest,
        "classification_rule_version": "1.0.0",
    }


def decision(level="C0", backend="local", permitted=True):
    return dict(
        tenant_id="tenant-demo", context=context(level), classification=classification((level,)),
        model_id="employee-planning", model_version="1.0.0", model_backend=backend,
        purpose="employee-planning", policy_version="1.0.0", identity_verified=True,
        access_scope_verified=True, minimized=True, dlp_passed=True, model_allowlisted=True,
        route_policy_permits=True, backend_healthy=True, c2_transform_approved=True,
        c4_task_allowlisted=True, c4_approval_verified=True, contains_key_material=False,
        permit_invocation=permitted, public_fallback=False, local_backend_failed=False,
    )


def acl():
    return dict(acl_ref="user-demo", acl_version="1.0.0", pre_retrieval_tenant_filter=True,
                pre_retrieval_factory_filter=True, pre_retrieval_role_filter=True,
                post_return_acl_recheck=True)


def source(level="C0"):
    return dict(
        tenant_id="tenant-demo", factory_id="factory-demo", task_id="task-demo",
        trace_id="trace-demo", source_system="factory-demo", source_id="task-demo",
        source_version="1.0.0", occurred_at="2026-10-04T00:00:00Z",
        received_at="2026-10-04T00:00:00Z", data_class=level,
        quality_status="employee-planning", citation_ref="task-demo", snapshot_ref="task-demo",
        valid_until="2026-10-05T00:00:00Z", acl_check=acl(),
    )


def audit(level="C0", backend="local", called=True):
    return dict(
        tenant_id="tenant-demo", factory_id="factory-demo", actor_id="user-demo",
        task_id="task-demo", trace_id="trace-demo", audit_ref="trace-demo",
        occurred_at="2026-10-04T00:00:00Z", classification_known=level is not None,
        highest_data_class=level, policy_version="1.0.0", classification_rule_version="1.0.0",
        model_version="1.0.0", requested_backend=backend, invocation_performed=called,
        invocation_succeeded=called, output_released=called, evidence_refs=["trace-demo"],
        input_hash="0" * 64, output_hash=None, stage_refs=[], summary_ref=None,
    )


def test_p0_original_bytes_are_locked():
    assert len(P0_SHA256) == 12
    for relative, expected in P0_SHA256.items():
        candidate = (CONTRACT / relative).resolve()
        assert candidate.is_relative_to(CONTRACT.resolve())
        assert hashlib.sha256(candidate.read_bytes()).hexdigest() == expected, relative
    # 每条契约都必须登记在阶段清单里（唯一登记处：tests/contracts/contract_inventory.py）。
    # 原先这里是 `existing - P1_FILES == set(P0_SHA256)`，新增 P1.5 契约时会误报；
    # 判据本身没变——仍然"不允许出现未登记的契约文件"，只是把它交给清单统一裁决。
    assert unregistered() == set(), f"存在未经阶段登记的契约文件: {sorted(unregistered())}"
    assert missing() == set(), f"清单登记了但磁盘缺失的契约文件: {sorted(missing())}"
    assert files_of("p0") == set(P0_SHA256), "P0 文件清单与哈希锁定集合不一致"
    assert files_of("p1") == P1_FILES, "P1 文件清单与门禁覆盖集合不一致"


def test_p1_parse_dialects_and_local_references(docs):
    for relative in P1_FILES:
        file = (CONTRACT / relative).resolve()
        document = docs.files[file]
        assert isinstance(document, dict)
        if relative.startswith("schemas/"):
            assert document["$schema"] == DIALECT
        for node in walk(document):
            if "$ref" in node:
                _, target = docs.resolve(file, node["$ref"])
                assert isinstance(target, (dict, bool))
    api = docs.api()
    assert api["openapi"] == "3.1.0" and api["jsonSchemaDialect"] == DIALECT
    assert api["x-source-version"] == api["info"]["version"] == "3.0.0"


def test_new_interfaces_no_p0_overlap_and_unified_errors(docs):
    api = docs.api()
    api_file = (CONTRACT / "openapi/p1.openapi.json").resolve()
    p0 = docs.files[(CONTRACT / "openapi/p0.openapi.json").resolve()]
    actual, ids = {}, set()
    for path, item in api["paths"].items():
        assert path.startswith(("/api/v1/", "/internal/v1/")) and path not in p0["paths"]
        for method, op in item.items():
            if method not in METHODS:
                continue
            actual[(method.upper(), path)] = op["x-owner"]
            assert op["operationId"] not in ids
            ids.add(op["operationId"])
            assert op["x-interface-layer"] in {"L0", "L1", "L2", "L3", "L4", "L5"}
            identity = "ExternalIdentity" if path.startswith("/api/") else "InternalIdentity"
            assert op["security"] == [{identity: []}]
            assert {"400", "401", "403", "422", "429", "503"} <= op["responses"].keys()
            for status, response in op["responses"].items():
                response_file, response = docs.deref(api_file, response)
                schema = response["content"]["application/json"]["schema"]
                target_file, target = docs.deref(response_file, schema)
                if status.startswith("4") or status == "503":
                    assert target_file.name == "error.json"
                    assert target["required"] == ["code", "message", "trace_id", "retryable", "details"]
            if method == "post":
                assert op["requestBody"]["required"] is True
                docs.resolve(api_file, op["requestBody"]["content"]["application/json"]["schema"]["$ref"])
            else:
                params = [docs.deref(api_file, p)[1] for p in op["parameters"]]
                assert {"X-Context", "task_id"} == {p["name"] for p in params}
                assert all(p["required"] for p in params)
            policy = op["x-admission-policy"]
            for key in ("unknown_tenant", "unknown_tool", "unknown_data_class", "unknown_model",
                        "missing_registry", "sensitive_nonlocal_model"):
                assert policy[key] == "reject"
            assert policy["local_failure"] == "reject_or_queue"
            assert policy["local_failure_public_fallback"] is False
    assert actual == EXPECTED
    _, error = docs.schema("error.json")
    assert "待冻结" in error["properties"]["code"]["description"]
    assert "enum" not in error["properties"]["code"]


@pytest.mark.parametrize("levels", itertools.product(CLASSES, repeat=3))
def test_all_merge_permutations_choose_maximum(docs, levels):
    file, schema = docs.schema("p1-classification.json")
    good = classification(levels)
    assert not docs.errors(good, file, schema)
    for other in CLASSES:
        if other == good["data_class"]:
            continue
        bad = copy.deepcopy(good)
        bad["data_class"] = bad["context"]["data_class"] = bad["output_data_class"] = other
        assert docs.errors(bad, file, schema), (levels, other)


@pytest.mark.parametrize("level,output", itertools.product(CLASSES, repeat=2))
def test_output_inheritance_and_anonymization(docs, level, output):
    file, schema = docs.schema("p1-classification.json")
    data = classification(("C0", level), output=output, anonymized=True)
    assert bool(docs.errors(data, file, schema)) == (CLASSES.index(output) < CLASSES.index(level))
    rules = schema["x-classification-rules"]
    assert rules["merge"] == "maximum" and rules["output"] == "inherit_maximum_or_raise"
    assert rules["anonymization_auto_downgrade"] is False


@pytest.mark.parametrize("unknown", ["UNKNOWN", "C5", "", None, 3])
def test_unknown_missing_empty_classification_rejected(docs, unknown):
    file, schema = docs.schema("p1-classification.json")
    data = classification()
    data["inputs"][0]["data_class"] = unknown
    assert docs.errors(data, file, schema)
    for field in ("data_class", "output_data_class", "inputs"):
        bad = classification()
        del bad[field]
        assert docs.errors(bad, file, schema)
    bad = classification()
    bad["inputs"] = []
    assert docs.errors(bad, file, schema)


@pytest.mark.parametrize("level,backend", itertools.product(CLASSES, ("local", "enterprise_cloud", "public")))
def test_egress_backend_matrix(docs, level, backend):
    file, schema = docs.schema("p1-governance.json")
    permitted = not ((level in ("C3", "C4") and backend != "local") or
                     (level == "C2" and backend == "public"))
    data = decision(level, backend, permitted)
    assert not docs.errors(data, file, schema)
    if not permitted:
        data["permit_invocation"] = True
        assert docs.errors(data, file, schema)


@pytest.mark.parametrize("field", [
    "identity_verified", "access_scope_verified", "minimized", "dlp_passed",
    "model_allowlisted", "route_policy_permits", "backend_healthy",
])
def test_failed_check_cannot_permit(docs, field):
    file, schema = docs.schema("p1-governance.json")
    data = decision("C3")
    data[field] = False
    assert docs.errors(data, file, schema)
    data["permit_invocation"] = False
    assert not docs.errors(data, file, schema)


def test_special_guards_unknown_backend_and_no_fallback(docs):
    file, schema = docs.schema("p1-governance.json")
    for level, backend, field in (
        ("C2", "enterprise_cloud", "c2_transform_approved"),
        ("C4", "local", "c4_task_allowlisted"), ("C4", "local", "c4_approval_verified"),
    ):
        data = decision(level, backend)
        data[field] = False
        assert docs.errors(data, file, schema)
        data["permit_invocation"] = False
        assert not docs.errors(data, file, schema)
    for backend in ("local", "enterprise_cloud", "public"):
        data = decision("C0", backend, False)
        data["backend_healthy"] = False
        data["public_fallback"] = True
        assert docs.errors(data, file, schema)
    for backend in ("local", "enterprise_cloud", "public"):
        data = decision("C3", backend)
        data["local_backend_failed"] = True
        assert docs.errors(data, file, schema)
        data["permit_invocation"] = False
        assert not docs.errors(data, file, schema)
    data = decision()
    data["contains_key_material"] = True
    assert docs.errors(data, file, schema)
    data = decision()
    data["model_backend"] = "UNKNOWN"
    assert docs.errors(data, file, schema)
    data = decision("C3", "public", False)
    data["context"]["data_class"] = "C0"
    assert docs.errors(data, file, schema)


def test_governance_original_eleven_steps(docs):
    _, schema = docs.schema("p1-governance.json")
    expected = [dict(order=i + 1, id=stage, label=label) for i, (stage, label) in enumerate(STAGES)]
    assert schema["x-governance-order"] == docs.api()["x-governance-order"] == expected
    assert {"output_schema_before_restore", "output_leak_scan_before_restore",
            "restore_requires_local_authorization", "restored_output_consumer_acl",
            "consume_only_after_scan", "append_graded_audit"} <= set(schema["x-required-enforcement"])


def test_audit_rejects_sensitive_calls_unknown_class_and_raw_content(docs):
    file, schema = docs.schema("p1-egress-audit.json")
    for level, backend in itertools.product(("C3", "C4"), ("enterprise_cloud", "public")):
        assert not docs.errors(audit(level, backend, False), file, schema)
        assert docs.errors(audit(level, backend, True), file, schema)
    assert not docs.errors(audit(None, "local", False), file, schema)
    assert docs.errors(audit(None, "local", True), file, schema)
    for field in ("prompt", "raw_output", "token", "original_value", "message", "payload"):
        data = {**audit(), field: "task-demo"}
        assert docs.errors(data, file, schema)
    policy = schema["x-audit-policy"]
    assert policy["append_only"] is True
    assert all(policy[k] is False for k in ("raw_content", "token_values", "restored_values"))


def test_vault_local_scope_restore_authorization_and_no_mapping_api(docs):
    file, schema = docs.schema("p1-token-vault.json")
    mapping = schema["$defs"]["Mapping"]
    assert {"tenant_id", "purpose", "expires_at", "entries"} <= set(mapping["required"])
    policy = mapping["x-storage-policy"]
    assert policy["domain"] == "approved_local" and policy["isolation"] == ["tenant", "purpose", "expires_at"]
    assert all(policy[k] is False for k in ("export", "log", "audit", "evidence"))
    assert {"local_only", "tenant_match", "purpose_match", "expiry_checked_at_restore",
            "output_schema_before_restore", "output_leak_scan_before_restore",
            "consumer_acl_at_restore_and_consume", "tokenization_no_auto_downgrade"} <= set(schema["x-required-enforcement"])
    restore = schema["$defs"]["RestoreResult"]
    data = dict(tenant_id="tenant-demo", context=context(), mapping_ref="task-demo",
                purpose="employee-planning", consumer_ref="user-demo", restored_ref="task-demo",
                vault_version="1.0.0", output_schema_passed=True, output_leak_scan_passed=True,
                local_authorization_verified=True, consumer_acl_verified=True)
    assert not docs.errors(data, file, restore)
    for field in ("output_schema_passed", "output_leak_scan_passed",
                  "local_authorization_verified", "consumer_acl_verified"):
        assert docs.errors({**data, field: False}, file, restore)
    for item in docs.api()["components"]["schemas"].values():
        assert not item.get("$ref", "").endswith("#/$defs/Mapping")
    assert "entries" not in schema["properties"] and "original_ref" not in restore["properties"]


def test_source_traceability_required_and_acl_failures_blocked(docs):
    file, schema = docs.schema("p1-knowledge-source.json")
    fields = {"source_system", "source_id", "source_version", "occurred_at", "data_class",
              "tenant_id", "factory_id", "task_id", "trace_id", "acl_check"}
    assert fields <= set(schema["required"])
    policy = schema["x-retrieval-policy"]
    assert policy["filter_before_retrieval"] == ["tenant", "factory", "role_acl"]
    assert policy["recheck_after_return"] is True and policy["llm_visibility_authority"] is False
    assert policy["knowledge_memory_implicit_copy"] is False and policy["reclassify_each_return"]
    data = source("C3")
    assert not docs.errors(data, file, schema)
    for field in fields:
        bad = copy.deepcopy(data)
        del bad[field]
        assert docs.errors(bad, file, schema)
    for field in ("pre_retrieval_tenant_filter", "pre_retrieval_factory_filter",
                  "pre_retrieval_role_filter", "post_return_acl_recheck"):
        bad = copy.deepcopy(data)
        bad["acl_check"][field] = False
        assert docs.errors(bad, file, schema)


def test_answer_scans_sources_and_output_escalation(docs):
    file, document = docs.schema("p1-readonly.json")
    schema = document["$defs"]["Answer"]
    data = dict(tenant_id="tenant-demo", context=context("C3"), answer_ref="task-demo",
                sources=[source("C3")], classification=classification(("C3",)),
                model_version="1.0.0", policy_version="1.0.0", output_schema_passed=True,
                output_leak_scan_passed=True, consumer_acl_verified=True, audit_refs=["trace-demo"])
    assert not docs.errors(data, file, schema)
    for field in ("output_schema_passed", "output_leak_scan_passed", "consumer_acl_verified"):
        bad = copy.deepcopy(data)
        bad[field] = False
        assert docs.errors(bad, file, schema)
    bad = copy.deepcopy(data)
    bad["sources"] = []
    assert docs.errors(bad, file, schema)
    bad = copy.deepcopy(data)
    bad["classification"] = classification(("C0",))
    bad["context"] = context("C0")
    assert docs.errors(bad, file, schema)
    high = copy.deepcopy(data)
    high["classification"]["output_data_class"] = "C4"
    assert docs.errors(high, file, schema)
    high["context"]["data_class"] = "C4"
    assert not docs.errors(high, file, schema)
    assert {"source_tenant_factory_task_trace_match", "source_complete_in_classification",
            "source_reference_acl_and_version_verified", "sensitive_answer_same_domain_only"} <= set(schema["x-required-enforcement"])
    policy = document["x-readonly-policy"]
    assert policy["business_side_effects"] is False and policy["arbitrary_sql"] is False
    assert policy["arbitrary_url"] is False and policy["source_tenant_task_trace_match"] is True


def test_closed_objects_and_tenant_registry(docs):
    for relative in P1_FILES:
        if not relative.startswith("schemas/"):
            continue
        file = (CONTRACT / relative).resolve()
        for node in walk(docs.files[file]):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) <= node["properties"].keys()
                if node["x-tenant-scope"] == "self":
                    prop = node["properties"]["tenant_id"]
                    assert prop["x-registry"] == "tenants"
                    assert prop["x-on-unknown"] == prop["x-on-unavailable"] == "reject"
            for field, prop in node.get("properties", {}).items():
                if field in {"quantity", "amount", "price", "cost", "money"}:
                    _, resolved = docs.deref(file, prop)
                    assert resolved["type"] == "string" and resolved["pattern"]


def test_utc_decimal_and_resolver_boundary(docs):
    file, common = docs.schema("common-context.json")
    utc = common["$defs"]["UtcTimestamp"]
    assert not docs.errors("2026-10-04T00:00:00Z", file, utc)
    for value in ("2026-10-04T08:00:00+08:00", "2026-10-04T00:00:00", "2026-02-30T00:00:00Z"):
        assert docs.errors(value, file, utc)
    decimal = common["$defs"]["Decimal"]
    assert not docs.errors("123.4500", file, decimal)
    for value in (123.45, "1e3", "NaN"):
        assert docs.errors(value, file, decimal)
    file, _ = docs.schema("p1-classification.json")
    for ref in ("../../soul.md", "/outside.json", "file:///outside.json",
                "C:/outside.json", "../outside.json", "#/$defs/Missing"):
        with pytest.raises((AssertionError, KeyError)):
            docs.resolve(file, ref)
