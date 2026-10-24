"""Guard 的共享判据必须**真的接在营业路径上**（上游 §9 / §17 A09）。

## 这条用例防的是什么（一次真实事故的同类风险）

2026-10-05 发生了一次事故：变异脚本被硬杀，`apps/web-admin/src/approval.js`
留下了 `if (false) {`，把「说明文字不能当授权」这条判据**整条禁用**。
当时没被发现，因为**测试测的是判据函数本身，而失效的是调用点**。

> **判据存在 ≠ 判据被使用。**

本仓实测同一形态的存量：`packages/domain/guard.py` 里的
`highest_priority_stage()` 与 `reason_requires_fallback()` **有单测、
但生产代码引用次数为 0** —— 因为 `services/_kernel/edge.py::evaluate_guard`
**内联重写**了同一套判定（自己建 `order` 字典排序、自己比 `REASONS_REQUIRING_FALLBACK`）。

后果不是"功能坏了"（两份当时等价），而是：

- **两份判据会各自演进**，而单测绿不代表生产跟着变；
- 用户改 `guard.py`（那份**有测试、看起来是权威**的）时，
  生产行为**纹丝不动** —— 这正是最坏的一种"改对了没生效"。

现在两处合成一处。下面几条把**接线本身**钉住：谁把它退回内联实现，谁就报红。
"""

from __future__ import annotations

import inspect

from services._kernel import edge

# ==========================================================================
# ① 接线是结构事实（防"退回内联实现"）
# ==========================================================================

def test_evaluate_guard_calls_the_shared_predicates():  # A09
    """`evaluate_guard` 必须**调用**共享判据，而不是自己再写一份。"""
    source = inspect.getsource(edge.evaluate_guard)

    assert "highest_priority_stage(" in source, (
        "`evaluate_guard` 必须用 `packages/domain/guard.highest_priority_stage` "
        "求最高优先级阶段——自己内联排序会让两份判据分叉"
    )
    assert "reason_requires_fallback(" in source, (
        "`evaluate_guard` 必须用 `reason_requires_fallback` 判定回退需求，"
        "而不是自己比对原因码集合"
    )


def test_evaluate_guard_does_not_reinline_the_predicates():  # A09
    """反向断言：不得把**回退判据**内联回去。

    只写"必须调用"是不够的——可以两者并存（调一次给日志用，
    真正生效的还是内联那份）。所以这里明确禁止内联形态。

    注意**不禁止** `GUARD_STAGE_INDEX`：排序用它是对的
    （`highest_priority_stage` 内部也用它）；要禁的是判据本身。
    """
    source = inspect.getsource(edge.evaluate_guard)
    code_lines = [
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)

    assert "REASONS_REQUIRING_FALLBACK" not in code, (
        "不得在 `evaluate_guard` 里直接比对 `REASONS_REQUIRING_FALLBACK`——"
        "那是内联重写了 `reason_requires_fallback` 的判据"
    )
    assert "reason_requires_fallback(" in code, "回退判据必须来自共享实现"


def test_guard_module_no_longer_exports_an_unused_fallback_set():  # A09
    """`edge` 不应再从 guard 导入**未被使用**的原因码集合。"""
    source = inspect.getsource(edge)
    assert "REASONS_REQUIRING_FALLBACK," not in source.split("def evaluate_guard")[0], (
        "`REASONS_REQUIRING_FALLBACK` 已不再被 edge 使用，不应保留导入"
        "（留着会让下一个读者以为判据在这里比对）"
    )


# ==========================================================================
# ② 接线是行为事实（端到端跑一遍判定）
# ==========================================================================

def _guard_input(**overrides):
    """一个"全部通过"的 Guard 输入（字段取自 `GuardInput` 的真实定义）。

    `input_at` 与 `now` 都取**同一个时刻**，让新鲜度检查得到 age=0。
    不设 `input_at` 会得到 `INPUT_TIMESTAMP_UNPARSABLE`——
    那是"时间戳没给"，与"数据陈旧"是两回事，别混。
    """
    moment = edge.utc_now()
    base = {
        "input_at": moment,
        "now": moment,
        "max_age_seconds": 5,
        "interlock_passed": True,
        "budget_exceeded": False,
        "confidence": 0.99,
        "confidence_threshold": 0.5,
        "confidence_is_calibrated": True,
    }
    base.update(overrides)
    return edge.GuardInput(**base)


def test_top_stage_follows_the_upstream_stage_order():  # A09
    """`top_stage` 必须按上游硬顺序给出**最先失败的那一步**。

    构造"结构问题 **先发现顺序在后**、但阶段在前"的场景：
    置信度问题在代码里最后检查，结构问题在最后才被触发（缺失必填特征）。
    结论必须遵循**阶段顺序**，不是发现顺序。
    """
    verdict = edge.evaluate_guard(
        _guard_input(
            required_features=("pressure",),
            features={},  # 缺必填特征 ⇒ structure 阶段失败
            confidence_is_calibrated=False,  # ⇒ confidence 阶段失败（最后检查）
        )
    )
    # `structure` 在 GUARD_STAGES 里早于 `confidence` ⇒ 必须是 structure
    assert verdict["top_stage"] == "structure", verdict
    assert verdict["verdict"] == edge.Verdict.DENY, verdict
    # 两条都在，且顺序按阶段排
    assert set(verdict["reasons"]) == {"MISSING_FIELD:pressure", "CONFIDENCE_NOT_CALIBRATED"}, verdict


def test_confidence_is_still_last_priority_after_wiring():  # A09
    """**置信度必须仍是最后一步**——接线不能改动这条语义。"""
    verdict = edge.evaluate_guard(_guard_input())
    assert verdict["confidence_is_last_priority"] is True, verdict
    assert verdict["stage_order"][-1] == "confidence", verdict


def test_interlock_still_cannot_be_overridden_by_human_approval():  # A09
    """硬互锁仍不可被人工批准覆盖（接线不能松掉这条）。"""
    verdict = edge.evaluate_guard(_guard_input(interlock_passed=False))
    assert verdict["interlock_overrides_approval"] is True, verdict
    assert "interlock" in verdict["approval_cannot_override"], verdict


def test_all_allow_when_nothing_fails():  # A09
    """反向自检：全通过时必须 ALLOW——防止判据被接成"总是拒绝"。"""
    verdict = edge.evaluate_guard(_guard_input())
    assert verdict["verdict"] == edge.Verdict.ALLOW, verdict
    assert verdict["reasons"] == [], verdict
    assert verdict["top_stage"] is None, verdict
    assert verdict["top_reason"] is None, verdict
