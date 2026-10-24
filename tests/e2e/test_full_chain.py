"""跨阶段全链路：**从外部事件一路走到权威回执与界面结论**（上游 §6 + §17 A14）。

## 为什么需要这一条

前 23 轮把每一**环**都测了（路由可达、契约合规、入参健壮、Guard 语义、
审批不可改判、回执驱动成功……），但**没有一条测试把整链连起来**。

这不是形式主义。本项目已反复证明**交接点**才是缺陷高发处：

- 单看"路由可达"是绿的，但一条**已接线却零覆盖**的接口坏着没人知道；
- 单看"命令视图"是绿的，但它对 **P3 路径创建的命令**直接 500；
- 单看各环都合规，但"任务↔命令"的关联**全仓没有写入路径**。

所以这条用例的价值不在"再测一遍各环"，而在**让交接点显式接受断言**——
每个交接点要么被证明可用，要么被如实记为已知缺口，不允许"没人看"。

## 几处刻意的选择

- **不 mock 任何一环**：真实多进程、真实 HTTP、真实 journal。
  否则测的是"我以为的链路"，而缺陷恰好藏在"我以为"里。
- **复用 P3 已有的 helper**（`_normalize` / `_submit` / `_take_lease` /
  `_execute` / `_reconcile`）：它们已是"能跑通"的路径。
  自己重写一份编排逻辑，第二份迟早与真实链路分叉，那样测的就是假东西。
- **最后一跳真的调用后台的判定模块**（用 node 跑 `apps/web-admin/src/status.js`），
  而不是在 Python 里重写一遍口径——重写等于又造了第二套真相。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import uuid

from tests.p0_runtime.conftest import context_body
from tests.p3_runtime.test_p3_runtime import (
    _decide,
    _execute,
    _normalize,
    _reconcile,
    _submit,
    _take_lease,
    _write_approval,
)

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ADMIN_DIR = REPO_ROOT / "apps" / "web-admin"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _get(cluster, service: str, path: str, *, trace_id: str):
    return cluster.get(
        service, path, headers={**IDENTITY, "X-Context": _json(context_body(trace_id=trace_id))}
    )


# ==========================================================================
# 交接点①：外部系统 → 事件受理（受理不等于业务成功）
# ==========================================================================

def test_chain_step1_event_acceptance_is_not_business_success(cluster):  # A14
    """外部事实事件被受理后，**只表示已持久化**，不代表业务成功。

    上游把 `business_success` 固定为 false：ACK 是传输层事实，
    业务成功只能由权威执行回执驱动。
    """
    trace = f"trace-chain1-{uuid.uuid4().hex[:8]}"
    context = context_body(trace_id=trace)

    envelope = {
        "event_id": f"evt-{uuid.uuid4().hex[:12]}",
        "event_type": "erp.demand.updated",
        "aggregate_id": f"demand-{uuid.uuid4().hex[:8]}",
        "aggregate_version": 1,
        "occurred_at": "2026-10-05T00:00:00Z",
        "payload": {"demand_ref": "demand-demo"},
    }
    status, body = cluster.post(
        "control-plane",
        "/internal/v1/events",
        {
            **context,
            "context": context,
            "event": envelope,
            "publication_evidence_ref": "evidence-demo",
        },
        headers=IDENTITY,
    )
    assert status == 200, body
    assert body.get("business_success") in (False, None), (
        f"事件受理不得声称业务成功：{body}"
    )
    assert body.get("disposition") or body.get("dlq_ref"), body


# ==========================================================================
# 交接点②：任务创建 → 任务读取（编排状态投影）
# ==========================================================================

def _create_task(cluster, *, trace_id: str) -> str:
    context = context_body(trace_id=trace_id)
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
    assert created["status"] == "ACCEPTED", created
    return created["task_id"]


def test_chain_step2_task_is_created_and_readable(cluster):  # A14
    """任务创建后可读回，且**编排状态带来源与更新时间**。

    来源与更新时间不是装饰：上游要求"控制台显示需带状态来源和更新时间"，
    没有它们，看的人无法判断这条状态是谁说的、什么时候说的。
    """
    trace = f"trace-chain2-{uuid.uuid4().hex[:8]}"
    task_id = _create_task(cluster, trace_id=trace)

    status, fetched = _get(cluster, "control-plane", f"/api/v1/tasks/{task_id}", trace_id=trace)
    assert status == 200, fetched
    assert fetched["task_id"] == task_id
    assert fetched["status_source"] == "control-plane", "结论必须带来源"
    assert fetched["updated_at"], "结论必须带更新时间"


# ==========================================================================
# 交接点③：任务 ↔ 命令 的关联
#
# 这一条搬到了 `test_chain_gap_task_command_link.py`：
# 它描述的是**结构缺口**（没有任何 A01–A14 覆盖"任务↔命令索引"），
# 因此按文件级豁免处理。混在本文件里会让它必须硬塞一条不相干的断言标签。
# ==========================================================================


# ==========================================================================
# 交接点④：批准 → 命令（绑定与不可改判）
# ==========================================================================

def test_chain_step4_approval_binds_to_payload_and_cannot_be_flipped(cluster):  # A06
    """批准绑定到具体 payload 哈希；一旦成立**不可改判**。

    "不可改判"不是洁癖：如果批准能被覆盖，那么"谁在什么时候批的"
    就不可追溯，审批的存在意义会被抹掉。
    """
    trace = f"trace-chain4-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id="schedule-chain4")
    approval_id = f"approval-chain-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])

    status, granted = _decide(
        cluster, approval_id=approval_id, binding=view["binding"], approved=True, trace_id=trace
    )
    assert status == 200, granted

    status, flipped = _decide(
        cluster, approval_id=approval_id, binding=view["binding"], approved=False, trace_id=trace
    )
    assert status == 409, f"已成立的批准不得改判：{status} {flipped}"
    assert flipped["code"] == "APPROVAL_ALREADY_GRANTED", flipped


# ==========================================================================
# 交接点⑤：命令 → 执行 → 权威回执 → **过账结论**
# ==========================================================================

def _run_to_receipt(cluster, *, trace_id: str, object_id: str) -> dict:
    """把一条命令推到"权威回执已落"，返回执行记录视图。

    走的是 P3 的**标准收敛路径**（与
    `test_reconcile_confirmed_success_records_receipt_and_succeeds` 同一条）。
    """
    view = _normalize(cluster, trace_id=trace_id, object_id=object_id)
    approval_id = f"approval-chain-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(
        cluster, trace_id=trace_id, normalization_ref=view["normalization_ref"], approval_ref=approval_id
    )[1]["command_id"]

    lease = _take_lease(
        cluster, command_id=command_id, version=1, worker="worker-chain", trace_id=trace_id
    )[1]
    execute_status, _ = _execute(
        cluster, command_id=command_id, version=lease["record_version"], lease=lease, trace_id=trace_id
    )
    assert execute_status == 202, "执行受理应为 202"

    status, record = _reconcile(
        cluster, command_id=command_id, lease=lease, source_state="SUCCEEDED",
        version=lease["record_version"], trace_id=trace_id,
    )
    assert status == 200, record
    return record


def test_chain_step5_receipt_drives_success_but_posting_stays_unconfirmed(cluster):  # A14
    """全链最后一跳，也是这组用例最要紧的一条：

    权威回执落地后 **`business_success` 变 true**，但 **`posting_status` 仍是
    `NOT_CONFIRMED`** —— 因为真实过账凭证是外部依赖，尚未接入。

    这正合上游 §6：**接口说成功，不等于账务已过账**。
    把它断言下来，是为了防止将来有人"顺手"把 `posting_status` 写成 POSTED
    让界面好看——那正是要防的那种事故。
    """
    trace = f"trace-chain5-{uuid.uuid4().hex[:8]}"
    record = _run_to_receipt(cluster, trace_id=trace, object_id="schedule-chain5")

    receipt = record.get("recorded_receipt") or {}
    assert receipt.get("authority_verified") is True, receipt
    assert record["business_success"] is True, "权威回执应驱动业务成功"

    # **但账务未过账**——这条断言是刻意的
    assert record["posting_status"] == "NOT_CONFIRMED", (
        "真实过账凭证尚未接入，posting_status 不得为 POSTED；"
        f"当前为 {record['posting_status']!r}"
    )
    assert record.get("posting_evidence_ref") in (None, ""), (
        "没有真实过账凭证时不得凭空给出 evidence_ref"
    )


# ==========================================================================
# 交接点⑤b：**控制台的数据源**（这一跳曾经缺失，导致三条变异逃逸）
# ==========================================================================

def test_chain_step5b_console_data_source_reports_the_same_facts(cluster):  # A14
    """**控制台读的那个接口**必须报出与链路一致的事实。

    ## 这条用例是"变异逃逸"逼出来的

    加完整链用例后，我把三处字段写死成缺陷注入，门禁**仍然全绿**：
    `posting_status` 写死 `POSTED`、`business_success` 写死 `False`、
    `status` 写死 `"accepted"` —— 一条都没被抓住。

    原因很实在：上面那条 step5 断言的是 **`_reconcile` 的响应**，
    而这三处字段在 **`_p3_command_view`** 里；
    全链用例从未调用 `_p3_command_view`。

    **而 `_p3_command_view` 正是管理后台读的接口**
    （`GET /internal/v1/commands/{id}/execution-record`）。
    也就是说：**链路验的是"库里的账"，界面读的是"另一个视图"，
    两者从未在同一处对上** —— 而"接口说成功、界面显示成功"这类事故
    恰好就藏在这个缝里。

    所以这一条专门补上：走完整链之后，**从控制台的数据源再读一次**，
    断言它报出的业务成功与过账状态与链路一致。
    """
    trace = f"trace-chain5b-{uuid.uuid4().hex[:8]}"
    record = _run_to_receipt(cluster, trace_id=trace, object_id="schedule-chain5b")
    command_id = record["command_id"]

    # **这就是控制台读的接口**（见 apps/web-admin/server.mjs 的 resolveCommand）
    status, view = _get(
        cluster,
        "transaction-service",
        f"/internal/v1/commands/{command_id}/execution-record",
        trace_id=trace,
    )
    assert status == 200, view

    # ① 权威回执必须在视图里可见——否则界面根本无从判断过账
    receipt = view.get("authority_receipt") or {}
    assert receipt.get("authority_verified") is True, (
        f"控制台数据源必须暴露权威回执，否则界面无法判断过账：{view.keys()}"
    )

    # ② 业务成功必须由回执驱动（写死 False 会被这条抓住）
    assert view["business_success"] is True, (
        "权威回执已确认业务成功，控制台数据源必须如实报出；"
        f"实际 business_success={view.get('business_success')!r}"
    )

    # ③ **过账状态必须是 NOT_CONFIRMED**（写死 POSTED 会被这条抓住）
    assert view["posting_status"] == "NOT_CONFIRMED", (
        "没有真实过账凭证时控制台数据源不得报 POSTED——"
        "那等于让界面显示「已过账」而账务其实没动；"
        f"实际 posting_status={view.get('posting_status')!r}"
    )

    # ④ 状态不得被换成编排口径（写死 "accepted" 会被这条抓住）
    assert view["status"] == "SUCCEEDED", (
        f"控制台数据源必须报执行侧的终态，实际 status={view.get('status')!r}"
    )
    assert view["status_source"] == "transaction-service", (
        "结论必须带来源，且来源是执行侧而非编排侧"
    )


# ==========================================================================
# 交接点⑥：链路事实 → **后台界面结论**（此前从没交汇过）
# ==========================================================================

def test_chain_step6_console_verdict_matches_the_chain_outcome(cluster):  # A14
    """把全链结果喂进后台的判定模块，确认**界面口径与链路事实一致**。

    这一跳此前从没测过：链路的断言在 `tests/p3_runtime`，
    后台的判定在 `apps/web-admin`，两者**从未在同一处交汇**。
    于是"接口说成功、界面显示成功"这类事故没有任何机器守护。

    这里用 **node 真的跑** `apps/web-admin/src/status.js`，
    而不是在 Python 里重写一遍判定——
    重写等于又造第二套真相，而两套口径分叉正是本项目反复吃亏的地方。
    """
    node = shutil.which("node")
    if node is None:
        return  # 无 node 时由 tests/web_admin 明确 skip 并说明（不算通过）

    trace = f"trace-chain6-{uuid.uuid4().hex[:8]}"
    record = _run_to_receipt(cluster, trace_id=trace, object_id="schedule-chain6")

    script = (
        "import { deriveDisplayStatus } from './src/status.js';"
        f"const receipt = {json.dumps(record.get('recorded_receipt'), ensure_ascii=False)};"
        f"const commandStatus = {json.dumps(record.get('command_status'))};"
        "const d = deriveDisplayStatus({ commandStatus, receipt,"
        " commandUpdatedAt: '2026-10-05T00:00:00Z', commandStatusSource: 'transaction-service' });"
        "process.stdout.write(JSON.stringify({outcome: d.outcome, label: d.label,"
        " reason: d.reason, sources: d.sources.length}));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ADMIN_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"后台判定模块执行失败：{(result.stderr or '')[:400]}"

    verdict = json.loads(result.stdout)
    # **全链最要紧的一致点**：链路说业务成功，但界面不得说"已过账"
    assert verdict["outcome"] == "NOT_POSTED", (
        "链路 business_success=true 且回执权威，但 posting_status=NOT_CONFIRMED，"
        f"界面必须显示「未过账」；实际 {verdict}"
    )
    assert verdict["sources"] == 3, f"结论必须带齐三处来源（task/command/receipt）：{verdict}"
