"""契约文件清单（唯一的登记处）。

为什么需要集中登记：每个阶段的门禁只应该校验**本阶段**的契约集合，
同时必须确保"目录里出现的任何契约文件都已登记"——否则新文件会静默逃过所有门禁。

早期做法是每个阶段测试各自维护自己的文件集合，结果新增 P1.5 契约时
P1 门禁报「未经基线锁定的已有契约」。集中登记后：
- 新增契约只需在 `CONTRACT_INVENTORY` 登记 + 在对应阶段门禁里锁定；
- `unregistered()` 让"漏登记"这件事有单一判据。

**注意**：本文件只登记文件名与归属阶段，不复制契约内容，也不生成期望值。
"""

from __future__ import annotations

import pathlib

CONTRACT_ROOT = pathlib.Path(__file__).resolve().parents[2] / "packages" / "contracts"

# 阶段 → (openapi 文件, schema 文件)
CONTRACT_INVENTORY: dict[str, dict[str, tuple[str, ...]]] = {
    "p0": {
        "openapi": ("p0.openapi.json",),
        "schemas": (
            "api-models.json",
            "command-status.json",
            "command.json",
            "common-context.json",
            "decision-result.json",
            "error.json",
            "event-envelope.json",
            "service-manifest.json",
            "state-machines.json",
            "task.json",
            "tool-spec.json",
        ),
    },
    "p1": {
        "openapi": ("p1.openapi.json",),
        "schemas": (
            "p1-classification.json",
            "p1-egress-audit.json",
            "p1-governance.json",
            "p1-knowledge-source.json",
            "p1-readonly.json",
            "p1-token-vault.json",
        ),
    },
    "p15": {
        "openapi": ("p15.openapi.json",),
        "schemas": (
            "p15-dlq.json",
            "p15-event-api.json",
            "p15-inbox.json",
            "p15-lifecycle.json",
            "p15-outbox.json",
            "p15-receipt.json",
        ),
    },
    "p3": {
        "openapi": ("p3.openapi.json",),
        "schemas": (
            "p3-approval.json",
            "p3-cancellation.json",
            "p3-command.json",
            "p3-employee.json",
            "p3-execution.json",
            "p3-saga.json",
            "p3-tool-admission.json",
            "p3-unknown.json",
        ),
    },
    "p4": {
        "openapi": ("p4.openapi.json",),
        "schemas": (
            "p4-autonomy.json",
            "p4-fallback.json",
            "p4-guard.json",
            "p4-observation.json",
            "p4-release.json",
            "p4-scenario.json",
            "p4-shadow.json",
            "p4-spool.json",
        ),
    },
}


def files_of(phase: str) -> set[str]:
    """某阶段的契约文件（相对 packages/contracts 的 POSIX 路径）。"""
    entry = CONTRACT_INVENTORY[phase]
    return {f"openapi/{name}" for name in entry["openapi"]} | {
        f"schemas/{name}" for name in entry["schemas"]
    }


def all_registered() -> set[str]:
    registered: set[str] = set()
    for phase in CONTRACT_INVENTORY:
        registered |= files_of(phase)
    return registered


def present() -> set[str]:
    """磁盘上实际存在的契约文件。"""
    found: set[str] = set()
    for directory in ("openapi", "schemas"):
        for path in (CONTRACT_ROOT / directory).glob("*.json"):
            found.add(f"{directory}/{path.name}")
    return found


def unregistered() -> set[str]:
    """存在但未在任何阶段登记的契约文件（应为空；非空说明有人绕过了门禁登记）。"""
    return present() - all_registered()


def missing() -> set[str]:
    """已登记但磁盘上不存在的契约文件（应为空；非空说明清单与目录不一致）。"""
    return all_registered() - present()


def phase_of(relative: str) -> str | None:
    for phase in CONTRACT_INVENTORY:
        if relative in files_of(phase):
            return phase
    return None
