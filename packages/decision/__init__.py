"""Jev Decision Fabric 适配接口（上游 §3 / §10.1 / §16.1）。

本包是**纯逻辑**：不依赖服务 DB、不依赖运行时、不做网络调用。
真实 Jev 产品的适配器**也应当满足 `adapter.DecisionAdapter`**，
从而让"换实现不改内核"成立。
"""

from .adapter import (
    DECISION_KINDS,
    IMPLEMENTED_KINDS,
    DecisionAdapter,
    DecisionError,
    DecisionOutcome,
)
from .mock_adapter import MOCK_CONFIDENCE, MockDecisionAdapter
from .registry import adapter_ids, resolve_adapter
from .rules_adapter import RulesDecisionAdapter

__all__ = [
    "DECISION_KINDS",
    "IMPLEMENTED_KINDS",
    "MOCK_CONFIDENCE",
    "DecisionAdapter",
    "DecisionError",
    "DecisionOutcome",
    "MockDecisionAdapter",
    "RulesDecisionAdapter",
    "adapter_ids",
    "resolve_adapter",
]
