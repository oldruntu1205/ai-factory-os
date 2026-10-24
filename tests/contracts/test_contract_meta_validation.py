"""契约自身的元模式校验（L2 补充门禁，2026-10-04）。

**为什么需要它**：P0/P1 的静态门禁都是"按已知模式逐条断言"，属于白名单式检查。
2026-10-04 在 P1 契约上实测到一类它能漏掉的真实缺陷：

    {"$ref": "common-context.json", "properties": {...}}

JSON Schema 2020-12 规定：与 `$ref` **同级**的关键字一律被忽略。于是同级的
`properties` 完全无效，被引用的 `additionalProperties: false` 生效，
该节点**没有任何实例可满足**——而 P1 门禁 188 项全绿也没发现（因为它只检查
"引用的文件存在、路径在目录内"，不检查组合是否可满足）。

本文件用**元模式**（`$schema` 声明的那个 schema）校验每个契约文件，
再由元模式直接拒绝 `$ref` 与其它关键字同级——这正是元模式存在的意义。

依赖：jsonschema（已在 packages/contracts/requirements-dev.txt 中）。
"""

from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACTS = pathlib.Path(__file__).resolve().parents[2] / "packages" / "contracts"
SCHEMA_DIR = CONTRACTS / "schemas"
OPENAPI_DIR = CONTRACTS / "openapi"
DIALECT = "https://json-schema.org/draft/2020-12/schema"

ALLOWED_SIBLINGS_OF_REF = {
    # 纯注释 / 展示 / 契约扩展关键字：不改变校验语义，允许与 $ref 同级
    "description",
    "title",
    "summary",
    "deprecated",
    "readOnly",
    "writeOnly",
    "examples",
    "example",
    "default",
    "externalDocs",
    "$comment",
    "$anchor",
    "$id",
    "$schema",
    "$defs",
    "definitions",
    # P0/P1 一致使用的契约扩展（机器可读语义，由 L2 实现执行，不参与 JSON Schema 校验）
    "x-registry",
    "x-on-unknown",
    "x-on-unavailable",
    "x-tenant-scope",
    "x-global-reason",
    "x-source",
    "x-required-enforcement",
    "x-admission-policy",
    "x-classification-rules",
    "x-governance-order",
    "x-audit-policy",
    "x-decision-authority",
    "x-policy-authority",
    "x-edge-constraints",
    "x-delivery",
    "x-execution-constraints",
    "x-schema-container",
    "x-source-version",
    "x-rule",
    "x-catalog-required",
}


def contract_files() -> list[pathlib.Path]:
    files = sorted(SCHEMA_DIR.glob("*.json"))
    files += sorted(OPENAPI_DIR.glob("*.json"))
    assert files, "没有找到契约文件"
    return files


def _registry() -> Registry:
    registry = Registry()
    for path in SCHEMA_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(
            path.name, Resource.from_contents(document, default_specification=DRAFT202012)
        )
    return registry


@pytest.mark.parametrize("path", contract_files(), ids=lambda p: p.name)
def test_contract_is_valid_against_its_declared_meta_schema(path):
    """每个契约文件必须能被它自己声明的元模式接受。

    注意：OpenAPI 文档不是 JSON Schema，跳过元模式（由 OpenAPI 3.1 语义约束）。
    """
    if path.parent == OPENAPI_DIR:
        pytest.skip("OpenAPI 文档不是 JSON Schema，另由 P0/P1 门禁按 OpenAPI 语义检查")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document.get("$schema") == DIALECT, f"{path.name} 未声明 draft 2020-12"
    validator = Draft202012Validator(
        {"$schema": DIALECT, "$ref": "https://json-schema.org/draft/2020-12/schema"},
    )
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    assert not errors, f"{path.name} 不是合法 JSON Schema: " + "; ".join(
        f"{'/'.join(str(part) for part in error.path)}: {error.message}" for error in errors[:5]
    )


@pytest.mark.parametrize("path", contract_files(), ids=lambda p: p.name)
def test_ref_has_no_semantic_siblings(path):
    """`$ref` 不得与非注释关键字同级：同级关键字会被 JSON Schema 静默忽略。

    这条检查与元模式校验互补——元模式会发现"关键字被忽略"，这里直接指出原因，
    错误信息对修复更有用。
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    def walk(node, pointer: str) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                siblings = set(node) - ALLOWED_SIBLINGS_OF_REF - {"$ref"}
                if siblings:
                    offenders.append(f"{pointer} 与 $ref 同级: {sorted(siblings)}")
            for key, value in node.items():
                walk(value, f"{pointer}/{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{pointer}/{index}")

    walk(document, path.name)
    assert not offenders, "存在会被忽略的同级关键字: " + "; ".join(offenders[:5])


def test_schema_references_resolve_locally_only():
    """所有 $ref 必须能在本目录内解析，且不得指向外网。"""
    registry = _registry()
    for path in SCHEMA_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))

        def walk(node, pointer: str) -> None:
            if isinstance(node, dict):
                reference = node.get("$ref")
                if reference:
                    assert not reference.startswith(("http://", "https://")), f"{path.name}{pointer} 外部引用"
                    target = reference.partition("#")[0]
                    if target:
                        assert (SCHEMA_DIR / target).is_file(), f"{path.name}{pointer} 悬空引用 {reference}"
                for key, value in node.items():
                    walk(value, f"{pointer}/{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{pointer}/{index}")

        walk(document, path.name)


def test_every_root_object_rejects_extension_and_declares_required():
    """契约型根对象必须 additionalProperties:false 且显式 required（防止静默扩展）。"""
    for path in SCHEMA_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("x-schema-container"):
            continue  # $defs 容器本身不是业务对象
        if document.get("type") != "object":
            continue
        assert document.get("additionalProperties") is False, f"{path.name} 允许静默扩展"
        assert document.get("required"), f"{path.name} 没有声明 required"
