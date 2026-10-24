"""P3 受控写入的核心语义验收（与接口形态无关的判定逻辑）。

覆盖上游 §5 / §10.3 / §12 里可以完全用 Mock 验证的部分：
批准绑定与 TOCTOU、UNKNOWN 收敛、Saga 补偿顺序与失败收敛、取消语义、租约与 fencing。

与 `tests/p15_events` 的分工：那边是"事件一致性"，这边是"受控写入判定"。
"""

from __future__ import annotations

import time

import pytest

from services._kernel import transactions as tx

UTC = "2026-10-05T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


class _Clock:
    """固定"现在"，让有效期判定可复现（UTC ISO8601）。"""

    def __init__(self, value: str = "2026-10-04T12:00:00Z") -> None:
        self.value = value

    def now(self) -> str:
        return self.value


# ==========================================================================
# 补偿"是否凭已验证回执完成"的判据（上游 §5 / §6 / A08）
# ==========================================================================
#
# 这块判据来自一次真实缺陷（2026-10-05）：补偿处理器曾**直接写上一份
# `authority_verified: True` 的回执**，并把步骤标成 SUCCEEDED，
# 于是"什么都没做"的补偿被算作完成、整单报 COMPENSATED。
#
# 之前**没有**任何用例考过"回执无效时该判否"——所以下面这三条
# 不是补充说明，而是补上了那块空白（变异测试正是靠它们才能报红）。


def _compensation(*, command_status="SUCCEEDED", receipt=None, command_id="command-comp-1"):
    return {
        "command_id": command_id,
        "command_status": command_status,
        "authority_receipt": receipt,
    }


def _receipt(*, command_id="command-comp-1", verified=True):
    return {
        "command_id": command_id,
        "authority_verified": verified,
        "business_success": True,
        "posting_status": "NOT_CONFIRMED",
    }


def test_compensation_verified_requires_terminal_success():  # A08
    """没到终态就不算完成——AUTHORIZED 只是受理（这正是修复后的受理态）。"""
    from services._kernel import routes as routes

    assert routes._compensation_receipt_is_verified(
        _compensation(command_status="AUTHORIZED", receipt=_receipt())
    ) is False


def test_compensation_verified_requires_verified_receipt():  # A08
    """**没有回执、或回执未经权威验证，都不得算完成。**

    这条直接对应被修掉的缺陷：旧实现里那份回执是凭空写的。
    """
    from services._kernel import routes as routes

    # 无回执
    assert routes._compensation_receipt_is_verified(
        _compensation(receipt=None)
    ) is False
    # 回执存在但未经验证
    assert routes._compensation_receipt_is_verified(
        _compensation(receipt=_receipt(verified=False))
    ) is False


def test_compensation_receipt_must_belong_to_the_compensation_command():  # A08
    """回执的 `command_id` 必须**就是**补偿命令——否则核不上账。

    真实缺陷：旧实现里回执的 command_id 是另起一个 uuid4 生成的，
    与步骤自己的补偿命令 id 不同；照着回执去查命令会查不到。
    """
    from services._kernel import routes as routes

    assert routes._compensation_receipt_is_verified(
        _compensation(command_id="command-comp-1", receipt=_receipt(command_id="command-comp-OTHER"))
    ) is False


def test_compensation_verified_happy_path():  # A08
    """三项都成立时才算完成（反向自检：防止判据过严而永远判否）。"""
    from services._kernel import routes as routes

    assert routes._compensation_receipt_is_verified(
        _compensation(command_status="SUCCEEDED", receipt=_receipt())
    ) is True


def test_compensation_verified_tolerates_missing_or_malformed_input():  # A08
    """缺字段/非字典一律判否，不得抛异常（判据是 fail-closed）。"""
    from services._kernel import routes as routes

    for value in (None, {}, "not-a-dict", {"command_status": "SUCCEEDED", "authority_receipt": "x"}):
        assert routes._compensation_receipt_is_verified(value) is False


# ==========================================================================
# 两处顺序口径必须一致（防止漂移）
# ==========================================================================
#
# 实测发现：`transactions.plan_compensation`（纯逻辑，有单测）
# **从未被任何 HTTP 路由调用**；Saga 的实际顺序判定写在
# `_handler_p3_saga_compensate` 里，是**另一处实现**。
#
# 当前两处口径一致（物理域先于账务域），所以**不是缺陷**。
# 风险在于：两处会各自演进、悄悄漂移，而只有一处真正生效——
# 于是"单测说顺序对"与"实际顺序对"可能不再等价。
#
# 这两条断言把两处**钉在一起**：谁先改，谁就会在这里报红。


def test_physical_domain_precedes_accounting_in_both_places():  # A08
    """纯逻辑与实际生效的顺序判据必须给出**同一结论**。

    两处口径：
    - 契约层：`transactions.plan_compensation()["recovery_order"]`；
    - 生效层：Saga 视图的 `recovery_order`（由路由构造，`_p3_saga_view`）。
    """
    from pathlib import Path

    from services._kernel import transactions as tx

    # ① 契约层的顺序
    steps = [
        tx.SagaStep(step_id="acc", domain="ACCOUNTING", phase="EXECUTED"),
        tx.SagaStep(step_id="phy", domain="PHYSICAL", phase="EXECUTED"),
    ]
    declared = tx.plan_compensation(steps)["recovery_order"]
    assert declared == [tx.DOMAIN_PHYSICAL, tx.DOMAIN_ACCOUNTING], (
        f"契约层的恢复顺序应为实物域先于账务域；实际 {declared}"
    )

    # ② 生效层：路由里那条判据
    #    （"账务域且实物未核实恢复 ⇒ 拒绝"意味着实物必须先做）
    source = (
        Path(__file__).resolve().parents[2] / "services" / "_kernel" / "routes.py"
    ).read_text(encoding="utf-8")
    assert "PHYSICAL_RESTORE_REQUIRED" in source, (
        "路由侧缺少「实物未恢复则拒绝账务补偿」的判据——两处口径已经漂移"
    )
    assert "DOMAIN_PHYSICAL" in source and "DOMAIN_ACCOUNTING" in source, (
        "路由侧不再区分两个域——域的顺序语义已丢失"
    )


def test_recovery_order_shape_matches_the_contract():  # A08
    """生效层的 `recovery_order` 必须是**两项对象**，且顺序固定。

    契约用 `prefixItems` 固定为两项对象（不是字符串数组）。
    若有人把它改成字符串数组，这条会报红——那是契约违规。
    """
    import inspect

    from services._kernel import routes

    view_source = inspect.getsource(routes._p3_saga_view)
    assert '"recovery_order"' in view_source, "Saga 视图必须给出 recovery_order"
    assert '"domain": transactions.DOMAIN_PHYSICAL' in view_source, (
        "recovery_order 第一项必须是实物域（契约固定两域顺序）"
    )
    assert '"domain": transactions.DOMAIN_ACCOUNTING' in view_source, (
        "recovery_order 第二项必须是账务域"
    )


CLOCK = _Clock()


def command(**overrides) -> dict:
    base = {
        "command_id": "command-demo",
        "object_id": "schedule-demo",
        "payload_hash": "sha256:payload",
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
        "policy_version": "policy-demo",
        "action_scope": ["pmc.dispatch.publish"],
        "expected_version": 6,
        "risk_class": "R2",
        "required_scopes": ["pmc.dispatch.publish"],
    }
    base.update(overrides)
    return base


def approval(**overrides) -> dict:
    base = {
        "approved": True,
        "object_id": "schedule-demo",
        "payload_hash": "sha256:payload",
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
        "policy_version": "policy-demo",
        "action_scope": ["pmc.dispatch.publish"],
        "valid_until": UTC,
    }
    base.update(overrides)
    return base


def current(**overrides) -> dict:
    base = {
        "object_version": 6,
        "granted_scopes": ["pmc.dispatch.publish"],
        "policy_version": "policy-demo",
        "object_phase": "NOT_STARTED",
    }
    base.update(overrides)
    return base


def tool(**overrides) -> dict:
    base = {
        "tool_id": "pmc.dispatch.publish",
        "risk_class": "R2",
        "idempotency_support": True,
        "status_query": True,
        "compensation": False,
    }
    base.update(overrides)
    return base


# ==========================================================================
# 一、批准绑定与 TOCTOU（A06）
# ==========================================================================

def test_happy_path_allows_execution():  # A06
    refusal = tx.revalidate_before_execution(
        command=command(), approval=approval(), current=current(), tool=tool(), now=CLOCK.now()
    )
    assert refusal.blocked is False, refusal.reasons


def test_payload_change_requires_new_approval():  # A06
    """批准后 payload 变了 → 绑定指纹变化 → 阻止执行。"""
    refusal = tx.revalidate_before_execution(
        command=command(payload_hash="sha256:changed"),
        approval=approval(),
        current=current(),
        tool=tool(),
        now=CLOCK.now(),
    )
    assert "APPROVAL_BINDING_CHANGED" in refusal.reasons


@pytest.mark.parametrize(
    "field,value",
    [
        ("object_id", "schedule-other"),
        ("tool_id", "pmc.other.tool"),
        ("tool_version", "2.0.0"),
        ("policy_version", "policy-other"),
        ("action_scope", ["other.scope"]),
    ],
)
def test_every_binding_dimension_is_enforced(field, value):  # A06
    """绑定的每一维都必须被核验：对象/payload/工具/版本/策略/动作范围。"""
    refusal = tx.revalidate_before_execution(
        command=command(**{field: value}), approval=approval(), current=current(), tool=tool(), now=CLOCK.now()
    )
    assert refusal.blocked is True, f"{field} 变化未被检测"


def test_expired_approval_is_blocked_by_clock():  # A06
    refusal = tx.revalidate_before_execution(
        command=command(), approval=approval(valid_until=PAST), current=current(), tool=tool(), now=CLOCK.now()
    )
    assert "APPROVAL_EXPIRED" in refusal.reasons


def test_rejected_approval_is_blocked():  # A06
    refusal = tx.revalidate_before_execution(
        command=command(), approval=approval(approved=False), current=current(), tool=tool(), now=CLOCK.now()
    )
    assert "APPROVAL_NOT_GRANTED" in refusal.reasons


def test_high_risk_without_approval_is_blocked():  # A06
    refusal = tx.revalidate_before_execution(
        command=command(), approval=None, current=current(), tool=tool(), now=CLOCK.now()
    )
    assert "APPROVAL_REQUIRED" in refusal.reasons


def test_object_version_conflict_blocks_execution():  # A06
    """TOCTOU：批准后对象版本被别人改了 → CAS 失败。"""
    refusal = tx.revalidate_before_execution(
        command=command(expected_version=6), approval=approval(), current=current(object_version=7),
        tool=tool(), now=CLOCK.now(),
    )
    assert "OBJECT_VERSION_CONFLICT" in refusal.reasons


def test_permission_revoked_after_approval_blocks_execution():  # A06
    """TOCTOU：批准后被撤权 → 执行前复核发现 → 阻止。"""
    refusal = tx.revalidate_before_execution(
        command=command(), approval=approval(), current=current(granted_scopes=[]), tool=tool(), now=CLOCK.now()
    )
    assert "PERMISSION_REVOKED" in refusal.reasons


def test_stale_policy_version_blocks_execution():  # A06
    refusal = tx.revalidate_before_execution(
        command=command(), approval=approval(), current=current(policy_version="policy-newer"),
        tool=tool(), now=CLOCK.now(),
    )
    assert "POLICY_VERSION_STALE" in refusal.reasons


def test_posted_or_shipped_object_requires_branched_handling():  # A06
    for phase in ("POSTED", "SHIPPED"):
        refusal = tx.revalidate_before_execution(
            command=command(), approval=approval(), current=current(object_phase=phase), tool=tool(), now=CLOCK.now()
        )
        assert "OBJECT_PHASE_REQUIRES_BRANCHED_HANDLING" in refusal.reasons


def test_risk_class_cannot_be_self_reported():  # A11
    """风险等级必须来自可信工具元数据，命令自报与之不符即拒绝。"""
    refusal = tx.revalidate_before_execution(
        command=command(risk_class="R1"), approval=approval(), current=current(), tool=tool(risk_class="R2"),
        now=CLOCK.now(),
    )
    assert "RISK_CLASS_MISMATCH_WITH_TRUSTED_METADATA" in refusal.reasons


def test_tool_without_idempotency_or_status_query_cannot_be_used_unattended():  # A06
    """§5：无幂等/状态查询能力的工具，不开放无人值守高风险写入。"""
    refusal = tx.revalidate_before_execution(
        command=command(),
        approval=approval(),
        current=current(),
        tool=tool(idempotency_support=False, status_query=False),
        now=CLOCK.now(),
    )
    assert "TOOL_LACKS_IDEMPOTENCY_OR_STATUS_QUERY_FOR_UNATTENDED_WRITE" in refusal.reasons


# ==========================================================================
# 二、UNKNOWN 收敛（A05）
# ==========================================================================

def test_unknown_confirmed_success_records_without_resending():  # A06
    decision = tx.decide_unknown_convergence(
        source_state="SUCCEEDED", same_key_retry_attempts=0, tool=tool()
    )
    assert decision["action"] == "record_confirmed_success"
    assert decision["terminal"] is True


def test_unknown_confirmed_not_executed_retries_with_same_key():  # A06
    decision = tx.decide_unknown_convergence(
        source_state="NOT_EXECUTED", same_key_retry_attempts=0, tool=tool()
    )
    assert decision["action"] == "retry_same_idempotency_key"
    assert decision["terminal"] is False


def test_unknown_unconfirmed_goes_to_manual():  # A06
    decision = tx.decide_unknown_convergence(
        source_state="UNKNOWN", same_key_retry_attempts=0, tool=tool()
    )
    assert decision["action"] == "manual_takeover"
    assert decision["terminal"] is True


def test_unknown_never_allows_new_key_or_direct_compensation():  # A05
    """§5 明文禁止：UNKNOWN 不得换新键重发、不得直接补偿。"""
    for state in ("SUCCEEDED", "NOT_EXECUTED", "UNKNOWN", "SOMETHING_ODD"):
        decision = tx.decide_unknown_convergence(
            source_state=state, same_key_retry_attempts=0, tool=tool()
        )
        assert decision["action"] in tx.UNKNOWN_ALLOWED_ACTIONS
        assert decision["action"] not in tx.UNKNOWN_FORBIDDEN_ACTIONS
        assert set(tx.UNKNOWN_FORBIDDEN_ACTIONS) == set(decision["forbidden_actions"])


def test_unknown_retry_stops_at_limit_and_escalates():  # A06
    decision = tx.decide_unknown_convergence(
        source_state="NOT_EXECUTED", same_key_retry_attempts=3, tool=tool()
    )
    assert decision["action"] == "manual_takeover"


def test_unknown_with_incapable_tool_goes_to_manual():  # A06
    decision = tx.decide_unknown_convergence(
        source_state="NOT_EXECUTED",
        same_key_retry_attempts=0,
        tool=tool(idempotency_support=False),
    )
    assert decision["action"] == "manual_takeover"


def test_unknown_with_unrecognized_source_state_fails_closed():  # A06
    decision = tx.decide_unknown_convergence(
        source_state="WEIRD", same_key_retry_attempts=0, tool=tool()
    )
    assert decision["action"] == "manual_takeover"
    assert decision["invalid_source_state"] is True


# ==========================================================================
# 三、Saga 补偿顺序与收敛（A08）
# ==========================================================================

def step(step_id: str, domain: str, phase: str, **overrides) -> tx.SagaStep:
    base = {
        "step_id": step_id,
        "domain": domain,
        "phase": phase,
        "command_id": f"command-{step_id}",
        "receipt_ref": f"receipt-{step_id}",
        "compensable": True,
        "compensation_scope": ["compensate.demo"],
    }
    base.update(overrides)
    return tx.SagaStep(**base)


def test_compensation_order_is_physical_before_ledger():  # A08
    """§5：先核实并恢复实物状态，再执行账务冲销。顺序不可颠倒。"""
    steps = [
        step("s1", "physical", "EXECUTED"),
        step("s2", "ledger", "EXECUTED"),
        step("s3", "physical", "EXECUTED"),
    ]
    plan = tx.plan_compensation(steps)
    assert plan["order"], "应有可补偿步骤"
    first_ledger = next((i for i, sid in enumerate(plan["order"]) if sid == "s2"), None)
    physical_indexes = [i for i, sid in enumerate(plan["order"]) if sid in ("s1", "s3")]
    assert first_ledger is not None
    assert max(physical_indexes) < first_ledger, "所有实物域步骤必须排在账务域之前"


def test_compensation_within_domain_is_reverse_order():  # A06
    steps = [step("a", "physical", "EXECUTED"), step("b", "physical", "EXECUTED")]
    assert tx.plan_compensation(steps)["order"] == ["b", "a"]


def test_posted_and_shipped_steps_go_to_branches_not_plain_reversal():  # A06
    """已发货/已过账必须分支处理，而不是普通冲销。"""
    steps = [
        step("ship", "physical", "SHIPPED"),
        step("post", "ledger", "POSTED"),
        step("exec", "physical", "EXECUTED"),
    ]
    plan = tx.plan_compensation(steps)
    handling = {item["step_id"]: item["handling"] for item in plan["branches"]}
    assert handling["ship"] == "RETURN_OR_CREDIT_NOTE"
    assert handling["post"] == "LEDGER_REVERSAL"
    assert all(item["requires_approval"] for item in plan["branches"])
    assert plan["compensable"] == ["exec"], "只有普通已执行步骤走常规补偿"
    assert plan["requires_manual_review"] is True


def test_non_terminal_steps_are_not_compensated_directly():  # A06
    steps = [step("pending", "physical", "IN_PROGRESS"), step("none", "physical", "NOT_STARTED")]
    plan = tx.plan_compensation(steps)
    assert plan["compensable"] == []
    reasons = {item["reason"] for item in plan["manual_review"]}
    assert reasons == {"STEP_NOT_TERMINAL"}


def test_non_compensable_or_scope_missing_goes_to_manual():  # A06
    steps = [
        step("nosup", "physical", "EXECUTED", compensable=False),
        step("noscope", "physical", "EXECUTED", compensation_scope=[]),
    ]
    plan = tx.plan_compensation(steps)
    assert plan["compensable"] == []
    reasons = {item["reason"] for item in plan["manual_review"]}
    assert reasons == {"NOT_COMPENSABLE", "NO_COMPENSATION_SCOPE"}


def test_compensation_failure_never_claims_compensated():  # A08
    outcome = tx.compensation_outcome(executed=["a"], failed=["b"])
    assert outcome["saga_status"] == "MANUAL_REVIEW"
    assert "b" in outcome["failed_steps"]

    success = tx.compensation_outcome(executed=["a", "b"], failed=[])
    assert success["saga_status"] == "COMPENSATED"


# ==========================================================================
# 四、取消语义（§12）
# ==========================================================================

def test_cancellation_only_stops_unstarted_steps():  # A06
    steps = [
        step("notstarted", "physical", "NOT_STARTED"),
        step("inflight", "physical", "IN_PROGRESS"),
        step("done", "physical", "EXECUTED"),
        step("shipped", "physical", "SHIPPED"),
    ]
    plan = tx.plan_cancellation(steps=steps)
    assert plan["stoppable_steps"] == ["notstarted"]
    assert plan["steps_needing_confirmation"] == ["inflight"]
    assert set(plan["steps_needing_compensation"]) == {"done", "shipped"}
    assert plan["task_status_may_be_set_cancelled"] is False


def test_cancellation_may_set_cancelled_when_nothing_started():  # A06
    steps = [step("a", "physical", "NOT_STARTED"), step("b", "ledger", "NOT_STARTED")]
    plan = tx.plan_cancellation(steps=steps)
    assert plan["task_status_may_be_set_cancelled"] is True


# ==========================================================================
# 五、租约与 fencing（§12）
# ==========================================================================

def test_fencing_token_is_monotonic_and_old_worker_is_rejected():  # A06
    lease1 = tx.acquire_lease(current=None, worker_id="w1", ttl_seconds=60, now=CLOCK.now())
    assert lease1.fencing_token == 1
    lease2 = tx.acquire_lease(current=lease1, worker_id="w2", ttl_seconds=60, now=CLOCK.now())
    assert lease2.fencing_token == 2

    # 旧 worker 携带旧 token 提交 → 拒绝
    assert lease2.valid(worker_id="w1", fencing_token=1, now=CLOCK.now()) is False
    # 新 worker 用当前 token → 通过
    assert lease2.valid(worker_id="w2", fencing_token=2, now=CLOCK.now()) is True
    # 过期租约 → 拒绝
    expired = tx.Lease(worker_id="w2", fencing_token=2, expires_at=PAST)
    assert expired.valid(worker_id="w2", fencing_token=2, now=CLOCK.now()) is False


def test_kernel_and_review_view_agree_and_accept_both_approval_shapes():  # A06
    """防回归：判定内核与逐项复核视图必须给出**一致**的 allowed 结论。

    实测事故：内核把**整个批准记录**（而非绑定本身）传进指纹函数，
    记录顶层没有 `expected_version`，指纹算成 None，于是"批准完全正确"
    却被判成 `APPROVAL_BINDING_CHANGED`——逐项 flags 全 True、结论却拒绝。
    这条用例把两种批准形态与两个入口的结论都钉在一起。
    """
    binding = {
        "object_id": "schedule-demo",
        "payload_hash": "sha256:payload",
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
        "policy_version": "policy-demo",
        "action_scope": ["pmc.dispatch.publish"],
    }
    approval_fact = {
        "approved": True,
        "binding": dict(binding),
        "valid_until": UTC,
        "approver_ref": "user-approver",
    }
    bare_binding = {**binding, "approved": True, "valid_until": UTC, "approver_ref": "user-approver"}
    cmd = {
        **binding,
        "actor_id": "user-demo",
        "expected_version": 6,
        "risk_class": "R2",
        "required_scopes": ["pmc.dispatch.publish"],
    }
    current_snapshot = {
        "object_version": 6,
        "granted_scopes": ["pmc.dispatch.publish"],
        "policy_version": "policy-demo",
        "object_phase": "NOT_STARTED",
        "permission_evidence_ref": "permission-user-demo",
    }
    tool_spec = tool(compensation=True)
    lease = {"lease_ref": "l1", "fencing_token": 1, "expires_at": UTC, "object_lock_ref": "lock-x"}

    # 两种批准形态都必须被接受（传错形态不得表现为"绑定改变"）
    for approval_input in (approval_fact, bare_binding):
        kernel = tx.revalidate_before_execution(
            command=cmd, approval=approval_input, current=current_snapshot, tool=tool_spec, now=CLOCK.now()
        )
        assert kernel.blocked is False, (approval_input.keys(), kernel.reasons)

    # 内核与逐项视图结论必须一致
    kernel = tx.revalidate_before_execution(
        command=cmd, approval=approval_fact, current=current_snapshot, tool=tool_spec, now=CLOCK.now()
    )
    view = tx.review_command(
        command=cmd, approval=approval_fact, current=current_snapshot, tool=tool_spec,
        lease=lease, lease_ref="l1", fencing_token=1, cancelled_before_start=False, now=CLOCK.now(),
    )
    assert view["allowed"] is True, view["reason_ref"]
    assert kernel.blocked == (not view["allowed"])

    # 反向：payload 变了 → 两边都必须拒绝，且原因一致
    changed = dict(cmd, payload_hash="sha256:changed")
    kernel2 = tx.revalidate_before_execution(
        command=changed, approval=approval_fact, current=current_snapshot, tool=tool_spec, now=CLOCK.now()
    )
    view2 = tx.review_command(
        command=changed, approval=approval_fact, current=current_snapshot, tool=tool_spec,
        lease=lease, lease_ref="l1", fencing_token=1, cancelled_before_start=False, now=CLOCK.now(),
    )
    assert "APPROVAL_BINDING_CHANGED" in kernel2.reasons
    assert view2["allowed"] is False
    assert view2["reason_ref"] == "reason-approval-binding-changed"


def test_forbidden_and_allowed_action_sets_are_disjoint():  # A06
    """契约层要能断言"禁止动作"，因此两组集合必须互斥且非空。"""
    assert set(tx.UNKNOWN_ALLOWED_ACTIONS) & set(tx.UNKNOWN_FORBIDDEN_ACTIONS) == set()
    assert "retry_with_new_idempotency_key" in tx.UNKNOWN_FORBIDDEN_ACTIONS
    assert "direct_compensation" in tx.UNKNOWN_FORBIDDEN_ACTIONS


# ==========================================================================
# 六、无人值守高风险写入准入（p3-tool-admission.json）
# ==========================================================================

def test_high_risk_tool_missing_any_capability_denies_unattended_write():  # A06
    """三项能力位只要缺一个，就不得无人值守地执行高风险写入。"""
    for missing_bit in tx.REQUIRED_CAPABILITY_BITS:
        spec = tool(idempotency_support=True, status_query=True, compensation=True)
        spec[missing_bit] = False
        admission = tx.write_admission(spec)
        assert admission["unattended_high_risk_write_allowed"] is False, f"{missing_bit}=false 仍被放行"
        assert missing_bit in admission["missing_capabilities"]
        assert admission["reason_ref"], "拒绝必须给出可追溯的原因引用"


def test_admission_always_requires_ledger_lock_and_manual_reconciliation():  # A06
    """§5：无论能力位是否齐备，台账/串行对象锁/人工对账兜底都必须保留。"""
    for spec in (
        tool(idempotency_support=True, status_query=True, compensation=True),
        tool(idempotency_support=False, compensation=False),
    ):
        admission = tx.write_admission(spec)
        assert admission["adapter_ledger_required"] is True
        assert admission["serial_object_lock_required"] is True
        assert admission["manual_reconciliation_required"] is True


def test_all_capabilities_true_allows_unattended_high_risk_write():  # A06
    admission = tx.write_admission(tool(idempotency_support=True, status_query=True, compensation=True))
    assert admission["unattended_high_risk_write_allowed"] is True
    assert admission["missing_capabilities"] == []


def test_low_risk_tool_is_still_classified_low_risk():  # A06
    """低风险工具仍必须被正确判定为"非高风险"。

    注意：`unattended_high_risk_write_allowed` 这一位**不因风险等级而放松** ——
    契约（`p3-tool-admission.json` 的 `allOf/then` 与 L1 断言）要求
    **任一能力位为 false 就必须为 false**，无论风险等级。
    它的语义是"允许无人值守地执行写入"，低风险但缺幂等/查询能力的工具
    同样不能无人值守地写；真正区分高低风险的是 `high_risk` 判定本身
    （决定是否触发审批要求，见 `revalidate_before_execution`）。
    """
    admission = tx.write_admission(
        tool(risk_class="R0", idempotency_support=False, status_query=False, compensation=False)
    )
    assert admission["high_risk"] is False, "R0 不应被判为高风险"
    assert admission["unattended_high_risk_write_allowed"] is False, "缺能力位一律不得放行无人值守写入"
    # 拒绝原因应当指向"能力位缺失"，而不是"风险等级过高"
    assert "missing-capability" in admission["reason_ref"]


def test_low_risk_tool_with_full_capabilities_is_allowed():  # A06
    admission = tx.write_admission(
        tool(risk_class="R0", idempotency_support=True, status_query=True, compensation=True)
    )
    assert admission["high_risk"] is False
    assert admission["unattended_high_risk_write_allowed"] is True


def test_missing_capability_is_not_treated_as_success():  # A06
    """能力位为 false 绝不等于"降级成功"：必须是明确的拒绝语义。"""
    admission = tx.write_admission(tool(idempotency_support=False))
    assert admission["unattended_high_risk_write_allowed"] is False
    assert "missing" in admission["reason_ref"]


def test_lease_expiry_follows_the_injected_now_not_the_wall_clock():  # A06
    """租约判定必须用**传入的 now**，不得读墙钟。

    **这是一条回归断言，来自一次真实缺陷（2026-10-05）**：
    `review_command` 早已按契约算出 `moment = _epoch(now or utc_now())`，
    但租约判过期却读 `time.time()`——于是"传入的 now"对它毫无作用。
    夹具里用固定常量 `2026-10-05T00:00:00Z` 当到期时间，
    墙钟一走过该时刻，`test_kernel_and_review_view_agree_and_accept_both_approval_shapes`
    就**永久失败**，且与代码对不对无关（表象像是"跑得快/慢"）。

    判据故意取**两侧极端时刻**：只要实现读墙钟，两次调用必得同一结果，断言即失败。
    """
    expires_at = "2026-10-05T00:00:00Z"

    # 传入的 now 远早于到期 ⇒ 未过期
    assert tx._lease_unexpired_static(expires_at, moment=1_700_000_000.0) is True
    # 传入的 now 远晚于到期 ⇒ 已过期
    assert tx._lease_unexpired_static(expires_at, moment=4_100_000_000.0) is False
    # 解析失败一律视为过期（fail-closed），与 now 无关
    assert tx._lease_unexpired_static("not-a-timestamp", moment=1_700_000_000.0) is False
    assert tx._lease_unexpired_static(None, moment=1_700_000_000.0) is False
