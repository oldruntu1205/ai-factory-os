"""**两个适配器都满足同一契约**（上游 §3「均需实现同一契约」）。

## 这组用例解决什么问题

`tests/decision/test_decision_adapter.py` 验的是"接口能被实现一次"。
**一个实现证明不了接口是接缝**——要证明接缝成立，至少需要两个实现，
且它们**跑同一套契约性判据**。

上游 §3 正好点名了三类实现者：

> 真实 Jev 产品、**兼容实现**或本地决策模型**均需实现同一契约**

本组里的 `rules` 适配器就是"**兼容实现**"的代表，`mock` 是第一实现。

## 本组守三件事

1. **契约一致**：两个适配器都只用目录里的取值、都不自造决策类型、
   都不给未校准的置信度——**逐条对两个实现都判**；
2. **换实现不改测试**：本组的契约性断言对两者同样成立（不是各写一套）；
3. **"第二个实现"必须真的不同**：如果 `rules` 对任何输入都给同一个结果，
   那它只是 mock 的复制品，**证明不了任何事**。所以有一条判据专门要求
   它对不同风险等级给出**不同**的路由。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from packages.decision import (
    DECISION_KINDS,
    DecisionError,
    resolve_adapter,
)
from services._kernel.registry import Registry

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "services/_kernel/registry.mock.json"

#: 两个适配器**一律**要满足的契约。新增实现时应把它加进来。
ALL_ADAPTERS = ("mock", "rules")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load(REGISTRY_PATH)


@pytest.fixture(scope="module")
def catalog(registry: Registry) -> dict:
    return registry.decision_catalog()


@pytest.fixture(scope="module")
def facts(registry: Registry) -> dict:
    return registry.decision_facts()


def _decide(adapter_id: str, kind: str, *, request=None, catalog=None,
            facts=None, calibration_version=None):
    """驱动任一适配器。

    **默认请求带一个已登记的工具**（`erp.stock.query`，R0）——
    rules 适配器需要可判定的风险等级才给决策；不给工具它就拒绝（那是正确行为，
    由 `test_rules_adapter_refuses_without_a_risk_basis` 单独验）。
    两个适配器收到**完全相同的输入**，这才叫"跑同一套契约性判据"。
    """
    adapter = resolve_adapter({"implementation": adapter_id})
    return adapter.decide(
        kind=kind,
        request=request or {"input_ref": "input-demo", "tool_id": "erp.stock.query"},
        catalog=catalog,
        calibration_version=calibration_version,
        facts=facts,
    )


def _catalog_values(catalog: dict) -> set[str]:
    values: set[str] = set()
    for key in ("routes", "actions", "categories", "reason_codes", "fallback_routes"):
        values.update(catalog.get(key) or [])
    return values


# ==========================================================================
# 一、契约一致：两个实现都守同样三条
# ==========================================================================

@pytest.mark.parametrize("adapter_id", ALL_ADAPTERS)
def test_only_catalog_values_are_used(adapter_id, catalog, facts):
    """**不得自造有限类型**：每个输出值都必须在目录里。

    上游 §3「输出有限类型决策」+ §10.1「未登记即拒绝」。
    这条对两个适配器同样成立——目录是契约，适配器只决定"怎么选"。
    """
    allowed = _catalog_values(catalog)
    for kind in ("Route", "Classify", "Gate", "Escalate"):
        outcome = _decide(adapter_id, kind, catalog=catalog, facts=facts)
        for field in ("route", "action", "category", "reason_code", "fallback_route"):
            value = getattr(outcome, field)
            assert value in allowed, (
                f"{adapter_id} 的 {kind} 在 {field} 里给了目录外的值 {value!r}"
            )


@pytest.mark.parametrize("adapter_id", ALL_ADAPTERS)
def test_no_confidence_without_calibration(adapter_id, catalog, facts):
    """**不得伪造置信度**：无校准时必须为 `None`（上游 §3/§8.2）。"""
    for kind in ("Route", "Classify", "Gate", "Escalate"):
        outcome = _decide(adapter_id, kind, catalog=catalog, facts=facts,
                          calibration_version=None)
        assert outcome.confidence is None, (
            f"{adapter_id} 的 {kind} 在没有校准时给了置信度 {outcome.confidence}"
        )
        assert outcome.model_version is None


@pytest.mark.parametrize("adapter_id", ALL_ADAPTERS)
def test_kind_is_echoed(adapter_id, catalog, facts):
    """请求哪类决策，回显就必须是哪类——否则调用方无法判断拿到的是什么。"""
    for kind in ("Route", "Classify", "Gate", "Escalate"):
        outcome = _decide(adapter_id, kind, catalog=catalog, facts=facts)
        assert outcome.decision_kind == kind


@pytest.mark.parametrize("adapter_id", ALL_ADAPTERS)
def test_unknown_kind_refused(adapter_id, catalog, facts):
    """自造类型被拒（两边都拒，且都是同一个错误码）。"""
    with pytest.raises(DecisionError) as excinfo:
        _decide(adapter_id, "Summarize", catalog=catalog, facts=facts)
    assert excinfo.value.code == "DECISION_KIND_UNKNOWN"


@pytest.mark.parametrize("adapter_id", ALL_ADAPTERS)
def test_every_upstream_kind_has_a_definite_destination(adapter_id, catalog, facts):
    """8 种类型逐个有归宿：实现（回显一致）或显式拒绝。**不允许静默返回。**"""
    adapter = resolve_adapter({"implementation": adapter_id})
    implemented = set(adapter.supported_kinds())
    for kind in DECISION_KINDS:
        if kind in implemented:
            outcome = _decide(adapter_id, kind, catalog=catalog, facts=facts)
            assert outcome.decision_kind == kind
        else:
            with pytest.raises(DecisionError) as excinfo:
                _decide(adapter_id, kind, catalog=catalog, facts=facts)
            assert excinfo.value.code == "DECISION_KIND_NOT_IMPLEMENTED"


@pytest.mark.parametrize("adapter_id", ALL_ADAPTERS)
def test_broken_catalog_is_refused(adapter_id, catalog, facts):
    """目录缺清单 ⇒ 拒绝，不给默认值凑合。"""
    broken = dict(catalog)
    broken["routes"] = []
    with pytest.raises(DecisionError) as excinfo:
        _decide(adapter_id, "Route", catalog=broken, facts=facts)
    assert excinfo.value.code == "DECISION_CATALOG_INCOMPLETE"


# ==========================================================================
# 二、"第二个实现"必须真的不同（否则它证明不了任何事）
# ==========================================================================

def test_rules_adapter_actually_varies_with_the_risk_class(catalog, facts):
    """`rules` 对**不同风险等级**必须给出**不同**的路由。

    ## 这条是本组的核心

    如果 `rules` 对任何输入都给同一个答案，那它就只是 mock 的复制品——
    "换了实现"这件事**没有被验证**，接缝是否成立仍然未知。

    所以这里要求它至少能区分「只读快通道」与「写入走深度处理」，
    这正是上游 §3 说的「任务分流：**决定谁处理任务**」。
    """
    read_only = _decide("rules", "Route",
                        request={"input_ref": "input-demo", "tool_id": "erp.stock.query"},
                        catalog=catalog, facts=facts)
    write = _decide("rules", "Route",
                    request={"input_ref": "input-demo", "tool_id": "pmc.dispatch.publish"},
                    catalog=catalog, facts=facts)

    assert read_only.route != write.route, (
        "rules 对 R0 只读与 R2 写入给出了同一条路由——那它没有真的在做分流。\n"
        f"  只读：{read_only.route}\n  写入：{write.route}"
    )
    assert read_only.reason_code != write.reason_code, "原因码也应不同（否则无法审计归因）"


def test_mock_adapter_is_input_invariant(catalog, facts):
    """反向对照：`mock` **刻意**对任何输入都给同一结果。

    这不是缺陷——mock 的职责只是证明"契约能被实现"。把它写成用例，
    是为了让两者的差别**显式**：`rules` 才是那个真的在做判定的实现。
    """
    first = _decide("mock", "Route",
                    request={"input_ref": "input-demo", "tool_id": "erp.stock.query"},
                    catalog=catalog, facts=facts)
    second = _decide("mock", "Route",
                     request={"input_ref": "input-demo", "tool_id": "pmc.dispatch.publish"},
                     catalog=catalog, facts=facts)
    assert (first.route, first.category, first.reason_code) == (
        second.route, second.category, second.reason_code
    )


def test_rules_adapter_refuses_rank_and_select_with_a_reason(catalog, facts):
    """`rules` 对 `Rank`/`Select` 的拒绝**要说明缺什么**，不是一句"未实现"。

    这两者的拒绝理由是**设计性的**（缺排序依据/缺候选集），
    不是"还没来得及写"。消息里必须能读出这个区别，否则下一个人会误以为
    补个代码就行——而实际上补的是**数据**。
    """
    for kind, needle in (("Rank", "排序依据"), ("Select", "候选集")):
        with pytest.raises(DecisionError) as excinfo:
            _decide("rules", kind, catalog=catalog, facts=facts)
        assert excinfo.value.code == "DECISION_KIND_NOT_IMPLEMENTED"
        assert needle in excinfo.value.message, (
            f"{kind} 的拒绝理由没说明缺什么：{excinfo.value.message!r}"
        )


def test_rules_adapter_refuses_without_a_risk_basis(catalog, facts):
    """没有可判定的风险等级时，`rules` **必须拒绝**，不得猜。

    猜错风险等级的后果是**把写入当成只读**——那正是这套风险授权要防的事。
    上游 §7：「风险级别由可信工具元数据与业务规则计算，**不由模型填写决定**；
    未知工具/动作**默认拒绝**」。
    """
    with pytest.raises(DecisionError) as excinfo:
        _decide("rules", "Route",
                request={"input_ref": "input-demo", "tool_id": "tool-not-registered"},
                catalog=catalog, facts=facts)
    assert excinfo.value.code == "DECISION_FACTS_UNAVAILABLE"
    assert "风险等级" in excinfo.value.message


def test_rules_adapter_refuses_without_facts(catalog):
    """拿不到 `facts` ⇒ 拒绝（不是"退回默认行为"）。"""
    with pytest.raises(DecisionError) as excinfo:
        _decide("rules", "Route", catalog=catalog, facts=None)
    assert excinfo.value.code == "DECISION_FACTS_UNAVAILABLE"


# ==========================================================================
# 三、facts 不得夹带租户私密数据
# ==========================================================================

def test_decision_facts_exclude_tenant_identifiers(facts):
    """交给适配器的 `facts` **不得含租户名单**（上游 §9：decision 只依赖
    「security 模型接口、批准的规则版本」）。

    适配器若拿得到租户名单，就会有人顺手用它——那会让决策服务成为
    跨租户信息的泄漏面。**能拿到就会有人用**，所以从源头不给。
    """
    text = json.dumps(facts, ensure_ascii=False)
    leaked = [tid for tid in ("tenant-demo", "tenant-c3", "tenant-ghost") if tid in text]
    assert not leaked, f"facts 里出现了租户标识：{leaked}"
    assert "tenants" not in facts, "facts 不应包含 tenants 节点"
