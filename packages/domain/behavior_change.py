"""行为性变化（上游 §9）：模型/Schema/特征提取器/tokenizer/校准参数/动作映射。

> 任一变更 ⇒ **必须重做对应离线验证与影子发布门禁**，
> 不能替换文件继续使用旧批准。

"哪些变化算行为性"是**中心与现场必须一致**的判断：
现场若认为某次替换不影响行为，就会带着旧批准继续跑——
而这正是上游明文禁止的情形。所以它属于本包，而不是某一个服务。
"""

from __future__ import annotations

from typing import Any, Final

# 行为性变化类别。**未列入的一律不算**——但调用方必须显式声明，
# 不能默认放过（默认放过 = 悄悄绕过门禁）。
BEHAVIORAL_CHANGE_KINDS: Final[tuple[str, ...]] = (
    "model",
    "schema",
    "feature_extractor",
    "tokenizer",
    "calibration_parameters",
    "action_mapping",
)

# 每类行为性变化需要回到的阶段（§9：重做对应门禁）
BEHAVIORAL_CHANGE_REQUIRED_STAGE: Final[dict[str, str]] = {
    "model": "CALIBRATED",
    "calibration_parameters": "CALIBRATED",
    "schema": "SCHEMA_FROZEN",
    "feature_extractor": "SCHEMA_FROZEN",
    "tokenizer": "SCHEMA_FROZEN",
    "action_mapping": "CALIBRATED",
}


def behavioral_change_impact(changed_kinds: list[str], *, order: dict[str, int]) -> dict[str, Any]:
    """评估行为性变化的影响：返回需要回退到的阶段与是否使旧批准失效。

    `order` 由调用方传入（通常是 `release.EDGE_RELEASE_ORDER`）——
    本函数保持**纯函数**，不隐式依赖模块级可变状态。
    """
    behavioral = [kind for kind in changed_kinds if kind in BEHAVIORAL_CHANGE_KINDS]
    if not behavioral:
        return {
            "behavioral": False,
            "requires_reapproval": False,
            "required_stage": None,
            "invalidates_approval": False,
            "note": "未声明行为性变化；不触发重开门禁（调用方须确保声明属实）",
        }
    # 取"最靠前"的必需阶段：只要有一项需要回到更早阶段，就必须回到最早那个
    required = min(
        (BEHAVIORAL_CHANGE_REQUIRED_STAGE[kind] for kind in behavioral),
        key=lambda state: order[state],
    )
    return {
        "behavioral": True,
        "requires_reapproval": True,
        "required_stage": required,
        "invalidates_approval": True,
        "changed_kinds": sorted(behavioral),
        "note": "行为性变化：旧批准失效，必须回到该阶段重开门禁",
    }
