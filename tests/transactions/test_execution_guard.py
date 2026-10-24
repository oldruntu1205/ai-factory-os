"""事务边界：执行前再验证 与 UNKNOWN 收敛（上游 §12 / §6.3 / §17 A05、A06）。

## 为什么单独一个目录

上游 §15 把 `tests/transactions/` 列为**独立边界**；本仓此前把它放在
`tests/p3_transactions/` 之下（按阶段组织）。本组把"事务"这条边界显式化。

已有覆盖**不迁移**（避免两份真相）。本组**只补原来没有的**：

- `tests/p3_runtime` 覆盖的是**端到端流程**（走 HTTP、造真实命令与租约）；
- 本组覆盖 `services/_kernel/transactions.py` 的**纯函数判据**——
  它俩查的是不同的东西：流程绿了不代表每条拒绝理由都还在。

## 为什么钉这一层

`transactions.py` 是**副作用能否发生**的最后一道闸（上游 §12 原文：
「AUTHORIZED 不是永续许可，EXECUTING 前必须重新验证批准有效、对象版本和当前权限」）。
它一旦有一条判据失效，端到端用例**可能仍然是绿的**——因为流程里
未必恰好构造出那个条件。所以每条拒绝码都值得**被单独钉住**。

本组把上游点名的四类失效各钉一条：
**旧审批 / payload 变更 / 权限撤销 / 对象版本变动**（A06 原文的四项）。
"""

from __future__ import annotations

from services._kernel import transactions

# ==========================================================================
# 夹具：一个"本来应当放行"的命令与执行时刻快照
# ==========================================================================

NOW = "2026-10-05T00:00:00Z"
FUTURE = "2026-12-31T00:00:00Z"
PAST = "2026-01-01T00:00:00Z"


def _tool(**overrides) -> dict:
    """可信工具元数据（R2 且具备幂等与状态查询）。"""
    tool = {
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
        "risk_class": "R2",
        "idempotency_support": True,
        "status_query": True,
    }
    tool.update(overrides)
    return tool


def _approval(**overrides) -> dict:
    """一份与 `_command()` 逐项绑定的批准。"""
    approval = {
        "object_id": "schedule-demo",
        "payload_hash": "a" * 64,
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
        "policy_version": "policy-demo",
        "action_scope": "dispatch.publish",
        "approved": True,
        "valid_until": FUTURE,
    }
    approval.update(overrides)
    return approval


def _command(**overrides) -> dict:
    command = {
        "object_id": "schedule-demo",
        "payload_hash": "a" * 64,
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
        "policy_version": "policy-demo",
        "action_scope": "dispatch.publish",
        "expected_version": 6,
        "risk_class": "R2",
        "required_scopes": ["pmc.dispatch.publish"],
    }
    command.update(overrides)
    return command


def _current(**overrides) -> dict:
    """执行时刻的可信事实快照。"""
    current = {
        "object_version": 6,
        "granted_scopes": ["pmc.dispatch.publish"],
        "policy_version": "policy-demo",
        "object_phase": "DRAFT",
    }
    current.update(overrides)
    return current


def _refusals(**kwargs) -> list[str]:
    return list(transactions.revalidate_before_execution(**kwargs).reasons)


def test_baseline_command_is_allowed():  # A06
    """**先证明基线是放行的** —— 否则下面每条"拒绝"都可能是假绿。

    这是本文件最重要的一条：如果基线本来就因为别的原因被拒，
    那"改了 X 之后被拒"就不能归因到 X。夹具必须先跑通。
    """
    refusal = transactions.revalidate_before_execution(
        command=_command(), approval=_approval(), current=_current(), tool=_tool(), now=NOW
    )
    assert refusal.reasons == [], f"基线应当放行，实际拒绝理由：{refusal.reasons}"


def test_stale_approval_blocks_execution():  # A06
    """**旧审批**阻止执行（A06 第一项）。"""
    reasons = _refusals(
        command=_command(), approval=_approval(valid_until=PAST),
        current=_current(), tool=_tool(), now=NOW,
    )
    assert "APPROVAL_EXPIRED" in reasons


def test_missing_valid_until_is_a_refusal_not_a_pass():  # A06
    """批准**没有有效期**也必须拒绝，不能当成"永久有效"。

    fail-open 的典型形态：字段缺失时默认放行。上游 §6.2 要求批准
    必须绑定「具体对象、规范化 payload 哈希、策略版本、动作范围**及有效期**」。
    """
    approval = _approval()
    approval.pop("valid_until")
    reasons = _refusals(
        command=_command(), approval=approval, current=_current(), tool=_tool(), now=NOW
    )
    assert "APPROVAL_EXPIRED" in reasons, "缺 valid_until 必须按过期处理（fail-closed）"


def test_payload_change_invalidates_approval():  # A06
    """**payload 变更**阻止执行（A06 第二项）。

    上游 §6.2：「payload 改变需重新审批」。
    """
    reasons = _refusals(
        command=_command(payload_hash="b" * 64), approval=_approval(),
        current=_current(), tool=_tool(), now=NOW,
    )
    assert "APPROVAL_BINDING_CHANGED" in reasons


def test_object_version_change_blocks_execution():  # A06
    """**对象版本变动**阻止执行（A06 第四项，TOCTOU）。

    批准时对象是版本 6，执行时已变成 7 ⇒ 不能再按旧判断执行。
    """
    reasons = _refusals(
        command=_command(expected_version=6), approval=_approval(),
        current=_current(object_version=7), tool=_tool(), now=NOW,
    )
    assert "OBJECT_VERSION_CONFLICT" in reasons


def test_revoked_permission_blocks_execution():  # A06
    """**权限撤销**阻止执行（A06 第三项）。"""
    reasons = _refusals(
        command=_command(), approval=_approval(),
        current=_current(granted_scopes=[]), tool=_tool(), now=NOW,
    )
    assert "PERMISSION_REVOKED" in reasons


def test_denied_approval_blocks_execution():  # A06
    """被**否决**的批准不得被执行（`approved` 必须为 True）。"""
    reasons = _refusals(
        command=_command(), approval=_approval(approved=False),
        current=_current(), tool=_tool(), now=NOW,
    )
    assert "APPROVAL_NOT_GRANTED" in reasons


def test_stale_policy_version_blocks_execution():  # A06
    """策略版本在执行时刻已变更 ⇒ 阻止（旧策略下的判断不再有效）。"""
    reasons = _refusals(
        command=_command(policy_version="policy-v1"), approval=_approval(policy_version="policy-v1"),
        current=_current(policy_version="policy-v2"), tool=_tool(), now=NOW,
    )
    assert "POLICY_VERSION_STALE" in reasons


def test_r2_tool_without_status_query_blocks_unattended_write():  # A05
    """R2 工具**没有状态查询能力** ⇒ 不得无人值守写入（上游 §5 末段）。

    理由：没有状态查询就无法在超时后确认"到底执行没有"，
    于是必然落到 UNKNOWN，而 UNKNOWN 的收敛需要查源系统——
    **能力缺失必须在这里拦住，而不是等到超时后才发现没法收敛。**
    """
    reasons = _refusals(
        command=_command(), approval=_approval(), current=_current(),
        tool=_tool(status_query=False), now=NOW,
    )
    assert "TOOL_LACKS_IDEMPOTENCY_OR_STATUS_QUERY_FOR_UNATTENDED_WRITE" in reasons


def test_risk_class_must_come_from_trusted_tool_metadata():  # A06
    """风险等级**不接受命令自报**（上游 §7：「由可信工具元数据与业务规则计算」）。

    命令自称 R0（只读）而工具元数据是 R2（业务写入）⇒ 必须拒绝。
    这是"降低风险等级来绕过审批"的直接防线。
    """
    reasons = _refusals(
        command=_command(risk_class="R0"), approval=_approval(),
        current=_current(), tool=_tool(risk_class="R2"), now=NOW,
    )
    assert "RISK_CLASS_MISMATCH_WITH_TRUSTED_METADATA" in reasons


def test_r3_without_approval_is_refused():  # A06
    """R3（高风险）没有批准 ⇒ 直接拒绝（并短路返回）。"""
    refusal = transactions.revalidate_before_execution(
        command=_command(risk_class="R3"), approval=None,
        current=_current(), tool=_tool(risk_class="R3"), now=NOW,
    )
    assert "APPROVAL_REQUIRED" in refusal.reasons


def test_posted_object_requires_branched_handling():  # A08
    """对象已过账/已发货 ⇒ 不得直接覆盖，须走**分支处置**（补偿/冲销）。

    上游 §6.3：「已发货、已过账等阶段必须分支处理。补偿是新的业务动作，不是数据库 rollback。」
    """
    reasons = _refusals(
        command=_command(), approval=_approval(),
        current=_current(object_phase="POSTED"), tool=_tool(), now=NOW,
    )
    assert "OBJECT_PHASE_REQUIRES_BRANCHED_HANDLING" in reasons


# ==========================================================================
# UNKNOWN 收敛决策表（§6.3）
# ==========================================================================

def test_unknown_never_allows_a_new_idempotency_key_or_direct_compensation():  # A05
    """无论源系统怎么回答，**换新键重发**与**直接补偿**永远在禁止名单里。

    这是 A05 的核心，也是本组唯一一条**遍历所有源状态**的用例：
    §6.3 原文「**禁止** UNKNOWN 状态换新键重发或直接补偿」——
    "禁止"是无条件的，所以必须对每个源状态都成立，而不是只对某一个。
    """
    for source_state in transactions.SOURCE_STATES:
        decision = transactions.decide_unknown_convergence(
            source_state=source_state, same_key_retry_attempts=0, tool=_tool()
        )
        # 键名以源码为准：`forbidden_actions`（不是 `forbidden`）
        forbidden = set(decision["forbidden_actions"])
        assert "retry_with_new_idempotency_key" in forbidden, (
            f"源状态 {source_state} 下允许了换新键重发——这是 §6.3 明文禁止的"
        )
        assert "direct_compensation" in forbidden, (
            f"源状态 {source_state} 下允许了直接补偿——这是 §6.3 明文禁止的"
        )


def test_unknown_action_is_always_within_the_allowed_whitelist():  # A05
    """收敛给出的 `action` **必须**落在白名单内（不得自造动作）。

    上游 §6.3 只给了四条下一步。允许自造动作等于给"绕过"留了口子。
    """
    for source_state in transactions.SOURCE_STATES:
        for attempts in (0, 1, 5):
            decision = transactions.decide_unknown_convergence(
                source_state=source_state, same_key_retry_attempts=attempts, tool=_tool()
            )
            assert decision["action"] in transactions.UNKNOWN_ALLOWED_ACTIONS, (
                f"源状态 {source_state}（第 {attempts} 次）给出了白名单外的动作："
                f"{decision['action']!r}；白名单={transactions.UNKNOWN_ALLOWED_ACTIONS}"
            )


def test_whitelist_and_blacklist_are_disjoint():  # A05
    """"允许"与"禁止"两套词表**不得有交集**。

    交集意味着同一条动作既被允许又被禁止——那种契约是自相矛盾的，
    真出现时无论代码选哪边都能被说成"符合契约"。
    """
    overlap = set(transactions.UNKNOWN_ALLOWED_ACTIONS) & set(
        transactions.UNKNOWN_FORBIDDEN_ACTIONS
    )
    assert not overlap, f"允许与禁止词表存在交集：{overlap}"


def test_source_states_match_the_contract_vocabulary():  # A05
    """源状态词表**以契约为准**，不得自造。

    `SOURCE_STATES` 必须与 `p3-unknown.json` 里 `source_outcome` 的枚举一致——
    代码与契约各写一份词表，是"两套真相"最常见的产地。
    """
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "packages/contracts/schemas/p3-unknown.json").read_text(encoding="utf-8")
    )
    text = json.dumps(schema, ensure_ascii=False)
    for state in transactions.SOURCE_STATES:
        assert f'"{state}"' in text, (
            f"源码词表里的 {state!r} 不在契约 p3-unknown.json 中——"
            "词表必须以契约为唯一来源"
        )
