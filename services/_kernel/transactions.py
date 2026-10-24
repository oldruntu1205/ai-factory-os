"""受控写入核心语义（P3，上游 §5 / §10.3 / §12）。

本模块只实现**与接口形态无关的判定逻辑**，供 transaction-service / tool-gateway /
control-plane 复用。上游对应的硬规则：

**批准绑定与 TOCTOU（§5）**
- 批准必须绑定：具体对象、**规范化 payload 哈希**、策略版本、动作范围、有效期。
- payload 改变 → 必须重新审批。
- **AUTHORIZED 不是永续许可**：EXECUTING 前必须重新验证批准有效、对象版本、当前权限。
- 规范化与哈希由**可信服务**完成，不能只信 Agent 提供的值。

**UNKNOWN 收敛（§5）**
- 超时 → UNKNOWN → 查源系统命令/凭证 → 已成功则记录成功 →
  **确认未执行才用同一幂等键重试** → 无法确认则人工接管。
- **禁止**：换新幂等键重发；直接补偿。
- 外部接口无幂等/状态查询能力时 → 适配器台账 + 串行对象锁 + 人工对账兜底，
  **不开放无人值守的高风险写入**。

**Saga 与补偿（§5）**
- 保存每步的前置状态、命令键、执行凭证、可补偿动作与补偿权限。
- MES/WMS 实物域与 ERP 账务域：**先核实并恢复实物状态，再执行账务冲销**。
- 已发货/已过账等阶段**必须分支**处理。
- **补偿是新的业务动作，不是数据库 rollback**。
- 补偿失败 → 人工队列。

**取消（§12）**
- 只能停止**未开始**步骤；在途或已完成副作用需确认及适用补偿。
- 不能把 Task 设为 CANCELLED 即假定撤销。

**租约与 fencing（§12）**
- worker 用租约与 fencing token，防止旧 worker 继续提交。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return time.mktime(time.strptime(value, fmt)) - time.timezone
        except ValueError:
            continue
    return None


# ==========================================================================
# 批准绑定与执行前复核（TOCTOU）
# ==========================================================================

# 与上游 §4.2 风险分级一致
RISK_CLASSES = ("R0", "R1", "R2", "R3")


class CheckOutcome(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"


@dataclass
class Refusal:
    """明确的拒绝原因集合（可安全暴露给调用方，不含业务正文）。"""

    reasons: list[str] = field(default_factory=list)

    def add(self, code: str) -> None:
        self.reasons.append(code)

    @property
    def blocked(self) -> bool:
        return bool(self.reasons)


def approval_binding_fingerprint(approval: dict[str, Any]) -> str:
    """批准绑定的规范形状。

    绑定必须覆盖：对象、payload 哈希、工具与版本、策略版本、动作范围。
    任何一项变化都必须重新批准——因此这里把它们拼成一个可比较的指纹。

    **只接受"绑定"本身**（`p3-approval.json#/$defs/Binding` 的那几个键）。
    不要传整个批准记录：记录顶层没有 `expected_version`，
    指纹会把它算成 None，造成"批准正确却被判绑定改变"（已实测）。
    """
    return "|".join(
        [
            str(approval.get("object_id")),
            str(approval.get("payload_hash")),
            str(approval.get("tool_id")),
            str(approval.get("tool_version")),
            str(approval.get("policy_version")),
            ",".join(sorted(approval.get("action_scope") or [])),
        ]
    )


def _normalize_approval(approval: dict[str, Any] | None) -> dict[str, Any] | None:
    """把批准事实记录或裸绑定统一成**裸绑定**形态（单一形状）。

    调用方可能传「批准事实记录」（含 `binding` 子对象），也可能只传绑定本身。
    两种形态都受欢迎，但**判定只认一种形状**——否则同一个批准会因为传参形态
    不同而得出不同结论（实测：整个记录传进去会缺 `expected_version`，
    被判成"绑定改变"这种误导性原因）。
    """
    if approval is None:
        return None
    nested = approval.get("binding")
    if isinstance(nested, dict):
        return {
            **nested,
            "approved": approval.get("approved"),
            "valid_until": approval.get("valid_until"),
            "approver_ref": approval.get("approver_ref"),
        }
    return approval


def revalidate_before_execution(
    *,
    command: dict[str, Any],
    approval: dict[str, Any] | None,
    current: dict[str, Any],
    tool: dict[str, Any],
    now: str | None = None,
) -> Refusal:
    """EXECUTING 前的再验证（§12：AUTHORIZED 不是永续许可）。

    `current` 是**执行时刻**的可信事实快照：对象版本、当前权限、策略是否仍有效。
    只要任一硬条件不成立就阻止执行，并给出可定位的原因。
    """
    refusal = Refusal()
    moment = _epoch(now or utc_now())
    tool_risk = tool.get("risk_class")

    approval = _normalize_approval(approval)

    # 0) 工具能力：无状态查询/补偿能力时，不得无人值守高风险写入（§5 末段）
    if tool_risk in ("R2", "R3"):
        if tool.get("idempotency_support") is not True or tool.get("status_query") is not True:
            refusal.add("TOOL_LACKS_IDEMPOTENCY_OR_STATUS_QUERY_FOR_UNATTENDED_WRITE")

    # 1) 风险等级必须由可信工具元数据得出，不接受命令自报
    if command.get("risk_class") and command.get("risk_class") != tool_risk:
        refusal.add("RISK_CLASS_MISMATCH_WITH_TRUSTED_METADATA")

    # 2) 需要批准的高风险动作：没有批准就不得执行
    if tool_risk in ("R2", "R3") and approval is None:
        refusal.add("APPROVAL_REQUIRED")
        return refusal

    if approval is not None:
        # 2a) 批准必须与当前命令**逐项绑定**（payload 改变需重新批准）。
        # 归一化之后 approval 本身即绑定形状，直接比较即可。
        if approval_binding_fingerprint(approval) != approval_binding_fingerprint(
            {
                "object_id": command.get("object_id"),
                "payload_hash": command.get("payload_hash"),
                "tool_id": command.get("tool_id"),
                "tool_version": command.get("tool_version"),
                "policy_version": command.get("policy_version"),
                "action_scope": command.get("action_scope"),
            }
        ):
            refusal.add("APPROVAL_BINDING_CHANGED")

        # 2b) 有效期（按可信时间判断，不做字符串比较）
        expiry = _epoch(approval.get("valid_until"))
        if expiry is None or (moment is not None and expiry <= moment):
            refusal.add("APPROVAL_EXPIRED")

        # 2c) 批准不得为"否决"
        if approval.get("approved") is not True:
            refusal.add("APPROVAL_NOT_GRANTED")

    # 3) 对象版本 CAS：执行时的对象版本必须与命令期望一致
    expected = command.get("expected_version")
    actual = current.get("object_version")
    if expected is not None and actual is not None and int(expected) != int(actual):
        refusal.add("OBJECT_VERSION_CONFLICT")

    # 4) 权限在执行时刻仍然有效（撤销即阻止）
    scope = set(command.get("required_scopes") or [])
    granted = set(current.get("granted_scopes") or [])
    missing = scope - granted
    if missing:
        refusal.add("PERMISSION_REVOKED")

    # 5) 策略版本在执行时刻仍然有效
    if current.get("policy_version") and command.get("policy_version") != current["policy_version"]:
        refusal.add("POLICY_VERSION_STALE")

    # 6) 对象状态不得处于"已过账/已发货"等不可直接覆盖的阶段
    if current.get("object_phase") in ("POSTED", "SHIPPED"):
        # 不是简单拒绝，而是要求走分支（补偿/冲销）路径
        refusal.add("OBJECT_PHASE_REQUIRES_BRANCHED_HANDLING")

    return refusal


# ==========================================================================
# UNKNOWN 收敛
# ==========================================================================

# §5 允许的下一步（白名单）
UNKNOWN_ALLOWED_ACTIONS = (
    "query_source_system",   # 查源系统命令/凭证
    "record_confirmed_success",  # 已成功则记录成功
    "retry_same_idempotency_key",  # 确认未执行才用**同一**键重试
    "manual_takeover",       # 无法确认则人工接管
)

# §5 明文禁止的动作（黑名单，与白名单互斥）
UNKNOWN_FORBIDDEN_ACTIONS = (
    "retry_with_new_idempotency_key",  # 换新键重发
    "direct_compensation",             # 直接补偿
)

# 源系统查询的结论。**词汇以契约为准**（p3-unknown.json 的 source_outcome 枚举），
# 不得自造：`UNKNOWN` = 查了但无法确认；`accepted` = 源系统只受理未执行。
SOURCE_STATES = ("SUCCEEDED", "NOT_EXECUTED", "UNKNOWN", "FAILED", "accepted")


def decide_unknown_convergence(
    *,
    source_state: str,
    same_key_retry_attempts: int,
    tool: dict[str, Any],
) -> dict[str, Any]:
    """UNKNOWN 的收敛决策表（§5 原文逐条实现）。

    返回 `action`（必须落在白名单）与 `forbidden`（始终给出，便于审计与验收断言）。
    """
    result: dict[str, Any] = {
        "source_state": source_state,
        "allowed_actions": list(UNKNOWN_ALLOWED_ACTIONS),
        "forbidden_actions": list(UNKNOWN_FORBIDDEN_ACTIONS),
    }

    if source_state == "SUCCEEDED":
        result["action"] = "record_confirmed_success"
        result["terminal"] = True
        result["note"] = "源系统已成功：记录成功，不得重发、不得补偿"
        return result

    if source_state == "NOT_EXECUTED":
        # 仅当确认未执行才允许用**同一**幂等键重试；且工具必须支持幂等与状态查询
        capable = tool.get("idempotency_support") is True and tool.get("status_query") is True
        if not capable or same_key_retry_attempts >= 3:
            result["action"] = "manual_takeover"
            result["terminal"] = True
            result["note"] = "工具无幂等/查询能力或重试已达上限：转人工，不换键、不补偿"
        else:
            result["action"] = "retry_same_idempotency_key"
            result["terminal"] = False
            result["note"] = "已确认未执行：用同一幂等键重试"
        return result

    if source_state == "FAILED":
        # 已确认失败：记录权威失败结论；是否重试/补偿由人工判断，不自动做
        result["action"] = "manual_takeover"
        result["terminal"] = True
        result["note"] = "源系统已确认失败：记录失败结论；重试与补偿需人工判断"
        return result

    if source_state in ("UNKNOWN", "accepted"):
        # `UNKNOWN` = 查了仍无法确认；`accepted` = 源系统只受理、未执行。
        # 两者都**不能**当成"已成功"或"确认未执行"，一律人工接管。
        result["action"] = "manual_takeover"
        result["terminal"] = True
        result["note"] = "无法确认是否执行：人工接管；不得换键重发、不得直接补偿"
        return result

    # 未知结论 → fail-closed，转人工
    result["action"] = "manual_takeover"
    result["terminal"] = True
    result["note"] = "源系统结论不可识别：fail-closed 转人工"
    result["invalid_source_state"] = True
    return result


# ==========================================================================
# Saga 步骤账与补偿
# ==========================================================================

# 恢复顺序：先实物域，再账务域（§5 明文）
#
# 词汇以契约为准（p3-saga.json 的 domain 枚举是**大写** PHYSICAL/ACCOUNTING）；
# 内核早期用小写，这里做**单向**归一化，避免"同一个域两种拼写"导致顺序判定失效。
DOMAIN_PHYSICAL = "PHYSICAL"
DOMAIN_ACCOUNTING = "ACCOUNTING"
DOMAIN_ORDER = {DOMAIN_PHYSICAL: 0, DOMAIN_ACCOUNTING: 1}
_DOMAIN_ALIASES = {
    "physical": DOMAIN_PHYSICAL,
    "PHYSICAL": DOMAIN_PHYSICAL,
    "ledger": DOMAIN_ACCOUNTING,
    "ACCOUNTING": DOMAIN_ACCOUNTING,
}


def normalize_domain(domain: str | None) -> str:
    """把历史小写拼写归一到契约枚举；未知域原样返回（由调用方 fail-closed）。"""
    return _DOMAIN_ALIASES.get(domain or "", domain or "")


# 阶段分支（§5：已发货/已过账等阶段必须分支处理）
PHASES = ("NOT_STARTED", "IN_PROGRESS", "EXECUTED", "SHIPPED", "POSTED")


@dataclass
class SagaStep:
    step_id: str
    domain: str                 # physical / ledger
    phase: str                  # PHASES
    command_id: str | None = None
    receipt_ref: str | None = None
    compensable: bool = True
    compensation_scope: list[str] = field(default_factory=list)
    compensable_action_ref: str | None = None


def compensation_order(steps: list[SagaStep]) -> list[str]:
    """补偿顺序：**实物域先于账务域**。

    域内按**逆序**回退（后做的先撤），符合"恢复前置状态"的直觉；
    跨域顺序不能颠倒——账务冲销必须建立在实物状态已核实恢复之上。
    """
    physical = [s for s in steps if normalize_domain(s.domain) == DOMAIN_PHYSICAL]
    accounting = [s for s in steps if normalize_domain(s.domain) == DOMAIN_ACCOUNTING]
    # 域内逆序回退（后做的先撤）；跨域顺序**不可颠倒**：账务冲销必须建立在实物已恢复之上
    ordered = list(reversed(physical)) + list(reversed(accounting))
    return [s.step_id for s in ordered]


def plan_compensation(steps: list[SagaStep]) -> dict[str, Any]:
    """生成补偿计划。

    关键语义：
    - 只补偿**已执行且可补偿**的步骤；
    - 已过账/已发货阶段不是"直接冲销"，而是走**分支**（各自的处理动作）；
    - 不可补偿的步骤进入人工队列，不能让 Saga 谎报 COMPENSATED。
    """
    plan: dict[str, Any] = {
        "order": [],
        "branches": [],
        "manual_review": [],
        "compensable": [],
        "recovery_order": [DOMAIN_PHYSICAL, DOMAIN_ACCOUNTING],
    }
    for step in steps:
        if step.phase in ("NOT_STARTED", "IN_PROGRESS"):
            # 未开始/在途：先确认状态，不直接补偿
            plan["manual_review"].append({"step_id": step.step_id, "reason": "STEP_NOT_TERMINAL"})
            continue
        if step.phase in ("SHIPPED", "POSTED"):
            # 已发生不可逆事实：走分支处理，而不是普通冲销
            plan["branches"].append(
                {
                    "step_id": step.step_id,
                    "domain": step.domain,
                    "phase": step.phase,
                    "handling": "RETURN_OR_CREDIT_NOTE" if step.phase == "SHIPPED" else "LEDGER_REVERSAL",
                    "requires_approval": True,
                }
            )
            continue
        if not step.compensable:
            plan["manual_review"].append({"step_id": step.step_id, "reason": "NOT_COMPENSABLE"})
            continue
        if not step.compensation_scope:
            # 可补偿但没有登记补偿权限 → 不得自行补偿
            plan["manual_review"].append({"step_id": step.step_id, "reason": "NO_COMPENSATION_SCOPE"})
            continue
        plan["compensable"].append(step.step_id)

    ordered = compensation_order([s for s in steps if s.step_id in set(plan["compensable"])])
    plan["order"] = ordered
    plan["requires_manual_review"] = bool(plan["manual_review"] or plan["branches"])
    return plan


def compensation_outcome(*, executed: list[str], failed: list[str]) -> dict[str, Any]:
    """补偿结果判定。补偿失败**不得**谎报 COMPENSATED。"""
    if failed:
        return {
            "saga_status": "MANUAL_REVIEW",
            "compensated_steps": list(executed),
            "failed_steps": list(failed),
            "note": "补偿失败进人工队列；不得对外宣称已完全补偿",
        }
    return {
        "saga_status": "COMPENSATED",
        "compensated_steps": list(executed),
        "failed_steps": [],
        "note": "全部可补偿步骤已补偿完成",
    }


# ==========================================================================
# 取消语义
# ==========================================================================

def plan_cancellation(*, steps: list[SagaStep]) -> dict[str, Any]:
    """取消只能停止**未开始**步骤；在途/已完成需确认与补偿。"""
    stoppable = [s.step_id for s in steps if s.phase == "NOT_STARTED"]
    needs_confirm = [s.step_id for s in steps if s.phase == "IN_PROGRESS"]
    needs_compensation = [s.step_id for s in steps if s.phase in ("EXECUTED", "SHIPPED", "POSTED")]
    return {
        "stoppable_steps": stoppable,
        "steps_needing_confirmation": needs_confirm,
        "steps_needing_compensation": needs_compensation,
        "task_status_may_be_set_cancelled": not (needs_confirm or needs_compensation),
        "note": (
            "只有未开始步骤被停止时才可把任务置为 CANCELLED；"
            "存在在途/已完成副作用时必须先确认并（视情况）补偿"
        ),
    }


# ==========================================================================
# 受控规范化与绑定哈希（§10.4 / §5：哈希由可信服务计算）
# ==========================================================================

def canonical_binding_material(
    *,
    tenant_id: str,
    object_id: str,
    expected_version: int,
    tool_id: str,
    tool_version: str,
    command_type: str,
    action_scope: list[str],
    payload_ref: str,
) -> dict[str, Any]:
    """规范化绑定的**唯一**输入形状。

    §5 明文：规范化与哈希由**可信服务**完成，不能只信 Agent 提供的值。
    因此这里把参与哈希的全部字段固定成一个有序结构，
    任何一侧（tool-gateway 计算 / transaction 复核）都必须用它重算，
    否则会出现"同一个 payload 算出两个哈希"而互相不认的情况。
    """
    return {
        "tenant_id": tenant_id,
        "object_id": object_id,
        "expected_version": int(expected_version),
        "tool_id": tool_id,
        "tool_version": tool_version,
        "command_type": command_type,
        "action_scope": sorted(action_scope),
        "payload_ref": payload_ref,
    }


def canonical_payload_hash(
    *,
    tenant_id: str,
    object_id: str,
    expected_version: int,
    tool_id: str,
    tool_version: str,
    command_type: str,
    action_scope: list[str],
    payload_ref: str,
) -> str:
    """计算规范化 payload 哈希（64 位小写十六进制 SHA256）。

    刻意**不含**任何时间戳或随机量：同一个绑定必须永远算出同一个哈希，
    否则幂等与"同键同 payload"判定会失效。
    """
    import hashlib
    import json as _json

    material = canonical_binding_material(
        tenant_id=tenant_id,
        object_id=object_id,
        expected_version=expected_version,
        tool_id=tool_id,
        tool_version=tool_version,
        command_type=command_type,
        action_scope=action_scope,
        payload_ref=payload_ref,
    )
    encoded = _json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ==========================================================================
# 执行前复核视图（§12 / p3-approval.json 的 Review）
# ==========================================================================

# 契约 Review 里必须逐项给出的布尔判定（allowed=false 时也要给，便于定位）
REVIEW_FLAGS = (
    "approval_valid",
    "payload_hash_matches",
    "policy_version_matches",
    "action_scope_matches",
    "object_version_matches",
    "current_permission_valid",
    "delegation_valid",
    "duties_separated",
    "capabilities_admitted",
    "lease_current",
    "fencing_current",
    "object_lock_held",
    "not_cancelled_before_start",
)

# 内部复核原因 → 契约 Review 的 reason_ref（必须是合法 Identifier，不能把枚举名直接塞进去）
REVIEW_REASON_REFS = {
    "APPROVAL_REQUIRED": "reason-approval-required",
    "APPROVAL_NOT_GRANTED": "reason-approval-not-granted",
    "APPROVAL_EXPIRED": "reason-approval-expired",
    "APPROVAL_BINDING_CHANGED": "reason-approval-binding-changed",
    "OBJECT_VERSION_CONFLICT": "reason-object-version-conflict",
    "PERMISSION_REVOKED": "reason-permission-revoked",
    "POLICY_VERSION_STALE": "reason-policy-version-stale",
    "TOOL_LACKS_IDEMPOTENCY_OR_STATUS_QUERY_FOR_UNATTENDED_WRITE": "reason-capability-not-admitted",
    "RISK_CLASS_MISMATCH_WITH_TRUSTED_METADATA": "reason-risk-class-mismatch",
    "OBJECT_PHASE_REQUIRES_BRANCHED_HANDLING": "reason-object-phase-branched",
    "CANCELLED_BEFORE_START": "reason-cancelled-before-start",
    "LEASE_NOT_HELD": "reason-lease-not-held",
    "LEASE_EXPIRED": "reason-lease-expired",
    "FENCING_TOKEN_STALE": "reason-fencing-stale",
    "DUTIES_NOT_SEPARATED": "reason-duties-not-separated",
}


def _lease_unexpired_static(expires_at: str | None, *, moment: float | None = None) -> bool:
    """租约是否未过期；解析失败一律视为过期（fail-closed）。

    **判定时刻必须由调用方传入**（`moment`），不要在这里读墙钟。

    实测缺陷（2026-10-05）：`review_command` 已按契约算出
    `moment = _epoch(now or utc_now())`，但本函数却读 `time.time()`——
    于是"传入的 now"对租约判定**完全不起作用**。
    测试夹具用固定常量当到期时间（`2026-10-05T00:00:00Z`），
    墙钟一走过该时刻，这条用例就**永久失败**，且与代码对不对无关。

    更普遍地说：判定函数读墙钟 ⇒ 结论不可复现，
    而"执行前复核"的结论是要被审计的。
    """
    expiry = _epoch(expires_at)
    if expiry is None:
        return False
    return expiry > (moment if moment is not None else time.time())


def review_command(
    *,
    command: dict[str, Any],
    approval: dict[str, Any] | None,
    current: dict[str, Any],
    tool: dict[str, Any],
    lease: dict[str, Any] | None,
    lease_ref: str | None,
    fencing_token: int | None,
    cancelled_before_start: bool,
    now: str | None = None,
) -> dict[str, Any]:
    """生成执行前复核结论（契约 `Review` 的逐项布尔判定）。

    与 `revalidate_before_execution` 的分工：
    - 后者是**判定内核**（返回拒绝原因列表），供 execute 这类要拒绝的路径用；
    - 本函数是**可展示、可审计的逐项结论**，供 control-plane 的复核接口用。

    两者必须给出一致的 allowed 结论，否则会出现"复核说可以、执行被拒"的怪象；
    因此最后会把内核给出的原因并入本结论。逐项独立计算、**不做短路**，
    以便排障时看到全部不成立项。
    """
    approval = _normalize_approval(approval)
    moment = _epoch(now or utc_now())
    approval_required = tool.get("risk_class") in ("R2", "R3")
    flags: dict[str, bool] = {name: True for name in REVIEW_FLAGS}
    reasons: list[str] = []

    # 1) 批准相关
    if approval is None:
        flags["approval_valid"] = not approval_required
        flags["payload_hash_matches"] = False
        flags["policy_version_matches"] = False
        flags["action_scope_matches"] = False
        if approval_required:
            reasons.append("APPROVAL_REQUIRED")
    else:
        expiry = _epoch(approval.get("valid_until"))
        flags["approval_valid"] = (
            approval.get("approved") is True and expiry is not None and (moment is None or expiry > moment)
        )
        if approval.get("approved") is not True:
            reasons.append("APPROVAL_NOT_GRANTED")
        elif expiry is None or (moment is not None and expiry <= moment):
            reasons.append("APPROVAL_EXPIRED")

        bound = approval
        expected = {
            "object_id": command.get("object_id"),
            "payload_hash": command.get("payload_hash"),
            "tool_id": command.get("tool_id"),
            "tool_version": command.get("tool_version"),
            "policy_version": command.get("policy_version"),
            "action_scope": command.get("action_scope"),
        }
        flags["payload_hash_matches"] = bound.get("payload_hash") == expected["payload_hash"]
        flags["policy_version_matches"] = bound.get("policy_version") == expected["policy_version"]
        flags["action_scope_matches"] = sorted(bound.get("action_scope") or []) == sorted(
            expected["action_scope"] or []
        )
        if approval_binding_fingerprint(bound) != approval_binding_fingerprint(expected):
            reasons.append("APPROVAL_BINDING_CHANGED")

    # 2) 对象版本（CAS）
    expected_version = command.get("expected_version")
    actual_version = current.get("object_version")
    flags["object_version_matches"] = (
        expected_version is None or actual_version is None or int(expected_version) == int(actual_version)
    )
    if not flags["object_version_matches"]:
        reasons.append("OBJECT_VERSION_CONFLICT")

    # 3) 当前权限仍然有效
    scope = set(command.get("required_scopes") or [])
    granted = set(current.get("granted_scopes") or [])
    flags["current_permission_valid"] = not (scope - granted)
    if not flags["current_permission_valid"]:
        reasons.append("PERMISSION_REVOKED")

    # 4) 策略版本（与批准绑定的一致性合并判断）
    if current.get("policy_version"):
        flags["policy_version_matches"] = (
            flags["policy_version_matches"] and command.get("policy_version") == current["policy_version"]
        )
    if not flags["policy_version_matches"] and "POLICY_VERSION_STALE" not in reasons:
        reasons.append("POLICY_VERSION_STALE")

    # 5) 委托与职责分离：审批人不得是发起人，且必须有可信权限证据
    approver = (approval or {}).get("approver_ref")
    requester = command.get("actor_id")
    if approval is None:
        flags["duties_separated"] = not approval_required
    else:
        flags["duties_separated"] = bool(approver) and approver != requester
    flags["delegation_valid"] = bool(current.get("permission_evidence_ref"))
    if not flags["duties_separated"] and approval is not None:
        reasons.append("DUTIES_NOT_SEPARATED")
    if not flags["delegation_valid"]:
        flags["current_permission_valid"] = False
        if "PERMISSION_REVOKED" not in reasons:
            reasons.append("PERMISSION_REVOKED")

    # 6) 工具能力准入（无幂等/查询能力 → 不得无人值守高风险写入）
    admission = write_admission(tool)
    flags["capabilities_admitted"] = admission["unattended_high_risk_write_allowed"]
    if not flags["capabilities_admitted"]:
        reasons.append("TOOL_LACKS_IDEMPOTENCY_OR_STATUS_QUERY_FOR_UNATTENDED_WRITE")

    # 7) 租约 / fencing / 对象锁 / 取消
    flags["lease_current"] = lease is not None and lease.get("lease_ref") == lease_ref
    if not flags["lease_current"]:
        reasons.append("LEASE_NOT_HELD")
    elif not _lease_unexpired_static(lease.get("expires_at"), moment=moment):
        reasons.append("LEASE_EXPIRED")
    flags["fencing_current"] = lease is not None and int(lease.get("fencing_token", -1)) == int(
        fencing_token if fencing_token is not None else -1
    )
    if flags["lease_current"] and not flags["fencing_current"]:
        reasons.append("FENCING_TOKEN_STALE")
    flags["object_lock_held"] = lease is not None and bool(lease.get("object_lock_ref"))
    flags["not_cancelled_before_start"] = not cancelled_before_start
    if cancelled_before_start:
        reasons.append("CANCELLED_BEFORE_START")

    # 与判定内核保持一致（内核提到的问题必须也反映在逐项结论里）
    for code in revalidate_before_execution(
        command=command, approval=approval, current=current, tool=tool, now=now
    ).reasons:
        if code not in reasons:
            reasons.append(code)

    allowed = not reasons
    return {
        "allowed": allowed,
        "flags": flags,
        "reasons": reasons,
        "reason_ref": None if allowed else REVIEW_REASON_REFS.get(reasons[0], "reason-review-failed"),
    }


# ==========================================================================
# 无人值守高风险写入的准入（§5 末段 / §10.3 / p3-tool-admission.json）
# ==========================================================================

# 能力位：三项必须**全部为 true** 才允许无人值守的高风险写入
REQUIRED_CAPABILITY_BITS = ("idempotency_support", "status_query", "compensation")

# 任一能力位缺失时强制走的兜底（§5：外部接口无幂等/状态查询能力时）
ADMISSION_FALLBACK = ("adapter_ledger", "serial_object_lock", "manual_reconciliation")


def write_admission(tool: dict[str, Any]) -> dict[str, Any]:
    """判定某工具是否允许**无人值守**的高风险写入。

    规则（契约 `x-admission-policy`）：
    - `unattended_high_risk_requires` 三项能力位；`any_false` →
      `deny_unattended_high_risk_write`；
    - 但**无论允许与否**，适配器台账、串行对象锁、人工对账都是必须的兜底；
    - 能力位为 false **不是**"降级成功"，更不是"可以重试到成功"；
    - 能力位来自**可信登记**（`tool-spec.json`），不由 Agent 填写。

    容易误解的一点：`compensation=false` 不代表"不能写"，
    而是"不能**无人值守**地写"——有补偿能力才允许在无人干预下承担高风险副作用。
    """
    missing = [bit for bit in REQUIRED_CAPABILITY_BITS if tool.get(bit) is not True]
    risk_class = tool.get("risk_class")
    high_risk = risk_class in ("R2", "R3")
    allowed = not missing

    if missing:
        reason_ref = "reason-missing-capability-" + "-".join(sorted(missing))
    elif not high_risk:
        reason_ref = "reason-low-risk-read-or-prepare"
    else:
        reason_ref = None

    return {
        "unattended_high_risk_write_allowed": allowed,
        "adapter_ledger_required": True,
        "serial_object_lock_required": True,
        "manual_reconciliation_required": True,
        "missing_capabilities": missing,
        "risk_class": risk_class,
        "high_risk": high_risk,
        "reason_ref": reason_ref,
        "note": (
            "三项能力位不全时必须拒绝无人值守高风险写入，并走适配器台账/串行对象锁/人工对账兜底"
            if missing
            else "能力位齐备；仍需串行对象锁与人工对账兜底"
        ),
    }


# ==========================================================================
# 租约与 fencing
# ==========================================================================

@dataclass
class Lease:
    worker_id: str
    fencing_token: int
    expires_at: str

    def valid(self, *, worker_id: str, fencing_token: int, now: str | None = None) -> bool:
        """旧 worker 携带旧 fencing token 时拒绝提交。"""
        if worker_id != self.worker_id or fencing_token != self.fencing_token:
            return False
        expiry = _epoch(self.expires_at)
        moment = _epoch(now or utc_now())
        if expiry is None or moment is None:
            return False
        return expiry > moment


def acquire_lease(*, current: Lease | None, worker_id: str, ttl_seconds: int, now: str | None = None) -> Lease:
    """取租约：**fencing token 单调递增**，保证旧 worker 的提交会被拒。"""
    moment = _epoch(now or utc_now()) or time.time()
    next_token = (current.fencing_token + 1) if current else 1
    return Lease(
        worker_id=worker_id,
        fencing_token=next_token,
        expires_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment + ttl_seconds)),
    )
