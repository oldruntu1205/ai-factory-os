"""运维指标门禁（上游 §14）。

这一组用例守的是两件事：

1. **覆盖**：§14 点名的每一项指标都必须真的有定义（漏一项 = 运维看不见一类故障）；
2. **标签安全**：指标标签**不得**承载客户名称、Prompt 正文或密钥——
   而且防线不是"检查有没有敏感词"，是**标签值只接受受控令牌**，
   自由文本在格式上就进不来。

第三条同样重要：**缺数据必须如实表达为"无数据"，不得用 0 冒充**。
0 与"没测过"含义完全不同，混淆会让运维误判系统健康。
"""

from __future__ import annotations

import json

import pytest

from services._kernel import journal, metrics


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """把 journal 指到临时目录：指标是"从 journal 派生"的，必须控住事实来源。"""
    monkeypatch.setenv("V3_STATE_DIR", str(tmp_path))
    yield


# ==========================================================================
# 一、上游覆盖
# ==========================================================================

def test_every_upstream_metric_requirement_is_defined():  # A13
    """§14 点名要求的指标项必须全部有定义。"""
    missing = list(metrics.signals_missing_upstream_requirement())
    assert not missing, f"§14 要求但未实现（或未标注）的指标：{missing}"


def test_metric_keys_are_unique_and_well_formed():  # A13
    keys = [signal.key for signal in metrics.SIGNALS]
    assert len(keys) == len(set(keys)), "指标 key 重复"
    for key in keys:
        assert key.replace("_", "").isalnum(), f"指标 key 格式不合法: {key}"
        assert key.islower(), f"指标 key 应当全小写: {key}"


def test_every_signal_declares_unit_and_description():  # A13
    for signal in metrics.SIGNALS:
        assert signal.unit, f"{signal.key} 缺单位"
        assert signal.description.strip(), f"{signal.key} 缺描述"
        assert signal.description != signal.key, f"{signal.key} 的描述只是复述 key"


# ==========================================================================
# 二、标签安全（§14：不得含客户名称、Prompt 正文或密钥）
# ==========================================================================

def test_no_signal_uses_a_forbidden_label_key():  # A13
    problems = metrics.label_key_problems()
    assert not problems, "指标定义越界：\n  " + "\n  ".join(problems)


@pytest.mark.parametrize(
    "metric_key,label_key,value",
    [
        ("task_success_total", "outcome", "succeeded"),
        ("compensation_failure_total", "outcome", "manual_review"),
        ("dlp_rejection_total", "reason", "c3"),
        ("edge_disconnection_total", "outcome", "blocked"),
    ],
)
def test_controlled_label_values_are_accepted(metric_key, label_key, value):  # A13
    assert metrics.validate_label(metric_key, label_key, value) == value


@pytest.mark.parametrize(
    "metric_key,label_key,value",
    [
        # 格式合法但不在受控集合内的疑似密钥 —— **格式校验挡不住它**，靠封闭取值挡住
        ("task_success_total", "outcome", "sk-live-abcdef123456"),
        # 客户名称
        ("task_success_total", "outcome", "acme_corp"),
        # 自由文本（含空格）
        ("task_success_total", "outcome", "tenant demo"),
        # 大小写不规范
        ("task_success_total", "outcome", "SUCCEEDED"),
        # 空值
        ("task_success_total", "outcome", ""),
    ],
)
def test_values_outside_the_closed_set_are_rejected(metric_key, label_key, value):  # A13
    """标签值必须在**封闭集合**内。

    这条是本模块最重要的安全判据：仅靠"令牌格式"是不够的——
    `sk-live-abcdef123456` 完全符合小写字母数字连字符格式，实测被放行。
    封闭取值集合才是真正的防线。
    """
    with pytest.raises(ValueError):
        metrics.validate_label(metric_key, label_key, value)


def test_undeclared_label_key_is_rejected():  # A13
    with pytest.raises(ValueError, match="不允许标签键"):
        metrics.validate_label("task_success_total", "tenant_id", "tenant_demo")


def test_unknown_metric_is_rejected():  # A13
    with pytest.raises(ValueError, match="未登记的指标"):
        metrics.validate_label("not_a_metric", "outcome", "succeeded")


def test_no_signal_leaves_a_label_without_a_closed_value_set():  # A13
    """任何标签键都必须有封闭取值集合——否则等于给自由文本留了位置。"""
    problems = metrics.label_key_problems()
    assert not problems, "指标定义越界：\n  " + "\n  ".join(problems)


# ==========================================================================
# 三、从 journal 派生（真实数据）
# ==========================================================================

def test_metrics_are_derived_from_journal_records():  # A13
    journal.append("tasks", {"tenant_id": "tenant-demo", "task_id": "task-a", "status": "SUCCEEDED"})
    journal.append("tasks", {"tenant_id": "tenant-demo", "task_id": "task-b", "status": "ACCEPTED"})
    journal.append("tasks", {"tenant_id": "tenant-demo", "task_id": "task-c", "status": "SUCCEEDED"})
    journal.append("approvals", {"tenant_id": "tenant-demo", "approval_id": "ap-1", "approved": None})

    snapshot = metrics.compute_metrics()
    tasks = snapshot["signals"]["task_success_total"]
    assert tasks["total"] == 3
    assert tasks["succeeded"] == 2
    assert tasks["accepted_only"] == 1
    assert tasks["success_ratio"] == pytest.approx(2 / 3, abs=1e-6)

    waits = snapshot["signals"]["approval_wait_seconds"]["decisions"]
    assert waits["pending"] == 1


def test_success_ratio_is_none_when_there_are_no_samples():  # A13
    """**没有样本时必须是 None，不能是 0**。

    0 会被读成"成功率 0%"（系统全坏），而 None 表示"还没有样本"。
    把这两者混起来，运维会误判。
    """
    snapshot = metrics.compute_metrics()
    assert snapshot["signals"]["task_success_total"]["success_ratio"] is None


def test_cost_metric_explicitly_refuses_to_invent_a_number():  # A13
    """成本口径**待冻结**：不得用占位数值冒充。"""
    snapshot = metrics.compute_metrics()
    cost = snapshot["signals"]["model_cost_total"]
    assert cost["total"] is None
    assert "待冻结" in cost["note"]


def test_model_latency_metric_does_not_claim_to_be_generation_latency():  # A13
    """**语义诚实**：指标不得让"治理判定耗时"冒充"模型生成耗时"。

    当前没有代码真正调用模型——`/internal/v1/model-invocations` 只做准入判定。
    如果把这个耗时记成"模型耗时"，运维会按错误的容量模型做决策。
    因此：有一个**计数**指标反映受理次数；有一个**明确命名**为治理判定的耗时指标；
    **不存在**任何声称是"模型生成耗时"的数值。
    """
    keys = {signal.key for signal in metrics.SIGNALS}
    assert "model_invocation_total" in keys
    assert "governance_decision_seconds" in keys
    assert "model_invocation_seconds" not in keys, (
        "不得存在名为「模型调用耗时」的指标——它会让人以为在测生成耗时"
    )

    description = metrics.SIGNAL_BY_KEY["governance_decision_seconds"].description
    assert "不是" in description and "生成耗时" in description, "指标描述必须写明它**不是**生成耗时"

    snapshot = metrics.compute_metrics()
    assert "不是模型生成耗时" in snapshot["signals"]["governance_decision_seconds"]["note"]
    assert "无生成耗时样本" in snapshot["signals"]["model_invocation_total"]["note"]


def test_governance_decision_latency_is_measured_and_reported():  # A13
    journal.append("model-invocations", {"model_backend": "local", "decision_seconds": 0.002})
    journal.append("model-invocations", {"model_backend": "local", "decision_seconds": 0.004})
    journal.append("model-invocations", {"model_backend": "public", "decision_seconds": 0.010})

    snapshot = metrics.compute_metrics()
    detail = snapshot["signals"]["governance_decision_seconds"]
    assert detail["samples"] == 3
    assert detail["max"] == pytest.approx(0.010)
    assert detail["avg"] == pytest.approx(0.016 / 3, abs=1e-6)  # 报告值保留 6 位小数
    assert detail["by_backend_avg"]["local"] == pytest.approx(0.003)

    counts = snapshot["signals"]["model_invocation_total"]
    assert counts["total"] == 3
    assert counts["by_backend"] == {"local": 2, "public": 1}


def test_latency_metric_reports_no_data_when_there_are_no_samples():  # A13
    journal.append("model-invocations", {"model_backend": "local"})  # 计数有，样本无
    snapshot = metrics.compute_metrics()
    detail = snapshot["signals"]["governance_decision_seconds"]
    assert detail["samples"] == 0
    assert detail["avg"] is None, "无样本必须是 None，不能是 0"


def test_prometheus_renderer_marks_unfrozen_cost_as_no_data():  # A13
    """口径未定（total=None）必须渲染成 NO_DATA，**不能**渲染成 0。"""
    text = metrics.render_prometheus(metrics.compute_metrics())
    assert "v3_model_cost_total{scope=" not in text, "口径未定的成本不得渲染成数值"
    assert "model_cost_total: NO_DATA" in text


def test_unwired_signals_are_reported_not_zeroed():  # A13
    """没有事实来源的指标要出现在 `no_data` 清单里——而不是输出 0。"""
    snapshot = metrics.compute_metrics()
    assert "task_success_total" in snapshot["no_data"]
    assert "model_cost_total" in snapshot["no_data"]


def test_unknown_command_metric_counts_only_unknown_status():  # A13
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-1", "status": "UNKNOWN"})
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-2", "status": "SUCCEEDED"})
    snapshot = metrics.compute_metrics()
    assert snapshot["signals"]["unknown_command_total"]["total"] == 1


def test_compensation_failure_metric_counts_failed_and_manual_review():  # A13
    journal.append("compensations", {"tenant_id": "tenant-demo", "outcome": "FAILED"})
    journal.append("compensations", {"tenant_id": "tenant-demo", "outcome": "MANUAL_REVIEW"})
    journal.append("compensations", {"tenant_id": "tenant-demo", "outcome": "COMPENSATED"})
    snapshot = metrics.compute_metrics()
    assert snapshot["signals"]["compensation_failure_total"]["total"] == 2


def test_edge_metrics_derive_from_edge_journals():  # A13
    journal.append("edge-guard-decisions", {"release_id": "r-1", "verdict": "FALLBACK",
                                           "reasons": ["DRIFT_EXCEEDED"]})
    journal.append("edge-guard-decisions", {"release_id": "r-1", "verdict": "ALLOW", "reasons": []})
    journal.append("edge-autonomy-decisions", {"release_id": "r-1", "autonomy_allowed": False})
    journal.append("edge-spool", {"edge_id": "edge-demo", "event_id": "e-1", "kind": "executed_fact"})

    snapshot = metrics.compute_metrics()
    assert snapshot["signals"]["edge_drift_total"]["total"] == 1
    assert snapshot["signals"]["edge_rejection_total"]["total"] == 1
    assert snapshot["signals"]["edge_disconnection_total"]["blocked"] == 1
    assert snapshot["signals"]["edge_spool_entries"]["by_kind"]["executed_fact"] == 1


# ==========================================================================
# 四、积压（§14：就绪状态必须包含积压）
# ==========================================================================

def test_pending_backlog_reflects_unfinished_commands_and_approvals():  # A13
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-1", "status": "ACCEPTED"})
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-2", "status": "EXECUTING"})
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-3", "status": "SUCCEEDED"})
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-4", "status": "FAILED"})
    journal.append("approvals", {"tenant_id": "tenant-demo", "approval_id": "ap-1", "approved": None})
    journal.append("approvals", {"tenant_id": "tenant-demo", "approval_id": "ap-2", "approved": True})

    # 未终结命令 2 条 + 待决策批准 1 条 = 3
    assert metrics.pending_backlog() == 3


def test_pending_backlog_counts_unfinished_tasks():  # A14
    """**受理但未完成的任务**必须计入积压。

    实测踩过的坑：第一版 `pending_backlog()` 只统计命令/批准/inbox，
    而任务写在**独立的 `tasks` journal** 里 —— 于是"受理了一堆任务"
    完全不计入积压（建一个 ACCEPTED 任务后 backlog 仍是 0）。
    """
    journal.append("tasks", {"task_id": "t-accepted", "status": "ACCEPTED"})
    journal.append("tasks", {"task_id": "t-running", "status": "RUNNING"})
    journal.append("tasks", {"task_id": "t-done", "status": "SUCCEEDED"})
    journal.append("tasks", {"task_id": "t-cancelled", "status": "CANCELLED"})
    # 未终结 2 条
    assert metrics.pending_backlog() == 2


def test_pending_backlog_is_zero_when_everything_settled():  # A13
    journal.append("commands", {"tenant_id": "tenant-demo", "command_id": "c-1", "status": "SUCCEEDED"})
    journal.append("approvals", {"tenant_id": "tenant-demo", "approval_id": "ap-1", "approved": False})
    assert metrics.pending_backlog() == 0


# ==========================================================================
# 五、渲染与环境标注
# ==========================================================================

def test_prometheus_renderer_labels_no_data_explicitly():  # A13
    text = metrics.render_prometheus()
    assert "NO_DATA" in text, "无数据必须显式标注，不能静默省略"
    assert "v3_task_success_total" in text
    # 渲染文本里不得出现 tenant/客户信息（标签受控）
    assert "tenant-demo" not in text


def _walk_label_sets(node):
    """递归收集快照里所有 `labels` 字段的内容。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "labels" and isinstance(value, dict):
                yield value
            else:
                yield from _walk_label_sets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_label_sets(item)


def test_every_emitted_label_passes_the_closed_set_check():  # A13
    """**输出里的每个标签都必须能通过校验**。

    这条锁住的是"校验真的在出口生效"这个不变量。
    否则 `validate_label` 就成了一把没人用的锁——实测踩过：
    变异把封闭取值检查去掉，门禁照样全绿，因为当时的输出里根本没有标签。
    """
    journal.append("tasks", {"task_id": "t-1", "status": "SUCCEEDED"})
    journal.append("approvals", {"approval_id": "ap-1", "approved": True,
                                 "valid_from": "2026-10-05T00:00:00Z",
                                 "decided_at": "2026-10-05T00:01:00Z"})
    journal.append("compensations", {"outcome": "MANUAL_REVIEW"})
    journal.append("egress-audit", {"invocation_performed": False, "highest_data_class": "C4"})
    journal.append("model-invocations", {"model_backend": "local"})
    journal.append("inbox", {"consumer_unit": "control-plane"})
    journal.append("edge-guard-decisions", {"verdict": "FALLBACK", "reasons": ["DRIFT_EXCEEDED"]})
    journal.append("edge-autonomy-decisions", {"autonomy_allowed": False})
    journal.append("edge-spool", {"kind": "executed_fact"})

    snapshot = metrics.compute_metrics()
    seen = 0
    for label_set in _walk_label_sets(snapshot):
        for label_key, value in label_set.items():
            assert isinstance(value, str) and value, f"标签值非法: {label_key}={value!r}"
            # 关键判据：标签值必须能在**某个**指标的封闭集合里找到
            assert any(
                value in allowed
                for signal in metrics.SIGNALS
                for allowed in signal.allowed_values.values()
            ), f"标签值 {label_key}={value!r} 不在任何受控集合内"
            seen += 1
    assert seen > 0, "快照里没有任何标签——那这道校验就没被真正执行"


def test_illegal_label_value_at_the_exit_would_raise():  # A13
    """出口一旦遇到集合外的值必须**失败**，而**不是**静默过滤掉。

    第一版把集合外的值直接丢掉——结果是"去掉封闭取值检查"这个变异照样通过
    （过滤条件本身也在用同一个集合）。现在改成显式失败：
    集合外的值意味着代码产出了一个不该出现的维度，那是缺陷，不该被悄悄掩盖。

    配套地，**未分类值必须有合法桶**（`unknown`），否则真实数据里的
    "尚未识别的取值"会把整个指标计算打挂。
    """
    assert metrics._stage_of("DRIFT_EXCEEDED") == "ood_drift"
    assert metrics._stage_of("INTERLOCK_DENIED") == "interlock"
    # 未知原因码不猜，归入受控的 unknown
    assert metrics._stage_of("SOME_FREE_TEXT_REASON") == "unknown"
    assert "unknown" in metrics.SIGNAL_BY_KEY["edge_rejection_total"].allowed_values["stage"]

    journal.append("edge-guard-decisions", {"verdict": "DENY", "reasons": ["SOME_FREE_TEXT_REASON"]})
    snapshot = metrics.compute_metrics()
    emitted = snapshot["signals"]["edge_rejection_total"]["by_label"]
    # 自由文本原因码**不得**以原样出现在标签里
    assert "SOME_FREE_TEXT_REASON" not in json.dumps(emitted, ensure_ascii=False)
    # 但它必须被计入受控的 unknown 桶（事实不因标签受控而丢失）
    assert emitted["unknown"]["labels"] == {"stage": "unknown"}
    assert snapshot["signals"]["edge_rejection_total"]["total"] == 1


def test_out_of_set_dimension_value_fails_loudly_instead_of_being_silently_dropped():  # A13
    """集合外的维度值必须让计算**失败**，不能"过滤掉当没发生"。

    这是上一版的洞：静默过滤让"去掉封闭取值检查"这个变异照样通过。
    """
    with pytest.raises(ValueError, match="不在受控集合内"):
        metrics._labelled_by_value("edge_rejection_total", "stage", {"totally_free_text": 1})


def test_snapshot_declares_environment_and_source():  # A13
    snapshot = metrics.compute_metrics()
    assert metrics.ENVIRONMENT == "mock", (
        "当前环境是 Mock；把 mock 写成生产会让指标被误读为生产就绪"
    )
    assert "labels_policy" in snapshot
    assert snapshot["backlog"] == 0


def test_metric_inventory_is_json_serialisable():  # A13
    """清单要能直接进运维端点响应，必须可序列化且不含函数对象。"""
    payload = json.dumps(metrics.metric_inventory(), ensure_ascii=False)
    assert "task_success_total" in payload
    assert len(metrics.metric_inventory()) == len(metrics.SIGNALS)
