"""共享领域包门禁（上游 §15 `packages/domain/`）。

> 共享包只共享契约和纯函数，**不共享业务 repository、数据库连接或服务私有模型**。

这一组用例守四件事：

1. **一致性**：中心的观测侧与现场的判定侧必须用**同一份**词表——
   两份会漂移，而漂移的表现是"某条判定被归到一个不存在的阶段"，**不报错**；
2. **契约对齐**：阶段名必须与 `p4-guard.json` 的 `Check.stage` 枚举**逐字一致**
   （以契约为准，不以实现为准）；
3. **漂移探针**：`edge.py` 里实际产出的每个原因码都必须在 domain 登记过——
   漏登记即报红，而不是静默归到 `unknown` 桶；
4. **纯函数性**：本包不得读写文件、不得连库、不得依赖任何服务。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from packages.domain import behavior_change, guard, release

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOMAIN_DIR = REPO_ROOT / "packages" / "domain"

# ==========================================================================
# 一、词表自洽
# ==========================================================================

def test_stage_vocabulary_is_self_consistent():
    """映射里的每个阶段都必须已声明，且每个阶段都有原因码归属。"""
    problems = guard.stage_vocabulary_problems()
    assert not problems, "领域词表自检失败：\n  " + "\n  ".join(problems)


def test_stages_are_derived_from_the_reason_mapping():
    """**阶段表必须由原因码归属派生**，不是另写一份名单。

    两份名单一定会漂移。这里断言"派生"这个性质本身：
    阶段集合恰好等于映射的值集合。
    """
    assert set(guard.GUARD_STAGES) == set(guard.GUARD_REASON_TO_STAGE.values())


def test_confidence_is_the_last_stage():
    """上游 §9：置信度在**最后**——它不能覆盖以上任何一项判定。"""
    assert guard.GUARD_STAGE_ORDER[-1] == "confidence"
    assert guard.GUARD_STAGES[-1] == "confidence"


def test_stage_order_has_no_duplicates_and_covers_all_stages():
    assert len(guard.GUARD_STAGE_ORDER) == len(set(guard.GUARD_STAGE_ORDER))
    assert set(guard.GUARD_STAGE_ORDER) >= set(guard.GUARD_REASON_TO_STAGE.values())


# ==========================================================================
# 二、与契约逐字对齐（以契约为准，不以实现为准）
# ==========================================================================

def _contract_stage_enum() -> list[str]:
    document = json.loads(
        (REPO_ROOT / "packages" / "contracts" / "schemas" / "p4-guard.json").read_text(encoding="utf-8")
    )
    return list(document["$defs"]["Check"]["properties"]["stage"]["enum"])


def test_stage_order_matches_the_contract_exactly():
    """阶段名与顺序都必须与契约 `p4-guard.json` 一致。

    曾经实现里自带一套名字（ood / drift / license / scope 各占一级），
    结果同一语义在契约与实现里有两套术语——对接时必须翻译一次，**而翻译就会出错**。
    """
    assert list(guard.GUARD_STAGE_ORDER) == _contract_stage_enum()


# ==========================================================================
# 三、漂移探针：实现里的原因码必须全部登记
# ==========================================================================

def _reason_codes_in(source: str) -> list[str]:
    """从 Guard 实现里抓出原因码（`failures.append((stage, "CODE..."))` 形状）。"""
    return re.findall(r'failures\.append\(\s*\(\s*"[a-z_]+"\s*,\s*f?"([A-Z][A-Z0-9_]*)', source)


def test_drift_probe_actually_detects_unregistered_codes():
    """**能力自检**：喂构造输入，探针必须报出未登记的原因码。

    没有这条，探针可能在"探测能力为零"的情况下长期绿灯——
    干净树上"没有未登记码"与"根本不会识别未登记码"**在观测上无法区分**。
    本项目在仓库卫生守卫上踩过同一个坑（把检查改成 `if False` 仍然全绿），
    所以这里的判据必须**自己证明自己有效**。
    """
    fabricated = ["SOME_FUTURE_CODE", "ANOTHER_FUTURE_CODE:detail", "DRIFT_EXCEEDED"]
    unregistered = guard.unregistered_reason_codes(fabricated)
    assert unregistered == ["ANOTHER_FUTURE_CODE", "SOME_FUTURE_CODE"], (
        f"探针未正确识别未登记原因码（已登记的 DRIFT_EXCEEDED 不应出现）：{unregistered}"
    )


def test_drift_probe_returns_empty_for_fully_registered_input():
    """反向自检：全部已登记时必须返回空，不得误报。"""
    registered = list(guard.GUARD_REASON_TO_STAGE)
    assert guard.unregistered_reason_codes(registered) == []


def test_every_reason_code_in_the_guard_implementation_is_registered():
    """**实现里出现的每个原因码都必须在 domain 登记。**

    这是"两份词表漂移"的机械探针：漏登记时指标会把该判定静默归入 `unknown`，
    于是"某个阶段的拒绝率"长期偏低而无人发现。
    """
    source = (REPO_ROOT / "services" / "_kernel" / "edge.py").read_text(encoding="utf-8")
    codes = _reason_codes_in(source)
    assert codes, "未能从 Guard 实现里解析出任何原因码——探针本身失效了"
    unregistered = guard.unregistered_reason_codes(codes)
    assert not unregistered, (
        f"以下原因码在 Guard 实现里产出、但未在 packages/domain/guard.py 登记：{unregistered}"
    )


def test_metrics_and_guard_share_one_vocabulary():
    """观测侧（metrics）与现场侧（edge）必须用**同一份**阶段词表。

    这里断言对象相等，而不只是"值看起来一样"——值相等可能只是碰巧。
    """
    from services._kernel import edge, metrics

    assert metrics._REASON_TO_STAGE == guard.GUARD_REASON_TO_STAGE
    assert tuple(metrics.STAGE_LABELS) == tuple(guard.GUARD_STAGES) + ("unknown",)
    assert edge.GUARD_STAGES == guard.GUARD_STAGES


def test_edge_uses_the_guard_fallback_vocabulary_in_behavior():
    """`edge` 必须**按 guard 的词表**判定回退需求——用行为验，而不是再导出。

    ## 为什么改了这条（2026-10-05）

    原来这里断言的是 `edge.REASONS_REQUIRING_FALLBACK == guard.REASONS_REQUIRING_FALLBACK`
    ——通过 `edge` 对 guard 的**再导出**来验证。但再导出本身是死代码
    （生产里没人用 `edge.REASONS_REQUIRING_FALLBACK`），所以本轮把它去掉了，
    并让 `edge.evaluate_guard` **调用** `guard.reason_requires_fallback`。

    只验"再导出相等"有个盲区：两边可以各自演进、只要再导出还指着 guard 就通过，
    而**真正生效的判据**可能已经不在用它了。所以这里改成验**行为**。
    """
    from services._kernel import edge

    def verdict_for(**overrides):
        moment = "2026-10-05T00:00:00Z"
        base = {"input_at": moment, "now": moment}
        base.update(overrides)
        return edge.evaluate_guard(edge.GuardInput(**base))

    # ① 一个要求回退的原因码 ⇒ edge 必须给出 FALLBACK（而不是 DENY）
    assert guard.reason_requires_fallback("INTERLOCK_DENIED") is True, "前提：它要求回退"
    assert verdict_for(interlock_passed=False)["verdict"] == edge.Verdict.FALLBACK, (
        "`edge` 未按 guard 词表把硬互锁失败判为**回退**"
    )
    assert verdict_for(interlock_passed=False)["requires_device_baseline"] is True

    # ② 一个**不**要求回退的原因码 ⇒ edge 必须给出 DENY
    assert guard.reason_requires_fallback("CONFIDENCE_NOT_CALIBRATED") is False, "前提：不要求回退"
    assert verdict_for(confidence_is_calibrated=False)["verdict"] == edge.Verdict.DENY, (
        "`edge` 把不要求回退的原因码判成了回退"
    )

    # ③ 未登记原因码不得被猜成"要求回退"
    assert guard.reason_requires_fallback("SOME_FUTURE_CODE:x") is False
    # ④ 大小写/空白必须规范化
    assert guard.reason_requires_fallback("  interlock_denied  ") is True
    assert guard.reason_requires_fallback("INTERLOCK_DENIED:detail") is True


def test_metric_stage_labels_available_to_metrics_are_all_real_or_unknown():
    """指标可用标签必须是"真实阶段 + unknown"——不得凭空多出别的桶。"""
    from services._kernel import metrics

    extra = set(metrics.STAGE_LABELS) - set(guard.GUARD_STAGES) - {"unknown"}
    assert not extra, f"指标引入了未在领域包声明的阶段标签：{sorted(extra)}"


def test_unknown_reason_maps_to_unknown_not_to_an_arbitrary_stage():
    """未登记原因码**不得**被塞进任意阶段——那会污染判定口径。"""
    assert guard.stage_of_reason("SOME_FUTURE_CODE:x") == "unknown"
    assert guard.stage_of_reason("DRIFT_EXCEEDED") == "ood_drift"
    # 带 detail 后缀的也要能解析
    assert guard.stage_of_reason("MISSING_FIELD:pressure") == "structure"


def test_reason_code_parsing_is_case_insensitive_and_trims():
    assert guard.stage_of_reason("  drift_exceeded  ") == "ood_drift"


# ==========================================================================
# 四、优先级：上游硬顺序
# ==========================================================================

def test_highest_priority_stage_follows_the_declared_order():
    """`structure` 比 `confidence` 优先；`confidence` 最低。"""
    assert guard.highest_priority_stage(["CONFIDENCE_MISSING", "MISSING_FIELD"]) == "structure"
    assert guard.highest_priority_stage(["CONFIDENCE_MISSING", "INTERLOCK_DENIED"]) == "interlock"
    assert guard.highest_priority_stage(["CONFIDENCE_MISSING"]) == "confidence"


def test_highest_priority_stage_is_none_when_nothing_is_known():
    """全部是未登记原因码时返回 `None`——不猜一个阶段出来。"""
    assert guard.highest_priority_stage(["FUTURE_A", "FUTURE_B"]) is None
    assert guard.highest_priority_stage([]) is None


def test_reasons_requiring_fallback_are_all_registered():
    """要求回退的原因码必须已登记（否则它连阶段都归不了）。"""
    unregistered = guard.unregistered_reason_codes(guard.REASONS_REQUIRING_FALLBACK)
    assert not unregistered, f"要求回退但未登记的原因码：{unregistered}"


def test_reason_requires_fallback_matches_the_declared_set():
    assert guard.reason_requires_fallback("INTERLOCK_DENIED")
    assert guard.reason_requires_fallback("OOD")
    assert not guard.reason_requires_fallback("CONFIDENCE_BELOW_THRESHOLD")
    assert not guard.reason_requires_fallback("BUDGET_EXCEEDED")


# ==========================================================================
# 五、发布状态机
# ==========================================================================

def test_release_states_match_the_declared_seven():
    assert release.EDGE_RELEASE_STATES == (
        "DRAFT", "SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED", "ACTIVE", "REVOKED",
    )


def test_only_adjacent_forward_transitions_are_allowed():
    assert release.release_transition_allowed("DRAFT", "SCHEMA_FROZEN")
    assert not release.release_transition_allowed("DRAFT", "CALIBRATED")
    assert not release.release_transition_allowed("DRAFT", "ACTIVE")


def test_revoked_is_terminal_and_reachable_from_any_non_terminal_state():
    for state in release.EDGE_RELEASE_STATES[:-1]:
        assert release.release_transition_allowed(state, "REVOKED"), f"{state} → REVOKED 应允许"
    for state in release.EDGE_RELEASE_STATES:
        assert not release.release_transition_allowed("REVOKED", state), "REVOKED 是终态"


def test_unknown_states_are_rejected():
    assert not release.release_transition_allowed("NOPE", "DRAFT")
    assert not release.release_transition_allowed("DRAFT", "NOPE")


def test_backward_transition_is_not_allowed():
    """不允许回退到前一个状态——回退意味着"带着旧批准往回走"，上游禁止。"""
    assert not release.release_transition_allowed("CALIBRATED", "SCHEMA_FROZEN")


# ==========================================================================
# 六、行为性变化
# ==========================================================================

def test_every_behavioral_kind_has_a_required_stage():
    for kind in behavior_change.BEHAVIORAL_CHANGE_KINDS:
        assert kind in behavior_change.BEHAVIORAL_CHANGE_REQUIRED_STAGE, f"{kind} 未声明所需阶段"


def test_required_stages_are_declared_release_states():
    for kind, stage in behavior_change.BEHAVIORAL_CHANGE_REQUIRED_STAGE.items():
        assert stage in release.EDGE_RELEASE_ORDER, f"{kind} 指向了未声明的状态 {stage!r}"


def test_behavioral_change_invalidates_approval():
    impact = behavior_change.behavioral_change_impact(
        ["schema"], order=release.EDGE_RELEASE_ORDER
    )
    assert impact["behavioral"] is True
    assert impact["invalidates_approval"] is True
    assert impact["required_stage"] == "SCHEMA_FROZEN"


def test_impact_returns_the_earliest_required_stage():
    """多项变化时回到**最早**那个阶段——只要有一项要求更早，就得回到更早。"""
    impact = behavior_change.behavioral_change_impact(
        ["model", "schema"], order=release.EDGE_RELEASE_ORDER
    )
    assert impact["required_stage"] == "SCHEMA_FROZEN"


def test_non_behavioral_change_does_not_invalidate_approval():
    impact = behavior_change.behavioral_change_impact(
        ["copy_text"], order=release.EDGE_RELEASE_ORDER
    )
    assert impact["behavioral"] is False
    assert impact["invalidates_approval"] is False


# ==========================================================================
# 七、纯函数性：本包不得依赖文件 / 网络 / 数据库 / 服务
# ==========================================================================

FORBIDDEN_IMPORTS = (
    "os", "json", "pathlib", "sqlite3", "subprocess", "socket", "requests",
    "psycopg2", "services", "integrations",
)


@pytest.mark.parametrize("path", sorted(DOMAIN_DIR.glob("*.py")), ids=lambda p: p.name)
def test_domain_modules_import_nothing_from_the_outside(path):
    """上游明文：共享包只共享契约和纯函数，**不共享 repository / 连接 / 服务模型**。

    用 AST 读 import，而不是字符串匹配——注释里提到 `json` 不算违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：包内互引，允许
                continue
            imported.append((node.module or "").split(".")[0])
    offenders = sorted(set(imported) & set(FORBIDDEN_IMPORTS))
    assert not offenders, f"{path.name} 引入了不允许的外部依赖：{offenders}"


@pytest.mark.parametrize("path", sorted(DOMAIN_DIR.glob("*.py")), ids=lambda p: p.name)
def test_domain_modules_are_importable_without_a_runtime(path):
    """模块必须能在**无仓库状态、无环境变量**的情况下导入成功。

    这是"纯函数"最实际的判据：若它读取文件或环境，导入就会失败。
    """
    module_name = f"packages.domain.{path.stem}"
    if path.stem == "__init__":
        module_name = "packages.domain"
    __import__(module_name)
