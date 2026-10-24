"""Jev 适配层门禁（上游 §3 / §10.1 / §13 / §16.1）。

## 为什么单独一组

上游 §3 明写 Jev 是**逻辑角色及决策接口**，而

> **真实 Jev 产品、兼容实现或本地决策模型均需实现同一契约**；
> §13「实际版本与 Hermes/**Jev 适配方式需 P0 冻结**」

所以本组守的不是"决策对不对"（那是业务），而是**适配方式这三件事**：

1. **契约封闭**：决策类型只能取上游 §3 列出的那 8 种，不能自造；
2. **换实现不改内核**：内核不得残留任何决策逻辑（残留即意味着换实现要动内核）；
3. **fail-closed**：适配器未配置 / 配错 / 请求未实现的类型，一律**显式拒绝**，
   而不得静默退回某个默认实现或默认类型。

第 3 条是这一层最容易出的问题：决策服务一旦"猜"，
调用方拿到的是**看起来成功的错误答案**，比报错危险得多。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from packages.decision import (
    DECISION_KINDS,
    MOCK_CONFIDENCE,
    DecisionError,
    MockDecisionAdapter,
    adapter_ids,
    resolve_adapter,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DECISION_PKG = REPO_ROOT / "packages" / "decision"
ROUTES = REPO_ROOT / "services" / "_kernel" / "routes.py"

#: 上游 §3 的**封闭清单**（逐字抄录，顺序亦同）
UPSTREAM_KINDS = (
    "Route",
    "Classify",
    "Score",
    "Rank",
    "Filter",
    "Gate",
    "Select",
    "Escalate",
)


def _catalog(**overrides) -> dict:
    catalog = {
        "catalog_version": "catalog-mock-1",
        "routes": ["route-demo"],
        "actions": ["action-demo"],
        "categories": ["category-demo"],
        "reason_codes": ["reason-demo"],
        "fallback_routes": ["fallback-demo"],
    }
    catalog.update(overrides)
    return catalog


# ==========================================================================
# 一、契约封闭：决策类型只能来自上游清单
# ==========================================================================

def test_decision_kinds_match_upstream_verbatim():
    """8 种决策类型必须与上游 §3 **逐字、逐序**一致。

    这里的清单是从上游文档抄下来的常量——**故意不用任何推导**。
    推导出来的清单只能证明"自己和自己一致"，证明不了"和上游一致"。
    """
    assert DECISION_KINDS == UPSTREAM_KINDS, (
        "决策类型清单与上游 §3 不一致；\n"
        f"  上游：{list(UPSTREAM_KINDS)}\n"
        f"  实现：{list(DECISION_KINDS)}"
    )


def test_decision_kind_enum_in_contract_matches_the_package():
    """契约里的 `decision_kind` 枚举必须与包里的清单一致（两处不许漂移）。"""
    import json

    api_models = json.loads(
        (REPO_ROOT / "packages/contracts/schemas/api-models.json").read_text(encoding="utf-8")
    )
    request_enum = api_models["$defs"]["DecisionRequest"]["properties"]["decision_kind"]["enum"]
    assert tuple(request_enum) == DECISION_KINDS

    result = json.loads(
        (REPO_ROOT / "packages/contracts/schemas/decision-result.json").read_text(encoding="utf-8")
    )
    assert tuple(result["properties"]["decision_kind"]["enum"]) == DECISION_KINDS


def test_unknown_kind_is_rejected():
    """自造的决策类型必须被拒（上游 §3「输出有限类型决策」）。"""
    adapter = resolve_adapter({"implementation": "mock"})
    with pytest.raises(DecisionError) as excinfo:
        adapter.decide(
            kind="Summarize",  # 不在上游清单里
            request={"input_ref": "input-demo"},
            catalog=_catalog(),
            calibration_version=None,
        )
    assert excinfo.value.code == "DECISION_KIND_UNKNOWN"


# ==========================================================================
# 二、换实现不改内核
# ==========================================================================

def test_every_upstream_kind_is_either_implemented_or_explicitly_refused():
    """8 种类型**逐个**都要有明确归宿：实现，或显式拒绝。

    **不允许第三种情况**：静默返回一个看起来对的结果。
    这条遍历全部 8 种，所以将来加了目录却忘了实现，这里会红。
    """
    adapter = resolve_adapter({"implementation": "mock"})
    implemented = set(adapter.supported_kinds())

    for kind in DECISION_KINDS:
        if kind in implemented:
            outcome = adapter.decide(
                kind=kind,
                request={"input_ref": "input-demo"},
                catalog=_catalog(),
                calibration_version=None,
            )
            assert outcome.decision_kind == kind, (
                f"请求 {kind} 却回显 {outcome.decision_kind}——调用方无法判断拿到的是哪类决策"
            )
        else:
            with pytest.raises(DecisionError) as excinfo:
                adapter.decide(
                    kind=kind,
                    request={"input_ref": "input-demo"},
                    catalog=_catalog(),
                    calibration_version=None,
                )
            assert excinfo.value.code == "DECISION_KIND_NOT_IMPLEMENTED", (
                f"未实现的 {kind} 必须显式拒绝，实际 {excinfo.value.code}"
            )


def test_implemented_kinds_are_declared_in_the_registry():
    """登记表声明的已覆盖类型 = **各适配器实现的并集**。

    语义要说清：`implemented_decision_kinds` 描述的是**服务整体**能不能处理某类决策，
    而不是某一个适配器。所以判据取并集——单看一个适配器会误报
    （例如 `rules` 实现了 6 种而 `mock` 只实现 4 种）。

    同时反查：**每个适配器自称支持的类型都必须是上游清单的子集**，
    不许有适配器冒出清单外的类型。
    """
    import json

    from packages.decision import adapter_ids, resolve_adapter

    registry = json.loads(
        (REPO_ROOT / "services/_kernel/registry.mock.json").read_text(encoding="utf-8")
    )
    declared = tuple(registry["decision_catalog"]["implemented_decision_kinds"])

    union: list[str] = []
    for adapter_id in adapter_ids():
        kinds = resolve_adapter({"implementation": adapter_id}).supported_kinds()
        for kind in kinds:
            assert kind in DECISION_KINDS, (
                f"适配器 {adapter_id} 自称支持清单外的类型 {kind!r}"
            )
            if kind not in union:
                union.append(kind)

    # 按上游清单的顺序比对，避免"顺序不同"造成无意义的失败
    assert tuple(k for k in DECISION_KINDS if k in union) == tuple(
        k for k in DECISION_KINDS if k in declared
    ), (
        f"登记表声明覆盖 {declared}，各适配器实际并集 {tuple(union)}"
    )

    # 决策类型的全集也必须与上游清单一致
    assert tuple(registry["decision_catalog"]["decision_kinds"]) == DECISION_KINDS


def test_kernel_no_longer_contains_decision_logic():
    """内核**不得**再残留决策实现（残留 = 换实现要改内核）。

    判据取"内核有没有自己去读决策目录的清单"：
    抽层之后，`routes.py` 只应通过适配器取结果，
    不应再出现 `catalog.get("routes")` 这类直取。
    """
    source = ROUTES.read_text(encoding="utf-8")
    for marker in ('catalog.get("routes")', 'catalog.get("fallback_routes")'):
        assert marker not in source, (
            f"内核里仍有 {marker}——决策逻辑没抽干净，"
            "换实现时仍要改内核（与上游 §3「均需实现同一契约」冲突）"
        )
    # 反过来：内核**必须**经由适配层
    assert "resolve_decision_adapter" in source or "decision_adapter_for" in source


def test_decision_package_is_pure():
    """`packages/decision/` 是纯逻辑：不得读写文件、连库、发网络请求。

    与 `packages/domain/` 同一约定（上游 §15：
    「共享包只共享契约和纯函数」）。
    """
    banned = {"requests", "urllib", "socket", "http", "sqlite3", "psycopg", "redis"}
    problems: list[str] = []
    for path in sorted(DECISION_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in banned:
                        problems.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in banned:
                    problems.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in {"open", "urlopen"}:
                    problems.append(f"{path.name}: 调用 {name}()")
    assert not problems, "决策包不是纯的：\n  " + "\n  ".join(problems)


# ==========================================================================
# 三、fail-closed：不许猜
# ==========================================================================

@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({}, id="未配置"),
        pytest.param({"config": {}}, id="implementation 缺失"),
        pytest.param({"implementation": ""}, id="implementation 为空"),
        pytest.param({"implementation": None}, id="implementation 为 null"),
        pytest.param({"implementation": "real-jev-v9"}, id="未登记的实现"),
        pytest.param("mock", id="整节点写成了字符串"),
    ],
)
def test_adapter_resolution_fails_closed(entry):
    """适配器解析的每一种坏输入都必须**拒绝**，不得退回默认实现。

    **退回 mock 的后果**：生产配置写错时，系统用一个确定性 Mock 做决策，
    而调用方与界面**看不出区别**——那是最难发现的一类故障。
    """
    with pytest.raises(DecisionError) as excinfo:
        resolve_adapter(entry)
    assert excinfo.value.code in (
        "DECISION_ADAPTER_NOT_CONFIGURED",
        "DECISION_ADAPTER_UNKNOWN",
    ), f"{entry!r} 报了非预期错误码 {excinfo.value.code}"


def test_mock_is_registered_and_resolvable():
    """反向判据：正确配置必须**能**解析（否则上面的拒绝可能是"永远拒绝"）。"""
    assert "mock" in adapter_ids()
    adapter = resolve_adapter({"implementation": "mock"})
    assert adapter.adapter_id == "mock"
    assert isinstance(adapter, MockDecisionAdapter)


def test_incomplete_catalog_is_refused_not_guessed():
    """目录缺清单 ⇒ 拒绝，**不得**给个默认值凑合过去。"""
    adapter = resolve_adapter({"implementation": "mock"})
    with pytest.raises(DecisionError) as excinfo:
        adapter.decide(
            kind="Route",
            request={"input_ref": "input-demo"},
            catalog=_catalog(routes=[]),
            calibration_version=None,
        )
    assert excinfo.value.code == "DECISION_CATALOG_INCOMPLETE"


# ==========================================================================
# 四、置信度不赋执行权（上游 §3 / §8.2）
# ==========================================================================

def test_confidence_is_null_without_calibration():
    """无有效校准时 `confidence` 必须为 `None`。

    上游 §3「高置信**不得提升权限**」；§8.2「置信度是经独立数据校准的
    任务级指标，**不能取 LLM 自报数字**」。
    """
    adapter = resolve_adapter({"implementation": "mock"})
    outcome = adapter.decide(
        kind="Route",
        request={"input_ref": "input-demo"},
        catalog=_catalog(),
        calibration_version=None,
    )
    assert outcome.confidence is None
    assert outcome.model_version is None, "无校准时不应声称某个模型版本产出了结论"


def test_mock_confidence_is_not_in_the_auto_candidate_band():
    """Mock 的置信度**不得**落在自动候选区间（上游 §8.2：`> 0.92`）。

    **Mock 也不该示范"高置信即可自动"**：那是这套系统最危险的暗示。
    """
    assert MOCK_CONFIDENCE <= 0.92, (
        f"Mock 置信度 {MOCK_CONFIDENCE} 落在自动候选区间（>0.92）——"
        "会给人「高置信就能自动执行」的错误示范"
    )


def test_escalate_reports_no_confidence():
    """`Escalate`（升级转人工）**必须无置信度**。

    升级的语义就是"机器不确定"。此时给一个置信度是**自相矛盾**的：
    既说"我不确定要转人工"，又给一个数字暗示确定程度。
    """
    adapter = resolve_adapter({"implementation": "mock"})
    outcome = adapter.decide(
        kind="Escalate",
        request={"input_ref": "input-demo"},
        catalog=_catalog(),
        calibration_version="calib-demo",  # 即使有校准，也不给
    )
    assert outcome.confidence is None
    assert outcome.route == "fallback-demo", "升级应路由到 fallback（转人工那条路）"
