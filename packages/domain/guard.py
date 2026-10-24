"""Guard 判定语义（上游 §9）：阶段、优先级、原因码、安全回退。

## 唯一真相源

**只有一份词表**：`GUARD_REASON_TO_STAGE`（原因码 → 阶段）。

阶段表 `GUARD_STAGES` 由它**派生**，而不是另写一份名单——
两份名单一定会漂移，而漂移的表现是"某条判定被归到一个不存在的阶段"。

阶段顺序由 `GUARD_STAGE_ORDER` 单独声明（顺序是上游的硬要求，
不是按字母或拼音排的，无法从映射推出来）。

## 为什么要在这里（而不是在某个服务里）

现场（Edge）与中心（观测、指标、对账）必须对"阶段叫什么、谁先于谁"
逐字一致。放在某一个服务里，其它消费方就只能**手抄**——
本项目已经踩过：`metrics.py` 手抄了一份阶段名与原因码映射，
与 `edge.py` 的产出之间**没有任何机制保证一致**。
"""

from __future__ import annotations

from typing import Final

# ==========================================================================
# 判定阶段：**顺序即优先级**（上游 §9 的硬顺序）
#
# 名称与契约 `p4-guard.json` 的 `Check.stage` 枚举**逐字对齐**。
# 曾经实现里自带一套名字（ood / drift / license / scope 各占一级），
# 结果同一语义在契约与实现里有两套术语，接口对接时必然要翻译一次、
# 翻译就会出错。以契约为准：OOD 与漂移同级、许可与策略同级、设备与参数同级。
#
# `confidence` 必须在**最后**：置信度不能覆盖以上任何一项。
# ==========================================================================

GUARD_STAGE_ORDER: Final[tuple[str, ...]] = (
    "structure",                   # 缺失字段 / NaN / 越界 / 单位不明
    "freshness_timeout",           # 输入新鲜度 / 超时
    "ood_drift",                   # 适用域（OOD）与漂移
    "version",                     # 模型/Schema/校准/tokenizer 版本匹配
    "permit_policy",               # 签名本地策略与许可有效期
    "device_parameter_allowlist",  # 设备/参数白名单（许可范围）
    "interlock",                   # 硬互锁
    "budget",                      # 执行预算
    "confidence",                  # 置信度（**最后**：不能覆盖以上任何一项）
)

# ==========================================================================
# 原因码 → 阶段（**唯一真相源**）
#
# 键是 Guard 产出的原因码（`reasons` 里 `CODE` 或 `CODE:detail` 的 `CODE` 部分）。
# 值必须出现在 `GUARD_STAGE_ORDER` 里——由下面的自检保证。
# ==========================================================================

GUARD_REASON_TO_STAGE: Final[dict[str, str]] = {
    # structure：结构性问题（缺字段 / NaN / 越界 / 单位）
    "MISSING_FIELD": "structure",
    "MISSING_REFERENCE": "structure",
    "NAN_VALUE": "structure",
    "OUT_OF_RANGE": "structure",
    "UNIT_UNKNOWN_OR_MISMATCH": "structure",
    # freshness_timeout：输入新鲜度
    "INPUT_TIMESTAMP_UNPARSABLE": "freshness_timeout",
    "INPUT_STALE": "freshness_timeout",
    # ood_drift：适用域与漂移
    "OOD": "ood_drift",
    "DRIFT_EXCEEDED": "ood_drift",
    # version：版本匹配
    "VERSION_MISMATCH": "version",
    "CALIBRATION_INVALID": "version",
    # permit_policy：签名本地策略与许可有效期
    "SIGNATURE_POLICY_INVALID": "permit_policy",
    "LICENSE_EXPIRED": "permit_policy",
    "POLICY_EXPIRED": "permit_policy",
    "LOCAL_PERSISTENCE_UNAVAILABLE": "permit_policy",
    # device_parameter_allowlist：设备与参数白名单
    "DEVICE_NOT_IN_LICENSE": "device_parameter_allowlist",
    "PARAMETER_MISSING": "device_parameter_allowlist",
    "PARAMETER_OUT_OF_BOUNDS": "device_parameter_allowlist",
    # interlock：硬互锁
    "INTERLOCK_DENIED": "interlock",
    # budget：执行预算
    "BUDGET_EXCEEDED": "budget",
    # confidence：置信度（最后一道）
    "CONFIDENCE_NOT_CALIBRATED": "confidence",
    "CONFIDENCE_MISSING": "confidence",
    "CONFIDENCE_BELOW_THRESHOLD": "confidence",
}

# 阶段表**由映射派生**：不再单独维护一份名单
GUARD_STAGES: Final[tuple[str, ...]] = tuple(
    stage for stage in GUARD_STAGE_ORDER if stage in set(GUARD_REASON_TO_STAGE.values())
)

GUARD_STAGE_INDEX: Final[dict[str, int]] = {
    stage: index for index, stage in enumerate(GUARD_STAGE_ORDER)
}

# ==========================================================================
# 哪些原因码要求**安全回退到设备基线**（而不是停在原地）
#
# 这些是"继续按模型建议跑会更危险"的情形：互锁、适用域、漂移、版本与许可失效、
# 本地持久化不可用（断连时无法保证可追溯）。
# ==========================================================================

REASONS_REQUIRING_FALLBACK: Final[frozenset[str]] = frozenset(
    {
        "INTERLOCK_DENIED",
        "OOD",
        "DRIFT_EXCEEDED",
        "LOCAL_PERSISTENCE_UNAVAILABLE",
        "POLICY_EXPIRED",
        "LICENSE_EXPIRED",
        "VERSION_MISMATCH",
    }
)


def stage_of_reason(reason: str) -> str:
    """原因码 → 阶段。**未知原因码不猜**，返回 `"unknown"` 交调用方处理。

    注意这与"归到某个阶段"不同：把未知原因码塞进任意阶段会污染判定口径。
    调用方按自己的语义决定是拒绝、告警还是计入 `unknown` 桶。
    """
    code = reason.split(":", 1)[0].strip().upper()
    return GUARD_REASON_TO_STAGE.get(code, "unknown")


def reason_requires_fallback(reason: str) -> bool:
    """该原因码是否要求安全回退。"""
    code = reason.split(":", 1)[0].strip().upper()
    return code in REASONS_REQUIRING_FALLBACK


def highest_priority_stage(reasons: list[str]) -> str | None:
    """按**上游硬顺序**给出最高优先级的阶段（即最先失败的那一步）。"""
    stages = [stage_of_reason(reason) for reason in reasons]
    known = [stage for stage in stages if stage in GUARD_STAGE_INDEX]
    if not known:
        return None
    return min(known, key=lambda stage: GUARD_STAGE_INDEX[stage])


def unregistered_reason_codes(codes: object) -> list[str]:
    """列出**未在本包登记**的原因码（门禁用）。

    `codes` 是任意可迭代的原因码集合。返回空列表表示全部已登记。
    """
    return sorted({str(code).split(":", 1)[0].strip().upper() for code in codes} - set(GUARD_REASON_TO_STAGE))


def stage_vocabulary_problems() -> list[str]:
    """本包自身的一致性自检（应恒为空）。

    判据：映射里的每个值都必须是 `GUARD_STAGE_ORDER` 里的阶段；
    每个阶段都至少有一个原因码归属（否则它永远空转）。
    """
    problems: list[str] = []
    known = set(GUARD_STAGE_ORDER)
    for code, stage in GUARD_REASON_TO_STAGE.items():
        if stage not in known:
            problems.append(f"原因码 {code} 归到了未声明的阶段 {stage!r}")
    covered = set(GUARD_REASON_TO_STAGE.values())
    for stage in GUARD_STAGE_ORDER:
        if stage not in covered:
            problems.append(f"阶段 {stage!r} 没有任何原因码归属（永远空转）")
    if len(GUARD_STAGE_ORDER) != len(set(GUARD_STAGE_ORDER)):
        problems.append("GUARD_STAGE_ORDER 里有重复阶段")
    return problems
