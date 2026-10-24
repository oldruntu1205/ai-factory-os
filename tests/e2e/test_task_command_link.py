"""全链路：**任务 ↔ 命令的关联**（A14 / 上游 §5、§13）。

## 这个文件的前身是一条"缺口登记"

它原先叫 `test_task_to_command_link_is_a_known_gap`，作用是**把缺口钉在门禁里**：

> 任务侧的 `command_refs` 只在创建时被置为 `[]`，**全仓没有任何写入路径**。
> 一旦有人接上关联，本用例会立刻报红，提醒改掉它。

2026-10-06 关联被接上了，它**按设计报红了**。这里把它改成**正向断言**。

## 接法（以及为什么不是另一种）

任务归 control-plane，命令归 transaction-service（上游 §13 的 owner 表）。
所以"提命令时往任务记录里追加 `command_refs`"是**跨 owner 写**，被 §13 明令禁止。

实际做法：**在组装任务视图时，按 `task_id` 从命令账本读出来**。
命令的 `task_id` 本来就在契约里是必填（`command.json` 的 required），
所以这不是新增事实，而是**把已经存在的事实暴露出来**——且没有第二份真相可漂移。

## 这里验什么

不是"字段非空"（那太弱），而是**双向可索引**：
从任务能走到它的命令，从命令也能回到它的任务，且**跨租户看不到**。
"""

from __future__ import annotations

import json
import uuid

from tests.p0_runtime.conftest import context_body
from tests.p3_runtime.test_p3_runtime import _normalize, _submit, _write_approval

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _create_task(cluster, *, trace: str) -> str:
    context = context_body(trace_id=trace)
    status, created = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context,
            "context": context,
            "idempotency_key": f"idem-{uuid.uuid4().hex[:12]}",
            "input_ref": "schedule-input-demo",
            "input_schema": {"type": "object"},
        },
        headers=IDENTITY,
    )
    assert status == 202, created
    return created["task_id"]


def _submit_command_for(cluster, *, task_id: str, trace: str, object_id: str) -> dict:
    """在**指定任务下**提交一条真命令，返回完整受理回执。"""
    view = _normalize(cluster, trace_id=trace, object_id=object_id)
    approval_id = f"approval-link-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    status, submitted = _submit(
        cluster,
        trace_id=trace,
        normalization_ref=view["normalization_ref"],
        approval_ref=approval_id,
        task_id=task_id,
    )
    assert status == 202, submitted
    return submitted


def test_task_and_command_are_mutually_indexable(cluster):  # A14
    """**从任务能走到命令、从命令也能回到任务**——双向可索引。

    上游 §5 要求"审计时能从任务定位到它产生的副作用"。
    只做到单向（命令带 task_id）是不够的：那样必须先知道命令 id 才查得到，
    而审计的起点恰恰是**任务**。
    """
    trace = f"trace-link-{uuid.uuid4().hex[:8]}"
    task_id = _create_task(cluster, trace=trace)
    submitted = _submit_command_for(cluster, task_id=task_id, trace=trace, object_id="schedule-link")
    command_id = submitted["command_id"]
    context = context_body(trace_id=trace)

    # ① 任务 → 命令
    status, task_view = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{task_id}",
        headers={**IDENTITY, "X-Context": _json(context)},
    )
    assert status == 200, task_view
    assert command_id in task_view["command_refs"], (
        "任务侧的 command_refs 必须包含它名下的命令。\n"
        f"  期望含 {command_id}\n  实际 {task_view['command_refs']}"
    )

    # ② 命令 → 任务：走**受控交易的受理回执**（`p3-command.json` 的 Accepted 含 context）
    #
    #    ⚠️ 不要用 `GET /internal/v1/commands/{id}`：那个视图由
    #    `command-status.json` 规定，而该契约是 `additionalProperties: false`
    #    **且不含 `task_id`**——想让它带上任务得改契约，属另一件事。
    #    Context 里本来就有 `task_id`，用受控路径的视图即可，无需改契约。
    assert submitted["context"]["task_id"] == task_id, (
        "受理回执必须回显命令所属的任务，否则无法从命令回到任务。\n"
        f"  期望 {task_id}\n  实际 {submitted['context'].get('task_id')}"
    )


def test_task_lists_only_its_own_commands(cluster):  # A01
    """一个任务**只列自己的**命令，不把别的任务的命令算进来。

    这条防的是"按 trace 或按租户粗粒度关联"——那会把无关命令挂到任务上，
    审计时看起来有据可查，实际是**错的**。
    """
    trace = f"trace-link-own-{uuid.uuid4().hex[:8]}"
    first = _create_task(cluster, trace=trace)
    second = _create_task(cluster, trace=trace)

    first_command = _submit_command_for(cluster, task_id=first, trace=trace, object_id="schedule-own-a")["command_id"]
    second_command = _submit_command_for(cluster, task_id=second, trace=trace, object_id="schedule-own-b")["command_id"]

    context = context_body(trace_id=trace)
    status, view = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{first}",
        headers={**IDENTITY, "X-Context": _json(context)},
    )
    assert status == 200, view

    assert first_command in view["command_refs"]
    assert second_command not in view["command_refs"], (
        "另一个任务的命令被挂到了本任务上——关联过粗（例如按 trace 关联）"
    )


def test_command_without_a_task_is_not_attached_to_any(cluster):  # A14
    """`task_id` 为空的命令**不属于任何任务**，不得被任何任务列出来。

    非任务场景产生的命令是合法的（契约里 `task_id` 可为 null），
    但它们**不能**被随便挂到一个任务上——那会凭空造出关联。
    """
    trace = f"trace-link-none-{uuid.uuid4().hex[:8]}"
    task_id = _create_task(cluster, trace=trace)

    # 不传 task_id：命令落在默认的 `task-demo` 上（一个与本次任务无关的占位 id）
    view = _normalize(cluster, trace_id=trace, object_id="schedule-none")
    approval_id = f"approval-none-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    status, submitted = _submit(
        cluster, trace_id=trace, normalization_ref=view["normalization_ref"], approval_ref=approval_id
    )
    assert status == 202, submitted

    context = context_body(trace_id=trace)
    status, fetched = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{task_id}",
        headers={**IDENTITY, "X-Context": _json(context)},
    )
    assert status == 200, fetched
    assert submitted["command_id"] not in fetched["command_refs"], (
        "挂在 task-demo 上的命令被算进了另一个任务的 command_refs——关联按了错的东西"
    )
