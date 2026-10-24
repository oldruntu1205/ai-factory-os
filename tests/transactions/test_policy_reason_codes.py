"""策略判定的原因码必须**取自策略自己的目录**（上游 §10.2 / §3）。

## 这条用例钉住一次真实缺陷（2026-10-05）

`_handler_policy_evaluate` 里曾经写着：

```python
"reason_code": "reason-demo" if allowed else "reason-demo",
```

**两个分支同一个值**——等于没有区分，却长得像区分了。

而且 `reason-demo` 属于 `decision_catalog`，是**另一个决策类型**的目录。
契约把 `reason_code` 声明为 `CatalogValue`（`x-catalog-required: true`，
须取自有限目录），策略判定借用任务路由的目录，会有两个后果：

1. **审计分不清**一条原因码出自哪类判定；
2. **一边扩目录会意外放宽另一边**（目录变成两千多行里都合法的字符串）。

## 守三件事

1. `allowed` / `approval_required` / 拒绝 **三类情形的原因码互不相同** ——
   否则"区分"只写在注释里；
2. 每个原因码都**在 registry 的策略目录里登记过** —— 不是硬编码字面量；
3. 目录缺失时 **fail-closed**（拒绝），不得回退到某个"默认原因码"。
"""

from __future__ import annotations

import uuid

from tests.p0_runtime.conftest import REPO_ROOT, context_body

# 身份头从 p15 夹具取（与各运行时套件同源），不自己再写一份字面量
from tests.p15_runtime.conftest import IDENTITY

POLICY_END = "/internal/v1/policy/evaluate"


def _evaluate(cluster, *, risk_class: str, tool_id: str, delegated_scopes: list[str], trace: str):
    """调用策略判定端点，返回 (status, payload)。"""
    context = context_body(trace_id=trace)
    return cluster.post(
        "control-plane",
        POLICY_END,
        {
            **context,
            "context": context,
            "tool_id": tool_id,
            "tool_version": "1.0.0-mock",
            "risk_class": risk_class,
            "object_id": "schedule-demo",
            "delegated_scopes": delegated_scopes,
        },
        headers=IDENTITY,
    )


def _registered_reason_codes() -> set[str]:
    """registry 里策略声明的原因码（**唯一权威**）。"""
    import json

    registry = json.loads(
        (REPO_ROOT / "services/_kernel/registry.mock.json").read_text(encoding="utf-8")
    )
    codes: set[str] = set()
    for policy in (registry.get("policies") or {}).values():
        declared = policy.get("reason_codes")
        if isinstance(declared, dict):
            codes.update(v for v in declared.values() if isinstance(v, str))
    return codes


def test_reason_codes_distinguish_the_three_outcomes(cluster):  # A06
    """可达的两类情形必须给出**互不相同**的原因码。

    这里刻意只覆盖**可达**的情形（上游 §7 的 R0 与 R2）：
    允许与需批准**都不是拒绝**，但它们对使用者意味着完全不同的下一步
    （直接执行 vs 去走审批），所以不能共用一个码。

    `denied` 分支在当前 `policy-demo` 下不可达（没有哪条规则
    同时 `auto_execute=false` 且 `requires_approval=false`），
    因此**不假装覆盖它**——目录里为它留了原因码，等真有规则时再验。
    """
    trace = f"trace-pol-{uuid.uuid4().hex[:8]}"

    # erp.stock.query 是 R0、auto_execute=true，且**委派范围覆盖它要求的 scope**
    # （`required_scopes=['erp.read']`）⇒ allowed。
    # ⚠️ scope 没给够会导致 `allowed=False`（missing → approval_required），
    # 那不是"策略不允许",而是"这个人没这个权限"——两者原因码不同，别混。
    allowed = _evaluate(cluster, risk_class="R0", tool_id="erp.stock.query",
                        delegated_scopes=["erp.read"], trace=trace)
    assert allowed[0] == 200, allowed
    assert allowed[1]["allowed"] is True, f"R0 应被允许：{allowed[1]}"

    # pmc.dispatch.publish 是 R2 且策略 requires_approval=true ⇒ 需批准
    needs = _evaluate(cluster, risk_class="R2", tool_id="pmc.dispatch.publish",
                      delegated_scopes=["pmc.dispatch.publish"], trace=trace)
    assert needs[0] == 200, needs
    assert needs[1]["approval_required"] is True, f"R2 应需批准：{needs[1]}"

    codes = {allowed[1]["reason_code"], needs[1]["reason_code"]}
    assert len(codes) == 2, (
        "「允许」与「需批准」给出了同一个原因码——等于没有区分。"
        f"实际：allowed={allowed[1]['reason_code']!r} "
        f"approval_required={needs[1]['reason_code']!r}"
    )


def test_missing_scope_and_policy_approval_share_the_reason_code(cluster):  # A06
    """**权限不足**与**策略要求批准**都归 `approval_required` 原因码。

    这条记录一个**刻意的取舍**：上游要求"权限、审批、事务分别治理"，
    但在此处两者都表现为"不能直接执行、要走流程"，因此共用一个原因码。
    把它写成用例，是为了让这个取舍**是显式的**——将来若要区分，
    会先在这里看到"当初为什么没区分"。
    """
    trace = f"trace-pol-scope-{uuid.uuid4().hex[:8]}"
    status, payload = _evaluate(
        cluster, risk_class="R0", tool_id="erp.stock.query",
        delegated_scopes=["pmc.read"],  # 刻意**不含** erp.read（工具要求的那个）
        trace=trace,
    )
    assert status == 200, payload
    assert payload["allowed"] is False
    assert payload["approval_required"] is True, (
        "缺少必需 scope 时 approval_required 应为 True（不得静默放行）"
    )


def test_reason_codes_are_all_registered_in_the_policy_catalog(cluster):  # A06
    """每个原因码都必须在 registry 策略目录里登记（不得是硬编码字面量）。"""
    registered = _registered_reason_codes()
    assert registered, "registry 的 policies[*].reason_codes 不应为空"

    trace = f"trace-pol-reg-{uuid.uuid4().hex[:8]}"
    seen: set[str] = set()
    for risk_class, tool_id, scopes in (
        ("R0", "erp.stock.query", ["erp.read"]),
        ("R2", "pmc.dispatch.publish", ["pmc.dispatch.publish"]),
    ):
        status, payload = _evaluate(
            cluster, risk_class=risk_class, tool_id=tool_id, delegated_scopes=scopes, trace=trace
        )
        assert status == 200, payload
        seen.add(payload["reason_code"])

    unregistered = seen - registered
    assert not unregistered, (
        f"策略判定给出了目录外的原因码：{sorted(unregistered)}；"
        f"目录={sorted(registered)}。契约要求 reason_code 是 CatalogValue（有限目录，未知即拒绝）。"
    )


def test_policy_reason_code_catalog_is_not_the_decision_catalog():  # A06
    """策略判定的原因码目录**不得**与决策目录共用同一个值集合。

    共用会让"这条原因码出自哪类判定"在审计时无法区分。
    判据取**目录本身**（策略声明的原因码 ≠ 决策目录的原因码），
    而不是看某次响应——响应相同可能是巧合，目录相同才是设计问题。
    """
    import json

    registry = json.loads(
        (REPO_ROOT / "services/_kernel/registry.mock.json").read_text(encoding="utf-8")
    )
    decision_codes = set((registry.get("decision_catalog") or {}).get("reason_codes") or [])
    policy_codes = _registered_reason_codes()

    assert policy_codes, "策略应有自己的原因码目录"
    assert not (policy_codes & decision_codes), (
        "策略目录与决策目录共用了原因码："
        f"{sorted(policy_codes & decision_codes)}——应各自登记，便于审计归因"
    )
