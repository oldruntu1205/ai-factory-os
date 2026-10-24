"""Saga 补偿链：**补偿的完成必须是挣来的**（上游 §5 / §17 A08）。

## 这条用例钉住一次真实缺陷（2026-10-05）

`_handler_p3_saga_compensate` 曾经在"常规补偿"分支里**直接写上一份
`authority_verified: True, business_success: True` 的回执**，
并把步骤标成 `command_status: "SUCCEEDED"` —— 而那条补偿命令
**从未派发、从未执行、也没有任何回执**。

它同时违反三件事：

1. **核心不变量**（上游 §6 / A14）：只有真实执行产生的权威回执能驱动成功；
2. **一数一源**：命令的最终状态归 Transaction，步骤账不能自己当权威；
3. **可核对性**：那份回执里的 `command_id` 是**另起一个 uuid4** 生成的，
   与步骤自己的补偿命令 id 不同——照回执去查命令会查不到。

## 修好之后应该看到什么

- 补偿受理时：`command_status == "AUTHORIZED"`、**`authority_receipt is None`**；
- 整单**不得**因为"补偿已受理"就报 `COMPENSATED`；
- 只有补偿命令真的走完（租约 → 执行 → 权威回执），才可能报 `COMPENSATED`。

这条用例只验前两条（**不伪造**）；"走完才认"由
`tests/p3_runtime::test_saga_requires_physical_restore_before_accounting_compensation`
覆盖（它现在真的驱动收敛）。
"""

from __future__ import annotations

import json
import uuid

from tests.p0_runtime.conftest import context_body
from tests.p3_runtime.test_p3_runtime import _compensate, _create_saga, _step

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_compensation_is_accepted_not_fabricated_as_succeeded(cluster):  # A08
    """补偿刚受理时**不得**声称成功、**不得**携带回执。

    这是本组最要紧的一条：伪造回执正是"UI 显示成功而账务没动"的同类事故，
    只不过发生在补偿路径上。
    """
    trace = f"trace-comp-honest-{uuid.uuid4().hex[:8]}"
    created = _create_saga(
        cluster, trace_id=trace, steps=[_step("s-physical", "PHYSICAL")]
    )
    assert created[0] == 200, created
    saga_id = created[1]["saga_id"]

    status, view = _compensate(
        cluster, saga_id=saga_id, step_id="s-physical", version=0, trace_id=trace
    )
    assert status == 200, view

    step = next(s for s in view["steps"] if s["step_id"] == "s-physical")
    compensation = step["compensation"]

    assert compensation["command_status"] == "AUTHORIZED", (
        "补偿受理只能是 AUTHORIZED——不得预先声称 SUCCEEDED；"
        f"实际 {compensation['command_status']!r}"
    )
    assert compensation["authority_receipt"] is None, (
        "补偿尚未执行，**不得**携带权威回执（上游 §6：只有真实执行能产生回执）"
    )
    assert compensation["manual_queue_ref"] is None, "常规补偿不该一上来就进人工队列"

    # 整单状态：不能因为"补偿已受理"就报已补偿
    assert view["all_compensation_receipts_verified"] is False, (
        "补偿还没跑完，不得声称回执已验证"
    )
    assert view["status"] != "COMPENSATED", (
        f"补偿才刚受理，整单不得报已补偿；实际 {view['status']!r}"
    )


def test_compensation_registers_a_real_dispatchable_command(cluster):  # A08
    """补偿必须登记一条**真实、可派发**的命令（而不是只写个 id 字符串）。

    判据：拿这个 command_id 去读命令权威视图，必须**读得到**。
    旧实现只在步骤账里写了个 `command_id` 字符串，命令存储里根本没有它——
    于是"补偿命令"既不能被执行，也无法被核对。
    """
    trace = f"trace-comp-real-{uuid.uuid4().hex[:8]}"
    context = context_body(trace_id=trace)
    created = _create_saga(cluster, trace_id=trace, steps=[_step("s-real", "PHYSICAL")])
    assert created[0] == 200, created
    saga_id = created[1]["saga_id"]

    status, view = _compensate(
        cluster, saga_id=saga_id, step_id="s-real", version=0, trace_id=trace
    )
    assert status == 200, view
    compensation_id = next(
        s for s in view["steps"] if s["step_id"] == "s-real"
    )["compensation"]["command_id"]

    # **这就是控制台/运维读命令的接口**
    status, record = cluster.get(
        "transaction-service",
        f"/internal/v1/commands/{compensation_id}/execution-record",
        headers={**IDENTITY, "X-Context": _json({**context, "task_id": "task-demo"})},
    )
    assert status == 200, (
        f"补偿命令必须真实存在于命令存储中（否则无法执行、无法核对）：{status} {record}"
    )
    assert record["command_id"] == compensation_id
    # 还没执行 ⇒ 不得有回执、不得报成功
    assert record.get("authority_receipt") in (None, {}), record.get("authority_receipt")
    assert record.get("business_success") is False, (
        "补偿命令尚未执行，business_success 不得为真"
    )
