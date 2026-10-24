"""Jev 的 **规则适配器**（第二个实现，确定性）。

## 它为什么存在

上游 §3 说三类实现者「**均需实现同一契约**」：

> 真实 Jev 产品、**兼容实现**或本地决策模型均需实现同一契约

`mock_adapter` 只证明"接口能被实现一次"。**一个实现证明不了接口是接缝**——
所以这里给出第二个实现，用来验证两件事：

1. **换实现不改内核**：内核与 HTTP 层一行不动；
2. **换实现不改测试**：`tests/decision/` 的契约性判据对两个适配器同样成立。

而"兼容实现"正是上游点名的那一类——本适配器就是它的代表。

## 它依据什么做判定

**只用登记表里已有的事实**，不新编数据：

| 依据 | 来自 |
|---|---|
| 工具的风险等级、可读/可写、是否需要批准 | `tools[*]` |
| 风险等级 → 可否自动执行 | `policies[*].risk_rules` |
| 输入来源 → 数据等级 | `p1.classification.derived_origin_rules` |
| 数据等级 → 可否出域/交给哪个后端 | `data_classes`、`model_backends` |

**这与 mock 的差别**：mock 对所有输入返回同一个结果（它只证明"能返回"）；
本适配器**真的按输入分流**——不同风险/来源会得到不同的路由与原因码。
一个对什么输入都给同样答案的"决策"，等于没决策。

## 为什么它能实现 8 种中的 6 种

| 类型 | 依据 | 为什么可以 |
|---|---|---|
| `Route` | 工具风险 + 策略 + 数据等级 | 规则完全够 |
| `Classify` | 工具能力 + 策略 + 来源规则 | 是"归到哪个已知类"，不是"猜" |
| `Gate` | 策略 `auto_execute` | 就是布尔门禁 |
| `Escalate` | R3 或不可自动 | 明确条件 |
| `Score` | **返回策略里已有的门槛**，不是自造分数 | 见下 |
| `Filter` | 数据等级与后端能力比对 | 是"剔除不允许的"，规则够 |

## 为什么 `Rank` / `Select` 仍然**不做**

- `Rank` 需要**排序依据**。手上唯一可比的量是风险等级，而"按风险排序"
  对调用方毫无用处（它要的是"哪个方案更好"）——那是方案质量，需要真实评测数据；
- `Select` 是"从候选中选一个"，**手上没有候选集**。
  没有候选就"选"一个，就是编。

两者**显式拒绝**，理由写进 `DecisionError` 消息里，而不是伪装成已完成。

## `Score` 的诚实处理（值得单说）

上游 §8.2 要求「置信度是经**独立数据校准**的任务级指标，**不能取 LLM 自报数字**」。
本适配器**没有校准数据**，所以：

- **不返回 `confidence`**（保持 `None`）；
- 改为在 `extra.thresholds` 里给出**策略里已经存在的门槛**
  （`R2/R3` 需批准、`R0/R1` 可自动）——这是**已登记的事实**，不是我们算的分。

`extra` 字段**不进契约**，属适配器的附注；调用方若用了它，是调用方的判断。
这样既不违反"不得伪造置信度"，又确实提供了规则能做到的那部分价值。
"""

from __future__ import annotations

from typing import Any

from .adapter import DECISION_KINDS, DecisionError, DecisionOutcome

#: 输入来源的语义前缀 → 用途（供 Classify/Filter 用）。
#: 与 `p1.classification.derived_origin_rules` 的键**同名**，不另造词表。
ORIGIN_PREFIX = "origin:"


class RulesDecisionAdapter:
    """确定性规则适配器。不需要模型、不需要网络、不需要校准数据。"""

    adapter_id = "rules"

    def supported_kinds(self) -> tuple[str, ...]:
        return ("Route", "Classify", "Gate", "Escalate", "Score", "Filter")

    # ---- 内部：从事实里取依据 -------------------------------------------

    def _tool(self, facts: dict[str, Any], tool_id: str | None) -> dict[str, Any] | None:
        if not tool_id:
            return None
        tools = facts.get("tools") or {}
        entry = tools.get(tool_id)
        return entry if isinstance(entry, dict) else None

    def _risk_rule(self, facts: dict[str, Any], risk_class: str | None) -> dict[str, Any] | None:
        """风险等级 → 策略规则。策略版本取登记表的默认策略。"""
        policies = facts.get("policies") or {}
        if not policies:
            return None
        policy = next(iter(policies.values()))
        rules = (policy or {}).get("risk_rules") or {}
        rule = rules.get(risk_class) if risk_class else None
        return rule if isinstance(rule, dict) else None

    def _origin(self, request: dict[str, Any]) -> str | None:
        """从 `input_ref` 的 `origin:<name>` 前缀取输入来源。

        **读不到就返回 `None`，不猜**——猜错来源会导致错误的数据等级，
        而数据等级决定能不能出域，错一档就是泄漏。
        """
        input_ref = str(request.get("input_ref") or "")
        if input_ref.startswith(ORIGIN_PREFIX):
            name = input_ref[len(ORIGIN_PREFIX):].strip()
            return name or None
        return None

    def _derived_class(self, facts: dict[str, Any], origin: str | None) -> str | None:
        if not origin:
            return None
        rules = ((facts.get("classification") or {}).get("derived_origin_rules")) or {}
        value = rules.get(origin)
        return value if isinstance(value, str) else None

    def _required(self, catalog: dict[str, Any], key: str, value: str) -> str:
        """取一个**必须存在**的目录值；不存在即拒绝（不静默换一个）。"""
        if value not in (catalog.get(key) or []):
            raise DecisionError(
                "DECISION_CATALOG_INCOMPLETE",
                f"目录 {key} 里没有 {value!r}——适配器不得使用目录外的取值",
                details={"object_ref": key, "value": value},
            )
        return value

    def _rule_version(self, facts: dict[str, Any]) -> str:
        """规则版本 = **所依据的那两份规则的版本**拼起来（可追溯）。"""
        catalog_version = (facts.get("catalog") or {}).get("catalog_version") or "catalog-unknown"
        policy_version = (facts.get("policy") or {}).get("policy_version") or "policy-unknown"
        return f"{catalog_version}+{policy_version}"

    # ---- 主入口 ----------------------------------------------------------

    def decide(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        catalog: dict[str, Any],
        calibration_version: str | None,
        facts: dict[str, Any] | None = None,
    ) -> DecisionOutcome:
        if kind not in DECISION_KINDS:
            raise DecisionError(
                "DECISION_KIND_UNKNOWN",
                f"未知决策类型 {kind!r}；上游封闭清单是 {list(DECISION_KINDS)}",
                details={"object_ref": "decision_kind"},
            )
        if kind not in self.supported_kinds():
            if kind in ("Rank", "Select"):
                raise DecisionError(
                    "DECISION_KIND_NOT_IMPLEMENTED",
                    f"{kind} 需要{'排序依据（方案质量，需真实评测数据）' if kind == 'Rank' else '候选集'}，"
                    "本适配器不自造依据——显式拒绝优于假装完成",
                    details={"object_ref": "decision_kind", "kind": kind},
                )
            raise DecisionError(
                "DECISION_KIND_NOT_IMPLEMENTED",
                f"决策类型 {kind} 未实现（已实现：{list(self.supported_kinds())}）",
                details={"object_ref": "decision_kind", "kind": kind},
            )

        # ① **先验目录**（契约完整性），再看依据。
        #
        # 顺序有语义：目录是**契约**，依据是**输入**。目录本身不完整时，
        # 报"依据不足"是**误导**——那会让排障的人去翻登记表的工具条目，
        # 而真正坏掉的是目录结构。契约先于输入。
        for key in ("routes", "actions", "categories", "reason_codes", "fallback_routes"):
            if not (catalog.get(key) or []):
                raise DecisionError(
                    "DECISION_CATALOG_INCOMPLETE",
                    f"决策目录缺少 {key}——目录是契约，缺了就不能给出任何决策",
                    details={"object_ref": key},
                )

        facts = facts or {}
        tool_id = request.get("tool_id")
        tool = self._tool(facts, tool_id)
        risk_class = (tool or {}).get("risk_class")
        if risk_class is None:
            risk_class = request.get("risk_class")
        if isinstance(risk_class, str) and risk_class not in ("R0", "R1", "R2", "R3"):
            risk_class = None

        rule = self._risk_rule(facts, risk_class)
        auto_execute = bool((rule or {}).get("auto_execute") is True)
        requires_approval = bool((rule or {}).get("requires_approval") is True)
        origin = self._origin(request)
        derived_class = self._derived_class(facts, origin)

        # ② 依据不全 ⇒ **不给决策**（宁可拒绝，也不按半截信息分流）
        if not isinstance(facts.get("tools"), dict) or not facts.get("tools"):
            raise DecisionError(
                "DECISION_FACTS_UNAVAILABLE",
                "拿不到工具目录，规则适配器无法判定风险等级",
            )
        if risk_class is None:
            raise DecisionError(
                "DECISION_FACTS_UNAVAILABLE",
                "请求未给出可判定的风险等级（既没有已登记的工具，也没有 `risk_class`）——"
                "规则适配器不猜风险等级：猜错会把写入当成只读",
                details={"object_ref": "risk_class"},
            )
        if rule is None:
            raise DecisionError(
                "DECISION_FACTS_UNAVAILABLE",
                f"风险等级 {risk_class!r} 未在策略中登记，无法判定",
                details={"object_ref": "risk_class"},
            )

        route = self._required(catalog, "routes", self._pick_route(risk_class, auto_execute, requires_approval))
        reason = self._required(catalog, "reason_codes", self._pick_reason(risk_class, auto_execute, requires_approval))
        category = self._required(catalog, "categories", self._pick_category(risk_class))
        action = self._required(catalog, "actions", self._pick_action(risk_class, auto_execute, requires_approval))
        fallback = self._required(catalog, "fallback_routes", "route-human")

        extra: dict[str, Any] = {}
        if kind == "Classify":
            extra["classification_basis"] = {
                "origin": origin,
                "derived_data_class": derived_class,
                "basis": "p1.classification.derived_origin_rules",
            }
            if origin is None:
                # 来源不明 ⇒ 分类结论只能是"未定"，并让上游知道为什么
                reason = self._required(catalog, "reason_codes", "reason-rules-undetermined")
        elif kind == "Score":
            # 不给分数，给**已登记的门槛**（见模块文档的"诚实处理"）
            extra["thresholds"] = {
                "auto_execute": auto_execute,
                "requires_approval": requires_approval,
                "risk_class": risk_class,
            }
            extra["score_unavailable_reason"] = (
                "无独立校准数据集；上游 §8.2 禁止用自报数字充当置信度"
            )
        elif kind == "Filter":
            admitted = self._admitting_backends(facts, derived_class)
            extra["filter_basis"] = {
                "data_class": derived_class,
                "admitting_backends": admitted,
                "basis": "model_backends[*].allowed_data_classes",
            }
            if not admitted:
                # 没有后端被批准处理该等级 ⇒ 不得出域，转人工（上游 §4.1：
                # 「未知等级默认禁止出域」；C3/C4 仅批准本地模型）
                reason = self._required(catalog, "reason_codes", "reason-rules-high-risk")
                action = self._required(catalog, "actions", "action-escalate")
                route = self._required(catalog, "routes", "route-human")
        elif kind == "Escalate":
            pass  # 路由在下面统一按 kind 调整

        if kind == "Escalate":
            route = fallback
            action = self._required(catalog, "actions", "action-escalate")

        return DecisionOutcome(
            route=route,
            action=action,
            category=category,
            reason_code=reason,
            fallback_route=fallback,
            # **规则适配器同样不给置信度**：它没有校准，规则命中不等于置信度
            confidence=None,
            model_version=None,
            calibration_version=calibration_version,
            rule_version=self._rule_version(facts),
            decision_kind=kind,
            evidence_refs=[str(request.get("input_ref") or "input-unknown")],
            extra=extra,
        )

    # ---- 规则表（每一条都可对着上游条款读） ----------------------------

    @staticmethod
    def _pick_route(risk_class: str | None, auto_execute: bool, requires_approval: bool) -> str:
        """上游 §3：Jev 做**任务分流**——决定谁处理任务。

        - R0/R1 且策略允许自动 ⇒ `route-readonly-fast`（快速通道）
        - 其余（需要批准/高风险）⇒ `route-deep`（深度处理，走审批链）
        """
        if risk_class in ("R0", "R1") and auto_execute and not requires_approval:
            return "route-readonly-fast"
        return "route-deep"

    @staticmethod
    def _pick_reason(risk_class: str | None, auto_execute: bool, requires_approval: bool) -> str:
        if risk_class == "R3":
            return "reason-rules-high-risk"
        if requires_approval:
            # 是否已获批准由业务侧决定；这里只表达"策略要求批准"
            return "reason-rules-write-needs-approval"
        if auto_execute:
            return "reason-rules-readonly-r0" if risk_class == "R0" else "reason-rules-readonly-supervised"
        return "reason-rules-undetermined"

    @staticmethod
    def _pick_category(risk_class: str | None) -> str:
        if risk_class == "R3":
            return "category-high-risk"
        if risk_class in ("R2",):
            return "category-write"
        return "category-readonly"

    @staticmethod
    def _pick_action(risk_class: str | None, auto_execute: bool, requires_approval: bool) -> str:
        if risk_class == "R3":
            return "action-escalate"
        if requires_approval:
            return "action-require-approval"
        if auto_execute:
            return "action-proceed"
        return "action-noop"

    @staticmethod
    def _admitting_backends(facts: dict[str, Any], data_class: str | None) -> list[str]:
        """哪些模型后端被批准处理该等级（上游 §4.1 的出域矩阵）。

        矩阵在登记表的 `model_backends[*].allowed_data_classes`——
        **以登记为准，不在这里另写一份等级规则**（两处规则必然漂移）。

        ⚠️ 我第一版写成读 `data_classes[dclass].model_backends`，
        而真实结构是 `data_classes` 一个**字符串列表**、矩阵在 `model_backends`——
        于是每种等级都会被判"不允许"。**字段名不能猜**。
        """
        if not data_class:
            return []
        backends = facts.get("model_backends") or {}
        admitted: list[str] = []
        for name, entry in backends.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("reachable") is not True:
                continue
            allowed = entry.get("allowed_data_classes") or []
            if data_class in allowed:
                admitted.append(name)
        return sorted(admitted)
