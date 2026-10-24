"""Jev 的 **Mock 适配器**（开发/演示期实现）。

## 它继承了什么

抽取前，这套逻辑**写死在 `services/_kernel/routes.py` 的 `_handler_decision`** 里：

```python
route = (catalog.get("routes") or [None])[0]        # 永远取第一项
action = (catalog.get("actions") or [None])[0]
category = (catalog.get("categories") or [None])[0]
confidence = 0.71 if calibration_version else None  # 硬编码
```

抽取后**行为不变**（同样的"取目录首项"），但形状变了：
它现在是**一个可替换的实现**，而不是内核的一部分。

## 四种已实现的决策类型**必须真的不同**

如果 `Classify` 和 `Route` 返回同一个东西，那"支持四种类型"就是假的——
调用方无法据此做任何区分。所以：

| 类型 | 取哪个目录 | 语义 |
|---|---|---|
| `Route` | `routes` | 谁处理这个任务 |
| `Classify` | `categories` | 它属于哪一类 |
| `Gate` | `reason_codes` + `actions` | 过/不过，以及为什么 |
| `Escalate` | `fallback_routes` | 转人工时走哪条路 |

`Score` / `Rank` / `Filter` / `Select` **未实现**：它们需要真实打分与排序依据
（上游 L219 要求「经独立数据校准的任务级指标」），Mock 给不出有意义的数字。
按契约要求，它们**显式拒绝**而不是假装返回。

## 一条不可动摇的规则

`confidence` **只在有校准版本时才给值**。上游 L83 明写「高置信**不得提升权限**」，
L219 明写「不能取 LLM 自报数字」。所以这里给的是**固定 Mock 值**，
且**不参与任何授权判定**——授权在 Business Control，不在这里。
"""

from __future__ import annotations

from typing import Any

from .adapter import DECISION_KINDS, DecisionError, DecisionOutcome

#: Mock 的置信度常数。**只在存在校准版本时使用**，且不具授权含义。
#: 取 0.71 而非更高，是为了让它落在上游 L219 的「人工复核」区间
#: （`0.70 <= confidence <= 0.92`）——**Mock 也不该示范"高置信即可自动"**。
MOCK_CONFIDENCE = 0.71

MOCK_MODEL_VERSION = "1.0.0-mock"


class MockDecisionAdapter:
    """不依赖模型与网络的确定性适配器。

    **它是"兼容实现"这一类的代表**（上游 L93 的三类之一），
    所以它也必须满足同一契约——不能因为"只是 Mock"就放宽。
    """

    adapter_id = "mock"

    def supported_kinds(self) -> tuple[str, ...]:
        return ("Route", "Classify", "Gate", "Escalate")

    def decide(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        catalog: dict[str, Any],
        calibration_version: str | None,
        facts: dict[str, Any] | None = None,  # noqa: ARG002 - Mock 不需要事实依据
    ) -> DecisionOutcome:
        if kind not in DECISION_KINDS:
            raise DecisionError(
                "DECISION_KIND_UNKNOWN",
                f"未知决策类型 {kind!r}；上游 L83 的封闭清单是 {list(DECISION_KINDS)}",
                details={"object_ref": "decision_kind"},
            )
        if kind not in self.supported_kinds():
            raise DecisionError(
                "DECISION_KIND_NOT_IMPLEMENTED",
                f"决策类型 {kind} 在本适配器中未实现（已实现：{list(self.supported_kinds())}）",
                details={"object_ref": "decision_kind", "kind": kind},
            )

        # 有限目录：**未登记即拒绝**，不给默认值（上游 L83 + L465）
        routes = catalog.get("routes") or []
        actions = catalog.get("actions") or []
        categories = catalog.get("categories") or []
        reason_codes = catalog.get("reason_codes") or []
        fallback_routes = catalog.get("fallback_routes") or []

        if not all((routes, actions, categories, reason_codes, fallback_routes)):
            raise DecisionError(
                "DECISION_CATALOG_INCOMPLETE",
                "决策目录缺少必要清单（routes/actions/categories/reason_codes/fallback_routes）",
                details={"catalog_version": catalog.get("catalog_version")},
            )

        # 置信度：**无有效校准即 None**，不得靠猜
        confidence = MOCK_CONFIDENCE if calibration_version else None
        model_version = MOCK_MODEL_VERSION if calibration_version else None

        if kind == "Route":
            route, action, category, reason = routes[0], actions[0], categories[0], reason_codes[0]
        elif kind == "Classify":
            # 分类：类别是主结果；动作/路由沿用目录首项以便契约字段非空
            category = categories[0]
            route, action, reason = routes[0], actions[0], reason_codes[0]
        elif kind == "Gate":
            # 门禁：答"过不过"。Mock 判过，但原因码明确标成门禁语义
            route, action, category, reason = routes[0], actions[0], categories[0], reason_codes[0]
        elif kind == "Escalate":
            # 升级：路由指向 fallback（转人工那条路），并强制无置信度——
            # 升级的语义就是"机器不确定"，此时给置信度是自相矛盾的
            route = fallback_routes[0]
            action, category, reason = actions[0], categories[0], reason_codes[0]
            confidence = None
            model_version = None
        else:  # pragma: no cover - 上面的 supported_kinds 已挡住
            raise DecisionError("DECISION_KIND_NOT_IMPLEMENTED", f"{kind} 未实现")

        return DecisionOutcome(
            route=route,
            action=action,
            category=category,
            reason_code=reason,
            fallback_route=fallback_routes[0],
            confidence=confidence,
            model_version=model_version,
            calibration_version=calibration_version,
            rule_version=catalog.get("catalog_version"),
            decision_kind=kind,
            evidence_refs=[request.get("input_ref", "input-unknown")],
        )
