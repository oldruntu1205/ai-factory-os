"""跨租户隔离（上游 §17 A01 / §13 / §0.1 原则 1）。

## 为什么单独一个目录

上游 §15 的建议仓库布局里，`tests/isolation/` 是一个**独立边界**：

    tests/contracts/、isolation/、transactions/、edge/、e2e/

本仓此前按**实施阶段**组织（`p0_runtime`、`p2_storage`…），于是"跨租户"
这件事散落在 `tests/p2_storage/test_storage_isolation.py` 与各处运行时用例里——
**能被找到，但边界不显式**。

本组把 A01 的边界**显式化**。已有覆盖**不迁移**（避免两份真相）；
本组只补**边界入口**，并在用例里注明既有覆盖在哪。

## A01 原文

> 跨租户 task、Memory、RAG、object 和 command 读取均被拒绝

本组覆盖 **task / command / 未登记租户**（控制面与事务面）。
**Memory / RAG** 由 `tests/p1_runtime` 的知识面用例覆盖，此处不重复。

## 一条贯穿本组的判据纪律

每条"读不到"的断言，**前面必须先证明这条记录存在**（本租户读得到）。
否则"读不到"可能只是"因为压根没这条记录"——那是**假绿**。
"""

from __future__ import annotations

import json
import uuid

from tests.p0_runtime.conftest import context_body

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def _headers(tenant: str, trace: str) -> dict:
    """GET 的 Context 走 `X-Context` 头（`_p3_get_context`：GET 无 body）。"""
    return {**IDENTITY, "X-Context": json.dumps(context_body(tenant=tenant, trace_id=trace))}


def _context(tenant: str, trace: str, **extra) -> dict:
    return {**context_body(tenant=tenant, trace_id=trace), **extra}


def test_task_of_another_tenant_is_not_readable(cluster):  # A01
    """在 tenant-demo 建的任务，**用 tenant-c3 去读也取不到**。

    这是 A01 的正面形式：不是"接口报了个错"，而是
    **另一个租户拿着同样的 task_id 也拿不到那条记录**。
    两者差别很要紧——前者可能只是参数校验，后者才是隔离。
    """
    trace = f"trace-iso-task-{uuid.uuid4().hex[:8]}"
    context = _context("tenant-demo", trace)

    # ① 先建一条真实任务
    created = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context,
            "context": context,
            "idempotency_key": f"iso-task-{uuid.uuid4().hex[:8]}",
            "input_ref": "iso-input",
            "input_schema": {"type": "object"},
        },
        headers=IDENTITY,
    )
    assert created[0] == 202, f"建任务失败，后续断言无意义：{created}"
    task_id = created[1]["task_id"]

    # ② **本租户先读得到** —— 证明这条记录确实存在（否则 ③ 是假绿）
    own = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{task_id}",
        headers=_headers("tenant-demo", trace),
    )
    assert own[0] == 200, f"本租户必须读得到自己的任务：{own}"

    # ③ 另一个租户用**同一个 task_id** 读 → 必须拿不到
    other = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{task_id}",
        headers=_headers("tenant-c3", trace),
    )
    assert other[0] != 200, (
        "跨租户读到了别家任务！上游 §13「所有业务主键/唯一索引和幂等域包含 tenant」"
        f"与 §17 A01 都要求拒绝。实际：{other}"
    )


def test_missing_command_answers_are_indistinguishable_across_tenants(cluster):  # A01
    """对**同一不存在的 command**，两个租户得到的回答必须同构。

    ## 这条守的不是"拒绝"，而是"不泄露存在性"

    单纯要求"读不到"是不够的：如果 tenant-demo 得到
    `COMMAND_NOT_FOUND`、tenant-c3 得到 `FORBIDDEN`，那么**响应差异本身**
    就成了探测器——攻击者可以据此枚举出"别的租户有没有这条记录"。

    所以这里比的是**两个租户错误码是否一致**，而不是各自是否报错。
    """
    trace = f"trace-iso-cmd-{uuid.uuid4().hex[:8]}"
    ghost = f"command-{uuid.uuid4().hex[:16]}"

    a = cluster.get("transaction-service", f"/internal/v1/commands/{ghost}",
                    headers=_headers("tenant-demo", trace))
    b = cluster.get("transaction-service", f"/internal/v1/commands/{ghost}",
                    headers=_headers("tenant-c3", trace))

    assert a[0] != 200 and b[0] != 200, f"不存在的命令不该返回 200：{a} / {b}"
    assert a[0] == b[0] and a[1].get("code") == b[1].get("code"), (
        "同一不存在的 command 在两个租户下的回答必须一致，"
        "否则可用响应差异探测别租户是否存在该记录。\n"
        f"  tenant-demo: HTTP {a[0]} {a[1].get('code')!r}\n"
        f"  tenant-c3  : HTTP {b[0]} {b[1].get('code')!r}"
    )


def test_unregistered_tenant_is_rejected_by_control_and_decision(cluster):  # A01
    """未登记租户 `tenant-ghost` 在**控制面与决策面**都必须被拒（fail-closed）。

    上游 §7：「未知工具/动作默认拒绝」；§0.1 原则 1 的租户归属同理。
    这条不针对某个接口，而是**抽查两个不同平面**——隔离漏一个平面就等于没隔离。
    """
    trace = f"trace-iso-ghost-{uuid.uuid4().hex[:8]}"
    context = _context("tenant-ghost", trace)

    attempts = [
        ("control-plane", "/api/v1/tasks",
         {**context, "context": context,
          "idempotency_key": f"iso-ghost-{uuid.uuid4().hex[:8]}",
          "input_ref": "iso-input", "input_schema": {"type": "object"}}),
        ("decision-service", "/internal/v1/decisions",
         {**context, "context": context, "catalog_version": "catalog-mock-1",
          "input_ref": "iso-input"}),
    ]
    for service, path, body in attempts:
        status, payload = cluster.post(service, path, body, headers=IDENTITY)
        assert status in (401, 403), (
            f"{service} {path} 对未登记租户 tenant-ghost 必须拒绝（fail-closed），"
            f"实际 HTTP {status}：{payload}"
        )
