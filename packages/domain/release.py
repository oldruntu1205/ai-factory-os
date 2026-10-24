"""边缘发布状态机（上游 §12）：7 状态、只允许相邻推进、`REVOKED` 是唯一终态。"""

from __future__ import annotations

from typing import Final

EDGE_RELEASE_STATES: Final[tuple[str, ...]] = (
    "DRAFT",
    "SCHEMA_FROZEN",
    "CALIBRATED",
    "SHADOW",
    "APPROVED",
    "ACTIVE",
    "REVOKED",
)

EDGE_RELEASE_ORDER: Final[dict[str, int]] = {
    state: index for index, state in enumerate(EDGE_RELEASE_STATES)
}

# 唯一终态：进入后不能再出发
TERMINAL_RELEASE_STATE: Final[str] = "REVOKED"


def release_transition_allowed(current: str, target: str) -> bool:
    """只允许**相邻推进**；`REVOKED` 是终态，不能再出发。

    允许从任意非终态**直接撤销**到 `REVOKED`——撤销是"停止使用"，
    不该要求先把状态推进到某一步。
    """
    if current not in EDGE_RELEASE_ORDER or target not in EDGE_RELEASE_ORDER:
        return False
    if current == TERMINAL_RELEASE_STATE:
        return False
    if target == TERMINAL_RELEASE_STATE:
        return True
    return EDGE_RELEASE_ORDER[target] == EDGE_RELEASE_ORDER[current] + 1
