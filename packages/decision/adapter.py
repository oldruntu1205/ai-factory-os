"""Jev Decision Fabric 的**适配接口**（上游 §3 / §10.1 / §16.1）。

## 这个包存在的原因

上游把 Jev 定为一个**逻辑角色及决策接口**，并明文写下三件事：

> L83  Jev Decision Fabric：Route/Classify/Score/Rank/Filter/Gate/Select/Escalate，
>      **输出有限类型决策**；不执行交易、不发布排程；**高置信不得提升权限**
>
> L93  Jev 是逻辑角色及决策接口；**真实 Jev 产品、兼容实现或本地决策模型
>      均需实现同一契约**
>
> L421 实际版本与 Hermes/**Jev 适配方式需 P0 冻结**

第 93 行是关键：**三者都必须满足同一契约** ⇒ 换实现**不应该改内核**。

而在抽这个包之前，决策逻辑**写死在 `services/_kernel/routes.py`** 里
（取目录首项、`confidence` 硬编码 0.71）——换任何一个真实 Jev 都得改内核，
与"均需实现同一契约"直接冲突。这就是本包要解决的事。

## 为什么放 `packages/` 而不是 `services/decision-service/`

本仓约定：`services/<name>/` 是**零逻辑运行单元**（只有 `__main__.py` 调 `main(SERVICE_ID)`），
逻辑在 `services/_kernel/`。而 `services/decision-service` 带连字符，
**无法作为 Python 包名 import**。

所以适配接口放 `packages/decision/`：**纯逻辑、不依赖服务 DB 或运行时**，
内核与将来的 `decision-service` 都可以 import 它，且不会出现"同一份适配逻辑两处各写一遍"。

## 一条贯穿本包的纪律

**适配器不得自己降级**（no silent fallback）。

- 不认识的动作 → 返回 `None` 或 `unknown_reason`，**不得猜一个**；
- 没有有效校准 → `confidence` 必须为 `None`（上游 L83「高置信不得提升权限」，
  L219「置信度是经独立数据校准的任务级指标，**不能取 LLM 自报数字**」）；
- 能路由 → 给建议，但**建议不等于执行授权**（上游 L21/L50：输出是建议）。

`decide()` 的返回值**只描述建议**。能否执行由 Business Control 的 Policy 判定，
不在本包范围内——那是上游刻意分开的两条轴。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: 上游 L83 列出的**八种**决策类型。**这是上游的封闭清单**，不得增删。
#:
#: （计数提醒：Route/Classify/Score/Rank/Filter/Gate/Select/Escalate **是 8 个**。
#: 此前我在口头汇报里反复说成"九种"——数错了，此处以清单本身为准。）
#:
#: - `Route`      分流：决定谁处理任务（已在运行链路上）
#: - `Classify`   分类
#: - `Score`      打分
#: - `Rank`       排序
#: - `Filter`     过滤
#: - `Gate`       门禁
#: - `Select`     选择
#: - `Escalate`   升级（转人工）
DECISION_KINDS: tuple[str, ...] = (
    "Route",
    "Classify",
    "Score",
    "Rank",
    "Filter",
    "Gate",
    "Select",
    "Escalate",
)

#: 当前适配器**已实现**的决策类型。契约里八种都登记；未实现的**显式拒绝**，
#: 不静默当作 Route 返回——那会让调用方拿到一个"看起来成功"的错误答案。
IMPLEMENTED_KINDS: tuple[str, ...] = ("Route", "Classify", "Gate", "Escalate")


@dataclass(frozen=True)
class DecisionOutcome:
    """适配器给出的**建议**。字段与契约 `decision-result.json` 一一对应。

    `confidence` 为 `None` 表示"无有效校准"。**这不是失败**，
    而是上游要求的诚实表达：没有校准就不能给数字，
    更不能拿模型自报的数字当置信度。
    """

    route: str
    action: str
    category: str
    reason_code: str
    fallback_route: str
    confidence: float | None = None
    model_version: str | None = None
    calibration_version: str | None = None
    rule_version: str | None = None
    decision_kind: str = "Route"
    #: 证据引用（上游 §10.2 要求可追溯）
    evidence_refs: list[str] = field(default_factory=list)
    #: 适配器特有的补充字段（如 Score 的分数）。**不进契约**，
    #: 只在需要时由内核决定是否暴露；默认空。
    extra: dict[str, Any] = field(default_factory=dict)

    def as_view(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "route": self.route,
            "action": self.action,
            "category": self.category,
            "confidence": self.confidence,
            "model_version": self.model_version,
            "calibration_version": self.calibration_version,
            "rule_version": self.rule_version,
            "reason_code": self.reason_code,
            "decision_kind": self.decision_kind,
            "evidence_refs": list(self.evidence_refs),
            "fallback": {"route": self.fallback_route, "reason_code": self.reason_code},
        }
        payload.update(self.extra)
        return payload


class DecisionError(Exception):
    """适配器无法给出决策时的**显式**失败。

    **不要用返回值表达失败**：返回 `None` 容易被调用方当成"没有意见"而放行。
    抛异常能保证调用方必须处理。
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@runtime_checkable
class DecisionAdapter(Protocol):
    """Jev 的逻辑契约。**真实 Jev 产品、兼容实现或本地决策模型都实现它。**

    ## 实现者必须遵守的三条

    1. **不得自造有限类型**：`route`/`action`/`category`/`reason_code` 必须来自
       传入的 `catalog`（上游 L83「输出有限类型决策」+ L465「未登记即拒绝」）；
    2. **不得伪造置信度**：`confidence` 只有在 `calibration_version` 非空时才可给值，
       且必须来自**独立校准数据集**，不得取模型自报（上游 L219）；
    3. **建议不是授权**：本接口的输出**不构成**执行许可（上游 L21/L50）。
    """

    adapter_id: str

    def supported_kinds(self) -> tuple[str, ...]:
        """本适配器**已实现**的决策类型（`DECISION_KINDS` 的子集）。"""
        ...

    def decide(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        catalog: dict[str, Any],
        calibration_version: str | None,
        facts: dict[str, Any] | None = None,
    ) -> DecisionOutcome:
        """给出一个决策**建议**；无法决策时抛 `DecisionError`。

        `facts` 是**只读的登记表事实**（工具目录、策略规则、分类规则等），
        给需要真实依据的适配器用。Mock 适配器可以忽略它。
        **它不含任何租户私密数据**——只有登记的元数据。
        """
        ...
