"""影子报告的**读路径**语义（上游 §9 + A01 + A09）。

## 为什么要单独一个文件

`GET /api/v1/edge/shadow-reports/{report_id}` 此前是**唯一一条零覆盖路由**
（用 `V3_ROUTE_COVERAGE_FILE` 实测出来的：61 条注册路由里 60 条被调过，就它没有）。
可达性只能证明"能调到"——它的业务语义完全没人验过，所以在这里补齐。

它可直接测试的原因：与 `POST /edge/v1/shadow-reports` **共用同一个 journal**
（`edge-shadow-reports`），因此可以真的造出一份报告再读回来，零外部依赖。

## 这三条各自的归属

| 用例 | 断言 | 理由 |
| --- | --- | --- |
| 读回一致 | A09 | 影子链路的事实必须原样可查，不能读一次变一次 |
| 缺失明确 404 | A09 | 同上：读路径不得把"没有"伪装成"有但空" |
| 跨租户不可见 | A01 | 这是 **A01**（跨租户读取被拒绝），不是 A09 |
"""

from __future__ import annotations

import json
import uuid

from tests.p0_runtime.conftest import context_body

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _freeze_threshold(cluster, *, context: dict, threshold_id: str) -> None:
    """先冻结一个满足 14 天门槛的影子门槛。

    影子报告**必须**引用一个已冻结门槛——这正是上游 §9 的设计意图：
    没有事先定好的判定标准，"影子通过"就是事后自圆其说。
    所以这里不能图省事塞个假 `threshold_ref`，必须真的走一遍冻结。
    """
    body = {
        **context,
        "context": context,
        "threshold_id": threshold_id,
        "scenario_ref": "scenario-demo",
        "behavior_hash": "e" * 64,
        "threshold_hash": "f" * 64,
        "coverage_spec_ref": "edge-coverage-spec",
        "coverage_spec_hash": "1" * 64,
        "metrics_spec_ref": "edge-metrics-spec",
        "metrics_spec_hash": "2" * 64,
        "minimum_continuous_seconds": 14 * 24 * 3600,
        "required_conditions": ["工况A"],
        "minimum_coverage_ratio": 0.9,
        "maximum_deviation": 0.05,
        "deviation_unit_ref": "edge-unit-demo",
        "maximum_latency_ms": {"p95": 200, "p99": 500},
        "maximum_timeout_rate": 0.01,
        "maximum_false_positive_rate": 0.02,
        "maximum_false_negative_rate": 0.02,
        "maximum_rejection_rate": 0.1,
        "calibration_package_ref": "edge-calibration-demo",
        "calibration_package_hash": "3" * 64,
        "frozen_at": "2026-10-05T00:00:00Z",
        "frozen_by_ref": "user-approver",
    }
    status, payload = cluster.post(
        "control-plane",
        "/internal/v1/edge/shadow-thresholds/freeze",
        body,
        headers=IDENTITY,
    )
    assert status == 200, payload


def _shadow_report_body(context, threshold_ref: str, *, release_id: str, edge_id: str) -> dict:
    """影子报告请求体。

    两个容易踩的点（都由 handler 的真实实现决定）：
    1. **必须引用已冻结门槛**（`threshold_ref` + 一致的 `threshold_hash`）
       ——上游 §9 不接受"报告自带标准"；
    2. **`report_id` 由服务端生成**，请求体里给的值会被忽略。
       所以调用方要拿响应里的 id，不能拿自己编的那个。
    """
    return {
        **context,
        "context": context,
        "release_id": release_id,
        "edge_id": edge_id,
        "scenario_ref": "scenario-demo",
        "threshold_ref": threshold_ref,
        "threshold_hash": "f" * 64,
        "behavior_hash": "e" * 64,
        "started_at": "2026-10-01T00:00:00Z",
        "ended_at": "2026-10-15T00:00:00Z",
        "continuous_observation_seconds": 14 * 24 * 3600,
        "condition_measurements": [],
        "measured_coverage_ratio": 0.99,
        "measured_deviation": 0.01,
        "deviation_unit_ref": "edge-unit-demo",
        "latency_ms": {"p95": 100},
        "sample_count": 1000,
        "false_positive_count": 0,
        "false_negative_count": 0,
        "false_positive_denominator": 1000,
        "false_negative_denominator": 1000,
        "false_positive_rate": 0.0,
        "false_negative_rate": 0.0,
        "rejection_rate": 0.0,
        "timeout_rate": 0.0,
        "measurement_spec_ref": "edge-measurement-spec",
        "measurement_spec_hash": "4" * 64,
        "hardware_manifest_ref": "edge-hardware-manifest",
        "hardware_manifest_hash": "5" * 64,
    }


def _frozen_threshold_and_report(cluster, *, prefix: str) -> tuple[dict, dict, str]:
    """冻结门槛 → 登记影子报告，返回 (context, 服务端返回的报告, 真实 report_id)。"""
    suffix = uuid.uuid4().hex[:8]
    context = context_body(trace_id=f"trace-{prefix}-{suffix}")
    threshold_id = f"threshold-{prefix}-{suffix}"
    _freeze_threshold(cluster, context=context, threshold_id=threshold_id)

    status, created = cluster.post(
        "manufacturing-core",
        "/edge/v1/shadow-reports",
        _shadow_report_body(
            context, threshold_id, release_id=f"release-{suffix}", edge_id=f"edge-{suffix}"
        ),
        headers=IDENTITY,
    )
    assert status == 200, created
    return context, created, created["report_id"]


def test_shadow_report_round_trip(cluster):  # A09
    """写入后再读回：值必须逐字一致（不能被读路径改写或丢字段）。"""
    context, created, report_id = _frozen_threshold_and_report(cluster, prefix="rt")

    status, fetched = cluster.get(
        "manufacturing-core",
        f"/api/v1/edge/shadow-reports/{report_id}",
        headers={**IDENTITY, "X-Context": _json(context)},
    )
    assert status == 200, fetched
    assert fetched["report_id"] == report_id
    for field in (
        "release_id",
        "edge_id",
        "scenario_ref",
        "sample_count",
        "measured_coverage_ratio",
        "continuous_observation_seconds",
    ):
        assert fetched[field] == created[field], f"{field} 读回后与写入不一致"
    # 门槛引用必须原样保留：读回后换了门槛就等于换了判据
    assert fetched["threshold_ref"] == created["threshold_ref"]
    assert fetched["threshold_hash"] == created["threshold_hash"]


def test_shadow_report_missing_returns_404_not_empty_success(cluster):  # A09
    """**不存在必须明确 404**——返回空对象会让调用方以为「查到了但没内容」。"""
    context = context_body(trace_id=f"trace-shadow-miss-{uuid.uuid4().hex[:8]}")
    status, payload = cluster.get(
        "manufacturing-core",
        f"/api/v1/edge/shadow-reports/shadow-report-does-not-exist-{uuid.uuid4().hex[:6]}",
        headers={**IDENTITY, "X-Context": _json(context)},
    )
    assert status == 404, payload
    assert payload["code"] == "SHADOW_REPORT_NOT_FOUND", payload


def test_shadow_report_is_not_visible_to_another_tenant(cluster):  # A01
    """跨租户不可见：另一租户读到的东西必须与「不存在」无法区分。

    只断言"读不到"、且错误码与"不存在"一致——
    若这里改用"不属于你"之类的专用错误，就等于把资源存在性变成了侧信道。
    """
    _, _, report_id = _frozen_threshold_and_report(cluster, prefix="iso")

    other_context = {
        **context_body(trace_id=f"trace-iso-other-{uuid.uuid4().hex[:8]}"),
        "tenant_id": "tenant-c3",
    }
    status, payload = cluster.get(
        "manufacturing-core",
        f"/api/v1/edge/shadow-reports/{report_id}",
        headers={**IDENTITY, "X-Context": _json(other_context)},
    )
    assert status == 404, f"另一租户读到了影子报告：{payload}"
    assert payload["code"] == "SHADOW_REPORT_NOT_FOUND"
