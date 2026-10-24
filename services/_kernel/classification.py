"""数据分级与派生上下文重分级（P1，契约 `packages/contracts/schemas/p1-classification.json`）。

上游 §4.1 的硬规则（本模块逐条实现）：
- C0–C4 是**五级**封闭枚举；缺失或未知等级**拒绝**，不推断为低等级。
- 正文、图片 OCR、附件、RAG chunk、工具返回、历史 Memory、模型输出、轨迹都要分级。
- **合并上下文取最高等级**，不取平均值、不取首个或末个输入。
- **模型输出继承输入最高等级**，直到可信流程批准降级。
- **匿名化 / Tokenization 不自动降级**（C2 变换后仍是 C2）。
- 派生来源没有已登记规则时拒绝，不套用默认等级。

调用方（Agent）自报的等级只当作**下限**：可信分类结果不得低于自报值，但可以更高。
"""

from __future__ import annotations

from typing import Any

from . import errors
from .registry import Registry

CLASS_ORDER = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}

# 与契约 p1-classification.json#/$defs/Input 的 origin 枚举逐字一致
ORIGINS = (
    "text",
    "image_ocr",
    "attachment",
    "rag_chunk",
    "tool_return",
    "memory",
    "model_output",
    "trajectory",
    "declared_context",
)


def class_rank(data_class: str | None) -> int:
    """未知等级返回一个必然被拒的高排名，而不是 0。"""
    if data_class is None:
        return 99
    return CLASS_ORDER.get(data_class, 99)


def highest(*classes: str | None) -> str | None:
    known = [item for item in classes if item is not None]
    if not known:
        return None
    return max(known, key=class_rank)


class Classification:
    """一次分类判定的结果，含判定依据（便于审计与验收）。"""

    def __init__(self, *, inputs: list[dict[str, Any]], data_class: str, rule_version: str, derived: list[dict[str, Any]]) -> None:
        self.inputs = inputs
        self.data_class = data_class
        self.rule_version = rule_version
        self.derived = derived

    @property
    def output_data_class(self) -> str:
        # 输出继承输入最高等级；P1 不提供降级 API。
        return self.data_class

    def as_dict(self, context) -> dict[str, Any]:
        return {
            "tenant_id": context.tenant_id,
            "context": context.as_dict(),
            "inputs": self.inputs,
            "data_class": self.data_class,
            "output_data_class": self.output_data_class,
            "classification_rule_version": self.rule_version,
        }


def classify(request, *, declared_inputs: list[dict[str, Any]], derived_origins: list[dict[str, Any]]) -> Classification:
    """把请求自报输入与可信派生来源一起分级。

    declared_inputs：请求方给出的输入（含自报等级，只作为下限）。
    derived_origins：可信服务提供的派生来源（如 RAG 命中、工具返回、Memory），
                     必须先解析真实内容再分级——这里用 origin 类别映射到登记规则。
    """
    registry: Registry = request.registry
    rule_version = registry.classification_rule_version()
    if rule_version is None:
        # 没有已登记的分类规则 → 无法建立有效分级上下文 → 拒绝（不能默认低等级）。
        raise errors.reject_registry_unavailable("classification-rule")

    origin_rules: dict[str, str] = registry.derived_origin_rules()

    effective: list[dict[str, Any]] = []
    for item in declared_inputs:
        origin = item.get("origin")
        if origin not in ORIGINS:
            # 未登记的来源类别无法分级。
            raise errors.reject_unknown_data_class(item.get("data_class"))
        derived_class = origin_rules.get(origin)
        if derived_class is None:
            raise errors.reject_registry_unavailable(f"derived-origin:{origin}")
        declared_class = item["data_class"]
        if not registry.known_data_class(declared_class):
            raise errors.reject_unknown_data_class(declared_class)
        # 自报只是下限：取登记规则与自报的较高者。
        merged = highest(declared_class, derived_class) or derived_class
        effective.append(
            {
                "content_ref": item["content_ref"],
                "data_class": merged,
                "origin": origin,
                "anonymized": bool(item["anonymized"]),
                "transformation_rule_ref": item.get("transformation_rule_ref"),
                "review_ref": item.get("review_ref"),
            }
        )

    for item in derived_origins:
        origin = item.get("origin")
        if origin not in ORIGINS:
            raise errors.reject_registry_unavailable("derived-origin:unknown")
        derived_class = origin_rules.get(origin)
        if derived_class is None:
            raise errors.reject_registry_unavailable(f"derived-origin:{origin}")
        effective.append(
            {
                "content_ref": item["content_ref"],
                "data_class": derived_class,
                "origin": origin,
                # 派生内容一律按"未经过可信复核的变换"处理。
                "anonymized": bool(item.get("anonymized", False)),
                "transformation_rule_ref": item.get("transformation_rule_ref"),
                "review_ref": item.get("review_ref"),
            }
        )

    if not effective:
        raise errors.reject_bad_request("input_refs", "至少需要一个可分级输入")

    # 契约要求 inputs 覆盖实际完整上下文；重复引用合并为最高等级。
    by_ref: dict[str, dict[str, Any]] = {}
    for item in effective:
        ref = item["content_ref"]
        existing = by_ref.get(ref)
        if existing is None:
            by_ref[ref] = item
        else:
            existing["data_class"] = highest(existing["data_class"], item["data_class"])
    merged_inputs = list(by_ref.values())

    data_class = highest(*(item["data_class"] for item in merged_inputs))
    if data_class is None:
        raise errors.reject_unknown_data_class(None)
    return Classification(
        inputs=merged_inputs,
        data_class=data_class,
        rule_version=rule_version,
        derived=list(derived_origins),
    )


def contains_key_material(inputs: list[dict[str, Any]], registry: Registry) -> bool:
    """C4 密钥材料：无论后端是否本地都禁止进入模型上下文（上游 §4.3 / §4.1 C4）。

    判定依据是**已登记的密钥材料引用**（`key_material_refs`），不是请求自报：
    "密钥禁止进入模型上下文"是数据分类事实，不能由调用方声明与否决定。
    """
    known = registry.key_material_refs()
    if not known:
        return False
    return any(item.get("content_ref") in known for item in inputs)
