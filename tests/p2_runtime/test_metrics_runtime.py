"""运维指标运行期验收：真实请求 → journal 事实 → 指标数值（§14）。

`tests/p2_observability/test_metrics.py` 验的是**派生逻辑**（纯函数，快）；
这里验的是**接线**：真实进程收到请求后，指标事实是否真的落了盘、
以及 `/internal/v1/operations/metrics` 是否能把它读出来。

为什么必须分开：派生逻辑对、但**没人写事实**，指标会永远报"无数据"——
而单元测试全绿。这一层就是专门用来抓这种情况的。
"""

from __future__ import annotations

import json
import pathlib
import uuid

from tests.p0_runtime.conftest import context_body

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def _headers(trace_id: str) -> dict:
    return {"X-Context": json.dumps(context_body(trace_id=trace_id))}


def _body(trace_id: str, **extra) -> dict:
    context = context_body(trace_id=trace_id)
    return {**context, "context": context, "tenant_id": "tenant-demo", **extra}


def _model_invocation_body(trace_id: str) -> dict:
    return _body(
        trace_id,
        model_id="model-demo-local",
        model_version="1.0.0-mock",
        model_backend="local",
        purpose="purpose-model-invoke",
        policy_version="policy-demo",
        input_refs=["input-demo"],
        output_schema={"schema_ref": "task.json", "schema_version": "1.0.0", "schema_hash": "sha256:d"},
    )


def test_model_invocation_writes_metric_fact_to_journal(cluster):  # A13
    """真实请求必须**落一条指标事实**——否则指标永远报"无数据"。

    这条抓的是"派生逻辑对、但没人写事实"这类失效：
    单元测试全绿，运维面却一直是空的。
    """
    trace = f"trace-metrics-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/model-invocations",
        _model_invocation_body(trace),
        headers=IDENTITY,
    )
    assert status == 200, payload

    journal_path = cluster.state_dir / "model-invocations.jsonl"
    assert journal_path.exists(), "模型调用端点必须落指标事实（model-invocations.jsonl）"

    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    matching = [record for record in records if record.get("trace_id") == trace]
    assert matching, "本次请求的事实没有落盘"
    record = matching[-1]

    assert record["model_backend"] == "local"
    assert isinstance(record.get("decision_seconds"), (int, float))
    assert record["decision_seconds"] >= 0
    # 语义诚实：必须显式声明未发生真实推理
    assert record["invocation_performed"] is False
    assert "不是" not in record["note"] or "未发生" in record["note"]


def test_operations_metrics_endpoint_reports_recorded_invocation(cluster):  # A13
    """运维端点必须能读出刚记录的调用（计数 > 0，且耗时样本 > 0）。"""
    trace = f"trace-metrics-read-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "security-gateway", "/internal/v1/model-invocations",
        _model_invocation_body(trace), headers=IDENTITY,
    )
    assert status == 200, payload

    status, snapshot = cluster.get(
        "security-gateway", "/internal/v1/operations/metrics", headers=_headers(trace)
    )
    assert status == 200, snapshot
    assert snapshot["environment"] == "mock"
    assert snapshot["source"] == "derived_from_journal"

    invocations = snapshot["snapshot"]["signals"]["model_invocation_total"]
    assert invocations["total"] >= 1
    assert "local" in invocations["by_backend"]

    latency = snapshot["snapshot"]["signals"]["governance_decision_seconds"]
    assert latency["samples"] >= 1, "治理判定耗时必须有样本"
    assert latency["avg"] is not None
    # 描述里必须写明它不是生成耗时
    assert "不是" in latency["note"]

    # 端点必须自报"哪些指标没有数据来源"——而不是让人以为全都健康
    assert isinstance(snapshot["no_data_signals"], list)


def test_readiness_backlog_reflects_unfinished_work(cluster):  # A14
    """就绪端点的 backlog 必须是**真实积压**，不能是硬编码 0。

    构造一个真实未终结任务，再确认 backlog 增长。
    （此前这里用 `/internal/v1/commands` 直接建命令——那条路径需要 22 个必填字段，
    我的请求形状是错的。**这个错误顺带暴露了一个真缺陷**：
    缺字段原本会抛 KeyError 被兜底成 503，见下一条用例。）
    """
    from tests.p0_runtime.conftest import schema_binding

    status, before = cluster.get("control-plane", "/health/ready", headers=_headers("trace-backlog-a"))
    assert status in (200, 503), before
    baseline = before["backlog"]

    trace = f"trace-backlog-{uuid.uuid4().hex[:8]}"
    status, task = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        _body(
            trace,
            idempotency_key=f"backlog-{uuid.uuid4().hex[:10]}",
            input_ref="schedule-demo",
            input_schema=schema_binding(),
        ),
        headers=IDENTITY,
    )
    assert status == 202, task

    # 先确认事实确实落盘——否则"backlog 没变"可能只是写文件的问题，
    # 断言会指向错误的结论。
    tasks_path = cluster.state_dir / "tasks.jsonl"
    assert tasks_path.exists(), "受理的任务必须落 tasks.jsonl"
    records = [json.loads(line) for line in tasks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(record.get("status") == "ACCEPTED" for record in records), (
        f"受理的任务应处于未终结状态：{[r.get('status') for r in records]}"
    )

    status, after = cluster.get("control-plane", "/health/ready", headers=_headers("trace-backlog-b"))
    assert status in (200, 503), after
    assert after["backlog"] > baseline, (
        f"未终结任务必须计入积压：before={baseline} after={after['backlog']}"
    )


def test_missing_required_field_yields_400_not_503(cluster):  # A14
    """**客户端请求不合法必须是 4xx，不能是 5xx。**

    实测缺陷：`/internal/v1/commands` 直接取 `body["command_type"]` 等字段，
    缺字段时抛 `KeyError` → 被外层兜底成 `503 INTERNAL_ERROR`。
    把调用方的错误报成"服务内部错误"，会让调用方以为服务坏了，
    也会让排障的人去查错方向。
    """
    trace = f"trace-missing-field-{uuid.uuid4().hex[:8]}"
    status, payload = cluster.post(
        "transaction-service",
        "/internal/v1/commands",
        _body(trace, tool_id="pmc.dispatch.publish", tool_version="1.0.0-mock"),
        headers=IDENTITY,
    )
    assert status == 400, payload
    assert payload["code"] == "REQUEST_INVALID", payload
    assert "command_type" in json.dumps(payload, ensure_ascii=False)


def test_metrics_endpoint_has_no_tenant_or_prompt_text(cluster):  # A13
    """指标响应里不得出现租户名或 Prompt 正文（§14 标签约束）。"""
    trace = f"trace-metrics-labels-{uuid.uuid4().hex[:8]}"
    status, snapshot = cluster.get(
        "security-gateway", "/internal/v1/operations/metrics", headers=_headers(trace)
    )
    assert status == 200, snapshot
    text = json.dumps(snapshot, ensure_ascii=False)
    assert "tenant-demo" not in text, "指标响应不得含租户标识"
    assert "tenant-c3" not in text
    assert "input-demo" not in text, "指标响应不得含输入引用/正文"
