"""适配器解析：**登记表说用哪个，就用哪个；不认识就拒绝。**

上游 L421：「实际版本与 Hermes/**Jev 适配方式需 P0 冻结**」。

冻结的表现就是这张表：登记表 `decision_adapter` 指定实现 id，
内核按 id 解析。**未登记即拒绝（fail-closed）**——
不能"找不到就退回 Mock"，那会让生产环境在配置写错时
**静默使用一个确定性 Mock 做决策**，而调用方看不出区别。

## 换真实 Jev 时要改什么

1. 写一个满足 `DecisionAdapter` 的类（可以放在本包，也可以放在外部适配包）；
2. 在 `registry.mock.json`（生产则是登记表）的 `decision_adapter` 里登记；
3. **内核一行不改**——这正是抽这个包的目的。

如果换实现仍需改内核，说明接口抽象不成立，应当先把接口改对。
"""

from __future__ import annotations

from typing import Any

from .adapter import DecisionAdapter, DecisionError
from .mock_adapter import MockDecisionAdapter
from .rules_adapter import RulesDecisionAdapter

#: 已登记的适配器实现。**新增实现必须在此登记**，
#: 否则登记表指向它时会 fail-closed 拒绝。
#:
#: 现在有两个实现，这本身是判据的一部分：
#: 上游 §3 说三类实现者「均需实现同一契约」——**一个实现证明不了接口是接缝**，
#: 两个实现才能验证"换实现不改内核、不改测试"。
_ADAPTERS: dict[str, type] = {
    "mock": MockDecisionAdapter,
    "rules": RulesDecisionAdapter,
}


def adapter_ids() -> list[str]:
    return sorted(_ADAPTERS)


def resolve_adapter(registry_entry: Any) -> DecisionAdapter:
    """按登记表的 `decision_adapter` 解析适配器。

    `registry_entry` 是登记表里 `decision_adapter` 节点的形状，
    例如 `{"implementation": "mock", "config": {}}`。
    """
    if not isinstance(registry_entry, dict):
        raise DecisionError(
            "DECISION_ADAPTER_NOT_CONFIGURED",
            "登记表未配置 decision_adapter（上游 L421 要求 P0 冻结适配方式）",
        )
    implementation = registry_entry.get("implementation")
    if not isinstance(implementation, str) or not implementation:
        raise DecisionError(
            "DECISION_ADAPTER_NOT_CONFIGURED",
            "decision_adapter.implementation 缺失或不是字符串",
        )
    factory = _ADAPTERS.get(implementation)
    if factory is None:
        # **不退回 Mock**：配置写错时必须能被看见
        raise DecisionError(
            "DECISION_ADAPTER_UNKNOWN",
            f"适配器 {implementation!r} 未登记；已登记：{adapter_ids()}",
            details={"object_ref": "decision_adapter.implementation"},
        )
    return factory()
