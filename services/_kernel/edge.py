"""Edge-RLCD 现场判定核心（P4，上游 §8 / §9 / §12）。

本模块只实现**与接口形态无关的判定逻辑**，供 Edge Gateway / Guard / spool 复用。
之所以先做这一层：上游 §9 把"判定优先级"写得很硬，
而这类顺序约束最容易在实现里被悄悄打乱（顺序一乱，安全结论就错）。

**判定优先级（§9，硬顺序，必须高于置信度）**
缺失字段 → NaN → 越界 → 单位不明 → 超时 → OOD → 漂移 → 版本不匹配 → 互锁拒绝，
**全部优先于置信度**。因此本模块把"结构性/安全类问题"与"置信度不足"分成两类：
前者是 DENY，后者也是 DENY，但**原因不同且必须可定位**；顺序体现在 `reasons[0]`。

**不可覆盖项**
- 硬互锁拒绝**不能被人工批准覆盖**（§9 明文）。
- 置信度**不能**是 LLM 自报数字：只接受"经独立数据校准的任务级指标"（§9）。
- 安全回退动作**由设备工程师逐设备定义**，模型不得即兴选择（§9）。

**影子模式**
控制写入**物理/网络禁用**，高置信度也不能产生真实动作（§9、A09）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ==========================================================================
# 发布状态机（§12）
#
# **词表与迁移规则来自 `packages/domain/release.py`**（上游 §15：纯领域约束放共享包）。
# 这里只做再导出，保证既有调用方与测试不受影响——**不复制一份定义**。
# ==========================================================================

from packages.domain import behavior_change as _behavior_change
from packages.domain import release as _release
from packages.domain.guard import (
    GUARD_REASON_TO_STAGE,
    GUARD_STAGE_INDEX,
    GUARD_STAGE_ORDER,
    GUARD_STAGES,
    highest_priority_stage,
    reason_requires_fallback,
    stage_of_reason,
)

EDGE_RELEASE_STATES = _release.EDGE_RELEASE_STATES
EDGE_RELEASE_ORDER = _release.EDGE_RELEASE_ORDER
BEHAVIORAL_CHANGE_KINDS = _behavior_change.BEHAVIORAL_CHANGE_KINDS
BEHAVIORAL_CHANGE_REQUIRED_STAGE = _behavior_change.BEHAVIORAL_CHANGE_REQUIRED_STAGE


def release_transition_allowed(current: str, target: str) -> bool:
    """只允许**相邻推进**；REVOKED 是终态，不能再出发。（规则来自 domain）"""
    return _release.release_transition_allowed(current, target)


def behavioral_change_impact(changed_kinds: list[str]) -> dict[str, Any]:
    """评估行为性变化的影响（规则来自 domain）。"""
    return _behavior_change.behavioral_change_impact(
        changed_kinds, order=EDGE_RELEASE_ORDER
    )


# ==========================================================================
# Guard 判定（§9 的判定优先级）
#
# **阶段与原因码词表来自 `packages/domain/guard.py`**（见文件顶部的再导出）。
# 这里不再重复定义——两份定义一定会漂移，而漂移的表现是
# "某条判定被归到一个不存在的阶段"。观测侧（`metrics.py`）也改为从该包导入。
# ==========================================================================

class Verdict:
    ALLOW = "ALLOW"
    DENY = "DENY"
    FALLBACK = "FALLBACK"


@dataclass
class GuardInput:
    """Guard 的全部输入。字段名与契约一致，便于直接映射。"""

    features: dict[str, Any] = field(default_factory=dict)
    # 结构性校验所需：必填特征名与各自允许区间/单位
    required_features: tuple[str, ...] = ()
    feature_bounds: dict[str, tuple[float, float]] = field(default_factory=dict)
    feature_units: dict[str, str] = field(default_factory=dict)
    expected_units: dict[str, str] = field(default_factory=dict)

    input_at: str | None = None
    max_age_seconds: int = 5
    now: str | None = None

    in_applicability_domain: bool = True
    drift_within_threshold: bool = True

    model_version: str | None = None
    expected_model_version: str | None = None
    schema_version: str | None = None
    expected_schema_version: str | None = None
    calibration_ref: str | None = None
    calibration_valid: bool = True

    signature_policy_valid: bool = True
    policy_expires_at: str | None = None
    license_valid: bool = True

    device_id: str | None = None
    allowed_devices: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    parameter_boundaries: dict[str, tuple[float, float]] = field(default_factory=dict)

    interlock_passed: bool = True

    budget_exceeded: bool = False

    confidence: float | None = None
    confidence_threshold: float | None = None
    confidence_is_calibrated: bool = True

    # 人工批准：**不能覆盖硬互锁**
    approval_present: bool = False


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_nan(value: Any) -> bool:
    try:
        return isinstance(value, float) and math.isnan(value)
    except (TypeError, ValueError):
        return False


def _seconds_between(earlier: str | None, later: str | None) -> float | None:
    if not earlier or not later:
        return None
    parsed = []
    for value in (earlier, later):
        try:
            parsed.append(time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)
        except ValueError:
            return None
    return parsed[1] - parsed[0]


def evaluate_guard(guard: GuardInput) -> dict[str, Any]:
    """按 §9 的**硬顺序**逐阶段判定，返回可定位的结论。

    设计要点：
    - 逐阶段**不短路**：把所有失败项都收集起来，便于排障（但优先级由顺序保证）；
    - `reasons[0]` 永远是**最高优先级**的那一项，因此判据可断言；
    - 置信度永远排在最后：**它不能覆盖前面任何一项**；
    - 硬互锁失败时，即使有有效批准也照样拒（人工批准不能覆盖硬互锁）。
    """
    failures: list[tuple[str, str]] = []  # (stage, reason)

    # 1) 结构性：缺失 / NaN / 越界 / 单位不明
    for name in guard.required_features:
        if name not in guard.features or _is_missing(guard.features.get(name)):
            failures.append(("structure", f"MISSING_FIELD:{name}"))
    for name, value in guard.features.items():
        if isinstance(value, float) and math.isnan(value):
            failures.append(("structure", f"NAN_VALUE:{name}"))
    for name, (low, high) in guard.feature_bounds.items():
        actual = guard.features.get(name)
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            if actual < low or actual > high:
                failures.append(("structure", f"OUT_OF_RANGE:{name}"))
    for name, expected_unit in guard.expected_units.items():
        actual_unit = guard.feature_units.get(name)
        if actual_unit is not None and actual_unit != expected_unit:
            failures.append(("structure", f"UNIT_UNKNOWN_OR_MISMATCH:{name}"))

    # 2) 新鲜度 / 超时
    age = _seconds_between(guard.input_at, guard.now or utc_now())
    if age is None:
        failures.append(("freshness_timeout", "INPUT_TIMESTAMP_UNPARSABLE"))
    elif age > guard.max_age_seconds:
        failures.append(("freshness_timeout", "INPUT_STALE"))

    # 3) OOD / 4) 漂移
    if not guard.in_applicability_domain:
        failures.append(("ood_drift", "OOD"))
    if not guard.drift_within_threshold:
        failures.append(("ood_drift", "DRIFT_EXCEEDED"))

    # 5) 版本匹配（模型 / Schema / 校准）
    if guard.expected_model_version and guard.model_version != guard.expected_model_version:
        failures.append(("version", "VERSION_MISMATCH"))
    if guard.expected_schema_version and guard.schema_version != guard.expected_schema_version:
        failures.append(("version", "VERSION_MISMATCH"))
    if not guard.calibration_valid:
        failures.append(("version", "CALIBRATION_INVALID"))

    # 6) 许可 / 签名本地策略有效期
    if not guard.signature_policy_valid:
        failures.append(("permit_policy", "SIGNATURE_POLICY_INVALID"))
    if not guard.license_valid:
        failures.append(("permit_policy", "LICENSE_EXPIRED"))
    if guard.policy_expires_at:
        remaining = _seconds_between(guard.now or utc_now(), guard.policy_expires_at)
        if remaining is not None and remaining <= 0:
            failures.append(("permit_policy", "POLICY_EXPIRED"))

    # 7) 设备 / 参数白名单（许可范围）
    if guard.device_id is not None and guard.allowed_devices:
        if guard.device_id not in guard.allowed_devices:
            failures.append(("device_parameter_allowlist", "DEVICE_NOT_IN_LICENSE"))
    for name, (low, high) in guard.parameter_boundaries.items():
        actual = guard.parameters.get(name)
        if actual is None:
            failures.append(("device_parameter_allowlist", f"PARAMETER_MISSING:{name}"))
        elif isinstance(actual, (int, float)) and not isinstance(actual, bool):
            if actual < low or actual > high:
                failures.append(("device_parameter_allowlist", f"PARAMETER_OUT_OF_BOUNDS:{name}"))

    # 8) 硬互锁（**不可被人工批准覆盖**）
    if not guard.interlock_passed:
        failures.append(("interlock", "INTERLOCK_DENIED"))

    # 9) 执行预算
    if guard.budget_exceeded:
        failures.append(("budget", "BUDGET_EXCEEDED"))

    # 10) 置信度（**最后**；且必须是经校准的任务级指标）
    if not guard.confidence_is_calibrated:
        failures.append(("confidence", "CONFIDENCE_NOT_CALIBRATED"))
    elif guard.confidence_threshold is not None:
        if guard.confidence is None:
            failures.append(("confidence", "CONFIDENCE_MISSING"))
        elif guard.confidence < guard.confidence_threshold:
            failures.append(("confidence", "CONFIDENCE_BELOW_THRESHOLD"))

    # 按阶段优先级排序（稳定排序保证同阶段内按发现顺序）
    failures.sort(key=lambda item: GUARD_STAGE_INDEX[item[0]])

    reasons = [reason for _, reason in failures]
    top_reason = reasons[0] if reasons else None
    # **接在共享判据上，而不是在这里重写一遍**（2026-10-05）
    #
    # 这里原本内联写着：
    #   `fallback_required = any(reason in REASONS_REQUIRING_FALLBACK for reason in reasons)`
    #   `top_stage = failures[0][0]`
    # 而 `packages/domain/guard.py` **已经实现了同样的判定**
    # （`highest_priority_stage` / `reason_requires_fallback`）——那份实现有单测，
    # 却从未被生产调用（本仓实测：生产引用 = 0）。
    #
    # 后果与"判据存在却没被用"同型：**共享判据与生效判据是两份**，
    # 谁先改谁就对不上，而单测绿不代表生产跟着变。
    # 现在两处合成一处：改 `guard.py` 即改生产行为。
    top_stage = highest_priority_stage(reasons)
    fallback_required = any(reason_requires_fallback(reason) for reason in reasons)

    if not failures:
        verdict = Verdict.ALLOW
    elif fallback_required:
        verdict = Verdict.FALLBACK
    else:
        verdict = Verdict.DENY

    return {
        "verdict": verdict,
        "reasons": reasons,
        "top_reason": top_reason,
        "top_stage": top_stage,
        "stage_order": list(GUARD_STAGES),
        "confidence_is_last_priority": GUARD_STAGES[-1] == "confidence",
        "interlock_overrides_approval": (not guard.interlock_passed),
        "approval_cannot_override": ["interlock", "structural", "version"],
        "requires_device_baseline": fallback_required,
        "checked_at": guard.now or utc_now(),
    }


# ==========================================================================
# 安全回退动作：逐设备定义，模型不得即兴选择
# ==========================================================================

# 上游 §9 允许的回退动作集合。
# **取值与契约 `p4-fallback.json` 的 `Definition.action` 枚举逐字一致（中文）**：
# 曾经这里自带一套英文常量（HOLD_BASELINE 等），与契约互不相认，
# 接口对接时必须翻译一次——而"翻译安全回退动作"正是最不该出错的地方。
SAFE_FALLBACK_ACTIONS = (
    "保持基线",      # 维持设备当前基线运行
    "停止自动调参",  # 停止 AI 自动调整参数
    "降速",          # 降低运行速度
    "受控停机",      # 按规程受控停机
)


def resolve_fallback_action(*, device_id: str, device_fallback_table: dict[str, str],
                            model_proposed: str | None = None) -> dict[str, Any]:
    """取该设备的**既定**安全回退动作。

    上游 §9：回退动作由**设备工程师逐设备定义**，不能由模型即兴选择。
    因此：
    - 只接受设备表里登记的动作；表里没有 ⇒ fail-closed（进人工），**不**自行选择；
    - 模型给出的 `model_proposed` 仅作记录，**不参与决策**（若与登记值不同，标记为被忽略）。
    """
    registered = device_fallback_table.get(device_id)
    if registered not in SAFE_FALLBACK_ACTIONS:
        return {
            "device_id": device_id,
            "action": None,
            "resolved": False,
            "reason": "DEVICE_FALLBACK_NOT_DEFINED",
            "requires_manual": True,
            "model_proposal_ignored": model_proposed is not None,
            "note": "未逐设备定义回退动作：fail-closed 转人工，不得即兴选择",
        }
    return {
        "device_id": device_id,
        "action": registered,
        "resolved": True,
        "reason": "DEVICE_FALLBACK_REGISTERED",
        "requires_manual": False,
        "model_proposal_ignored": model_proposed is not None and model_proposed != registered,
        "note": "使用设备工程师登记的回退动作",
    }


# ==========================================================================
# 断连自治（§9）
# ==========================================================================

def evaluate_disconnected_autonomy(
    *,
    release_state: str,
    signature_policy_valid: bool,
    license_valid: bool,
    policy_expires_at: str | None,
    version_consistent: bool,
    local_persistence_available: bool,
    now: str | None = None,
) -> dict[str, Any]:
    """中心断连时是否允许继续按现场规程自治（§9）。

    上游明文：**策略过期、版本异常或本地持久化不可用则禁用 AI 新动作并回到设备基线**；
    且"控制安全不依赖中心人工及时响应"。
    """
    blockers: list[str] = []
    if release_state != "ACTIVE":
        blockers.append("RELEASE_NOT_ACTIVE")
    if not signature_policy_valid:
        blockers.append("SIGNATURE_POLICY_INVALID")
    if not license_valid:
        blockers.append("LICENSE_EXPIRED")
    if not version_consistent:
        blockers.append("VERSION_MISMATCH")
    if not local_persistence_available:
        # 本地持久化不可用 ⇒ 连 spool 都保不住，必须回基线
        blockers.append("LOCAL_PERSISTENCE_UNAVAILABLE")
    if policy_expires_at:
        remaining = _seconds_between(now or utc_now(), policy_expires_at)
        if remaining is not None and remaining <= 0:
            blockers.append("POLICY_EXPIRED")

    allowed = not blockers
    return {
        "autonomy_allowed": allowed,
        "blockers": blockers,
        "requires_device_baseline": not allowed,
        "depends_on_center_human": False,
        "note": (
            "签名策略与许可有效：可按现场规程自治"
            if allowed
            else "存在阻断项：禁用 AI 新动作并回到设备基线"
        ),
    }


# ==========================================================================
# 本地 spool（§9）
# ==========================================================================

# spool 条目状态
SPOOL_STATUSES = ("PENDING", "EXECUTED", "DROPPED_EXPIRED", "REPORTED")


@dataclass
class SpoolEntry:
    edge_id: str
    boot_id: str
    sequence: int
    event_id: str
    status: str
    created_at: str
    ttl_seconds: int = 3600
    executed: bool = False
    payload_ref: str | None = None


def replayable_entries(entries: list[SpoolEntry], *, now: str | None = None) -> dict[str, Any]:
    """重连补报规则（§9）。

    三条硬规则：
    1. **已执行设备动作只补报事实，禁止中心重放** —— 它们进 `facts_to_report`，
       永远不进 `to_execute`；
    2. **历史待执行动作过 TTL 后不执行** —— 进 `dropped_expired`；
    3. 按 `event_id` **去重**（同 event_id 只报一次；重复的进 `duplicates`）。
    """
    moment = now or utc_now()
    seen: set[str] = set()
    facts: list[dict[str, Any]] = []
    to_execute: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []

    for entry in sorted(entries, key=lambda item: (item.boot_id, item.sequence)):
        if entry.event_id in seen:
            duplicates.append({"event_id": entry.event_id, "edge_id": entry.edge_id})
            continue
        seen.add(entry.event_id)

        if entry.executed or entry.status == "EXECUTED":
            # 只补报事实：**绝不**进入待执行集合
            facts.append(
                {
                    "event_id": entry.event_id,
                    "edge_id": entry.edge_id,
                    "boot_id": entry.boot_id,
                    "sequence": entry.sequence,
                    "fact_only": True,
                    "replay_forbidden": True,
                    "payload_ref": entry.payload_ref,
                }
            )
            continue

        age = _seconds_between(entry.created_at, moment)
        if age is not None and age > entry.ttl_seconds:
            dropped.append(
                {
                    "event_id": entry.event_id,
                    "edge_id": entry.edge_id,
                    "reason": "TTL_EXPIRED",
                    "age_seconds": int(age),
                }
            )
            continue

        to_execute.append(
            {
                "event_id": entry.event_id,
                "edge_id": entry.edge_id,
                "boot_id": entry.boot_id,
                "sequence": entry.sequence,
                "payload_ref": entry.payload_ref,
            }
        )

    # 重放已执行动作的请求必须被拒（中心不得重放）
    executed_ids = {item["event_id"] for item in facts}
    return {
        "facts_to_report": facts,
        "to_execute": to_execute,
        "dropped_expired": dropped,
        "duplicates": duplicates,
        "replay_forbidden_event_ids": sorted(executed_ids),
        "dedup_key": "event_id",
        "note": "已执行动作只补报事实；中心重放请求必须被拒",
    }


def reject_replay(entry: SpoolEntry | None) -> dict[str, Any]:
    """中心尝试重放已执行动作 → 拒绝（§9 明文禁止）。"""
    if entry is None:
        return {"accepted": False, "reason": "ENTRY_NOT_FOUND"}
    if entry.executed or entry.status == "EXECUTED":
        return {
            "accepted": False,
            "reason": "REPLAY_OF_EXECUTED_ACTION_FORBIDDEN",
            "event_id": entry.event_id,
        }
    return {"accepted": True, "reason": "NOT_YET_EXECUTED", "event_id": entry.event_id}


# ==========================================================================
# 影子模式（§9 / A09）
# ==========================================================================

def shadow_disposition(*, confidence: float | None, confidence_threshold: float | None,
                       guard: dict[str, Any] | None = None) -> dict[str, Any]:
    """影子模式处置：**控制写入物理/网络禁用**，高置信度也不产生真实动作。

    A09：影子模式无法写控制口；高置信也不能绕过 Schema、OOD、互锁或过期许可。
    """
    return {
        "mode": "SHADOW",
        "control_write_enabled": False,
        "physical_write_disabled": True,
        "network_write_disabled": True,
        "would_have_acted": bool(guard and guard.get("verdict") == Verdict.ALLOW),
        "action_dispatched": False,
        "comparison_recorded": True,
        "note": "影子只落比对记录；任何置信度都不产生真实副作用",
    }
