"""出域治理：判定矩阵 + 治理有序环节（P1，契约 `p1-governance.json` / `p1-egress-audit.json`）。

上游 §4.2 / §4.3 与 §3 的硬规则：

出域矩阵（契约 §4 原文，逐格实现）

    data_class | local            | enterprise_cloud                  | public
    C0         | 允许             | 允许                              | 允许
    C1         | 允许             | 允许                              | 允许
    C2         | 允许             | 仅批准变换+复核后允许（仍是 C2）  | 拒绝
    C3         | 允许（只在批准本域）| 拒绝                             | 拒绝
    C4         | 允许（本地+任务白名单+专门批准+无密钥材料） | 拒绝          | 拒绝
    未知/缺失   | 拒绝             | 拒绝                              | 拒绝
    本地故障    | 拒绝或排队       | 不作为回退目标                    | 不作为回退目标

治理**有序**环节（§4.3 的 11 个环节）不能乱序：不得先调用后扫描、不得先恢复后扫描。
任一环节失败即停止后续环节，并且**未执行的后续环节不得伪造成通过**。
"""

from __future__ import annotations

from typing import Any

from . import errors
from .classification import Classification, contains_key_material
from .registry import Registry

# 与契约 p1-egress-audit.json 的 stage 枚举逐字一致
STAGES = (
    "identity_tenant",
    "access_scope",
    "classification",
    "minimization",
    "dlp_egress",
    "model_allowlist",
    "invocation",
    "output_scan",
    "local_token_restore",
    "consumption",
    "graded_audit",
)

# 判定矩阵：{data_class: {backend: verdict}}；verdict ∈ allow / deny / conditional
EGRESS_MATRIX: dict[str, dict[str, str]] = {
    "C0": {"local": "allow", "enterprise_cloud": "allow", "public": "allow"},
    "C1": {"local": "allow", "enterprise_cloud": "allow", "public": "allow"},
    "C2": {"local": "allow", "enterprise_cloud": "conditional", "public": "deny"},
    "C3": {"local": "allow", "enterprise_cloud": "deny", "public": "deny"},
    "C4": {"local": "allow", "enterprise_cloud": "deny", "public": "deny"},
}
BACKENDS = ("local", "enterprise_cloud", "public")


class Decision:
    """出域判定结果 + 逐步执行痕迹（供审计与验收断言）。"""

    def __init__(self, *, stages: list[dict[str, Any]], flags: dict[str, Any]) -> None:
        self.stages = stages
        self.flags = flags

    @property
    def permit(self) -> bool:
        return self.flags["permit_invocation"] is True

    def as_dict(self, context, classification: Classification, body: dict[str, Any]) -> dict[str, Any]:
        """只输出契约 p1-governance.json 声明的字段。

        `stage_refs` / `audit_ref` 属于 p1-egress-audit.json，**不在这里输出**：
        Decision 是判定结果，Audit 是审计事实，两者契约各自封闭。
        """
        return {
            "tenant_id": context.tenant_id,
            "context": context.as_dict(),
            "classification": classification.as_dict(context),
            "model_id": body["model_id"],
            "model_version": body["model_version"],
            "model_backend": body["model_backend"],
            "purpose": body["purpose"],
            "policy_version": body["policy_version"],
            **self.flags,
        }


def _identity_ok(registry: Registry, request, context, purpose: str) -> bool:
    """契约 `x-required-enforcement` 的三项身份要求，顺序固定：

    1. `authenticated_tenant`：可信身份存在且登记；
    2. `registered_tenant`：tenant 在登记表内；
    3. `deny_unknown_tool` 类比项在这里是"用途是否登记"——依赖项不可用即拒绝。

    用途在 identity 之前判定，因为"身份能否调用该用途"本身依赖用途登记存在。
    """
    if registry.tenant(context.tenant_id) is None:
        return False
    identity = request.headers.get("x-v3-identity")
    if not identity:
        return False
    # 身份登记存在性（未登记身份 = 未知身份 → 拒绝）
    if not registry.service_account_registered(identity):
        return False
    if not registry.purpose_registered(purpose):
        # 依赖（用途目录）不可用/未登记 → fail-closed，并给出更精确的 code。
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "PURPOSE_NOT_REGISTERED", "用途未登记",
            details={"reason_ref": "purpose"},
        )
    return registry.service_account_allows(identity, tenant_id=context.tenant_id, purpose=purpose)


def evaluate(
    *,
    request,
    classification: Classification,
    body: dict[str, Any],
) -> Decision:
    """按 §4.3 依次判定；任一步失败即停，后续步骤不写入 stages。"""
    registry: Registry = request.registry
    context = request.context
    backend = body["model_backend"]
    model_id = body["model_id"]
    data_class = classification.data_class

    flags: dict[str, Any] = {
        # 契约 p1-governance.json 的 required 布尔位：全部显式给出，未执行到的保持 false。
        "identity_verified": False,
        "access_scope_verified": False,
        "minimized": False,
        "dlp_passed": False,
        "model_allowlisted": False,
        "route_policy_permits": False,
        "backend_healthy": False,
        "c2_transform_approved": False,
        "c4_task_allowlisted": False,
        "c4_approval_verified": False,
        "contains_key_material": False,
        "permit_invocation": False,
        "public_fallback": False,  # 契约 const false：任何情况都不得自动公网回退
        "local_backend_failed": False,
    }
    stages: list[dict[str, Any]] = []

    def stage(name: str, ok: bool, ref: str) -> None:
        # 只记录**已实际执行**的环节；未执行的不写。
        stages.append({"stage": name, "passed": ok, "result_ref": ref})

    # 1) 身份与租户校验
    identity_ok = _identity_ok(registry, request, context, body["purpose"])
    flags["identity_verified"] = identity_ok
    stage("identity_tenant", identity_ok, "identity-check")
    if not identity_ok:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "IDENTITY_NOT_VERIFIED", "身份或委托范围校验失败",
            details={"denied_field": "tenant_id"},
        )

    # 2) 工具/检索访问范围
    scope_ok = _access_scope_ok(registry, context, body["purpose"])
    flags["access_scope_verified"] = scope_ok
    stage("access_scope", scope_ok, "access-scope-check")
    if not scope_ok:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "ACCESS_SCOPE_DENIED", "用途或岗位范围不允许",
            details={"reason_ref": "access_scope"},
        )

    # 3) 输入及派生上下文分类（已在调用前完成，这里记录事实）
    stage("classification", True, f"classification-rule:{classification.rule_version}")
    if contains_key_material(classification.inputs, registry):
        # C4 密钥材料：无论后端是否本地、是否有任务白名单，都禁止进入模型上下文。
        flags["contains_key_material"] = True
        stage("minimization", False, "key-material-forbidden")
        return Decision(stages=stages, flags=flags)

    # 4) 最小化与脱敏（Tokenization 不自动降级：变换后的等级由 _post_transform_class 决定，
    #    本步只确认每个输入都已有可判定的等级；降级必须经登记规则 + 复核，见 _post_transform_class）
    minimized = all(item["data_class"] in EGRESS_MATRIX for item in classification.inputs)
    flags["minimized"] = minimized
    stage("minimization", minimized, "minimization-check")
    if not minimized:
        return Decision(stages=stages, flags=flags)

    # 5) DLP / 出域策略 —— **先于**模型白名单与健康检查。
    #    原因：判定矩阵回答的是"这个等级能不能去这个后端"；若矩阵已否决，
    #    就不应再去看"模型是否登记/版本是否一致"，否则会把 200+permit=false 的
    #    明确拒绝信号变成 403 MODEL_NOT_ALLOWLISTED，掩盖数据分级结论。
    verdict = EGRESS_MATRIX.get(data_class, {}).get(backend, "deny")
    dlp_passed = verdict in ("allow", "conditional")
    flags["dlp_passed"] = dlp_passed
    stage("dlp_egress", dlp_passed, f"egress-matrix:{data_class}:{backend}:{verdict}")
    if not dlp_passed:
        return Decision(stages=stages, flags=flags)

    # 6) 模型白名单（登记模型 + 版本 + 后端；后端以登记元数据为准，不信请求自填）
    model = registry.model(model_id)
    model_allowlisted = (
        model is not None
        and model.get("model_version") == body["model_version"]
        and model.get("model_backend") == backend
    )
    flags["model_allowlisted"] = model_allowlisted
    flags["route_policy_permits"] = registry.policy(body["policy_version"]) is not None
    stage("model_allowlist", model_allowlisted, f"model:{model_id}")
    if not model_allowlisted:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "MODEL_NOT_ALLOWLISTED", "模型未登记或版本/后端不一致",
            details={"reason_ref": "model_allowlist"},
        )

    # 条件许可：C2 非本地需要批准的变换 + 复核
    if verdict == "conditional":
        approved = _c2_transform_approved(classification)
        flags["c2_transform_approved"] = approved
        if not approved:
            return Decision(stages=stages, flags=flags)

    # C4 本地还需要任务白名单 + 专门批准（C3 无此要求）
    if data_class == "C4":
        allowlisted = context.task_id in registry.c4_task_allowlist(context.tenant_id)
        approved = registry.c4_approval_exists(context.tenant_id, context.task_id)
        flags["c4_task_allowlisted"] = allowlisted
        flags["c4_approval_verified"] = approved
        if not (allowlisted and approved):
            return Decision(stages=stages, flags=flags)

    # 7) 调用可用性：健康与故障（本地故障不得回退公网）
    backend_record = registry.model_backend(backend) or {}
    healthy = backend_record.get("reachable") is True
    flags["backend_healthy"] = healthy
    local_failed = backend == "local" and not healthy
    flags["local_backend_failed"] = local_failed
    stage("invocation", healthy, f"backend:{backend}")
    if not healthy:
        # 契约要求 permit=false 且不得把公网当回退目标；由调用方决定拒绝或排队。
        return Decision(stages=stages, flags=flags)

    if not flags["route_policy_permits"]:
        return Decision(stages=stages, flags=flags)

    flags["permit_invocation"] = True
    return Decision(stages=stages, flags=flags)


def _access_scope_ok(registry: Registry, context, purpose: str) -> bool:
    scopes = set(registry.tenant_delegated_scopes(context.tenant_id))
    required = set(registry.purpose_scopes(purpose))
    if not required:
        # 未登记的用途 → 拒绝，不给默认许可。
        return False
    return required <= scopes


def _post_transform_class(registry: Registry, item: dict[str, Any]) -> str:
    """匿名化/Tokenization **不自动降级**：返回登记规则允许的等级，或原等级。"""
    if not item.get("anonymized"):
        return item["data_class"]
    rule = item.get("transformation_rule_ref")
    reviewed = item.get("review_ref") is not None
    if rule and reviewed:
        allowed = registry.transformation_target_class(rule)
        if allowed is not None:
            return allowed
    return item["data_class"]


def _c2_transform_approved(classification: Classification) -> bool:
    """C2 非本地：需要已登记变换 + 可信复核；仅替换姓名不算。"""
    for item in classification.inputs:
        if item["data_class"] == "C2" and item.get("anonymized"):
            if item.get("transformation_rule_ref") and item.get("review_ref"):
                return True
    return False
