"""跨 owner 权威读投影（P1.5，契约 `p15-lifecycle.json`）。

上游 §12/§13 的要求：

- Task 只保存**编排状态投影**，Command 的最终副作用状态归 Transaction；
- 控制台显示必须带**状态来源**与更新时间，防止 UI 显示成功而 ERP 未过账；
- 一个事实只能有**一个**真相源，另一个 owner 通过契约读取而非复制。

因此这里定义的是"别人怎么读我"的**只读视图**，不是第二份存储：
- `TaskView` / `RunView` 由各自的 owner（control-plane / hermes-runtime）从
  **自己的**状态记录投影出来；
- 两个视图都强制 `is_posting_evidence=false` 与
  `business_execution_source="transaction-service"` —— 业务成功只能由权威回执驱动，
  任何编排/受理状态都不能被当成过账证据。
"""

from __future__ import annotations

from typing import Any

# 与 p15-lifecycle.json 的 OwnerUnit 枚举一致
OWNER_UNITS = (
    "control-plane",
    "hermes-runtime",
    "security-gateway",
    "decision-service",
    "manufacturing-core",
    "tool-gateway",
    "transaction-service",
    "trajectory-eval",
)

# 与 state-machines.json#/$defs/Task 一致；用于视图校验（不改变状态机定义）
TASK_STATUSES = (
    "ACCEPTED",
    "RUNNING",
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
)
RUN_STATUSES = (
    "CREATED",
    "RUNNING",
    "CHECKPOINTED",
    "WAITING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)


def task_view(record: dict[str, Any], context) -> dict[str, Any]:
    """control-plane 的 Task 权威视图。

    只暴露编排状态；`is_posting_evidence` 恒为 false —— 它不是过账证据。
    """
    return {
        "context": context.as_dict(),
        "status": record["status"],
        "status_source": "control-plane",
        "updated_at": record["updated_at"],
        "record_version": int(record.get("record_version", 0)),
        "command_refs": list(record.get("command_refs") or []),
        "business_execution_source": "transaction-service",
        "is_posting_evidence": False,
    }


def run_view(record: dict[str, Any], context) -> dict[str, Any]:
    """hermes-runtime 的 Run 权威视图（含检查点版本）。"""
    checkpoint = record.get("checkpoint")
    return {
        "context": context.as_dict(),
        "run_id": record["run_id"],
        "status": record["status"],
        "status_source": "hermes-runtime",
        "updated_at": record["updated_at"],
        "record_version": int(checkpoint["version"]) if checkpoint else 0,
        "command_refs": list(record.get("command_refs") or []),
        "business_execution_source": "transaction-service",
        "is_posting_evidence": False,
    }
