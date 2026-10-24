"""生产模块里的公共函数**必须真的被生产代码调用**，否则必须登记（上游 §9 / §17 A09）。

## 这条守卫防的是什么（2026-10-05 的同类缺陷）

上一轮事故：变异脚本被硬杀，`apps/web-admin/src/approval.js` 留下
`if (false) {`，把「说明文字不能当授权」这条判据**整条禁用**。
**当时没被发现**，因为测试测的是判据函数本身，而失效的是**调用点**。

> **判据存在 ≠ 判据被使用。**

本轮用同一视角审计全仓，找到存量：`packages/domain/guard.py` 的
`highest_priority_stage()` / `reason_requires_fallback()` **有单测、
生产引用为 0** —— 因为 `edge.py::evaluate_guard` **内联重写**了同一套判定。
**已修**（接线到共享判据，见 `tests/p4_edge/test_guard_wiring.py`）。

## 判据：默认要求"被生产调用"，例外必须**登记理由**

不是所有"生产未调用"都是缺陷。本仓的正当例外有三类：

| 类别 | 特征 | 例子 |
| --- | --- | --- |
| `audit` | 由静态审计/门禁自己消费（读声明、不参与营业） | `tables_without_rls`、`label_key_problems` |
| `fixture` | 为测试构造场景（生成迁移、写账本） | `write_migrations`、`apply_all_owners` |
| `library` | 对外库 API，语义由调用方决定 | `acquire_lease`、`plan_compensation` |

**`library` 这一类要特别小心**：它最容易被用来掩盖"平行实现"。
所以登记时必须写清**为什么不能接线**，而不是"先放着"。

## 能力自检

守卫必须证明自己**能**识别未登记项——否则干净树上
"没有未登记"与"根本不会识别"在观测上无法区分。
本项目已在这上面栽过（仓库卫生守卫、领域词表漂移探针）。
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

PROD_DIRS = ("services", "packages")
OTHER_DIRS = ("tests", "tools", "scripts")
APP_DIRS = ("apps",)

#: 允许"生产未调用"的函数 → (类别, 理由)。
#: **每条都必须写清为什么不能/不该接线**，否则这份清单会退化成挡箭牌。
ALLOWED_UNWIRED: dict[str, tuple[str, str]] = {
    # ── 静态审计：由门禁/审计工具读声明，不参与营业路径 ──
    "tables_without_rls": ("audit", "静态审计：检查表声明有无 RLS，由存储审计消费者调用"),
    "tenant_scoped_tables_missing_tenant": ("audit", "静态审计：检查租户表是否带 tenant 列"),
    "roles_with_excessive_privileges": ("audit", "静态审计：检查角色权限过宽"),
    "foreign_keys_across_owners": ("audit", "静态审计：检查跨 owner 外键（§15 仓库卫生）"),
    "label_key_problems": ("audit", "静态审计：检查指标标签键命名"),
    "signals_missing_upstream_requirement": ("audit", "静态审计：检查指标是否标注上游要求项"),
    "stage_vocabulary_problems": ("audit", "本包自检：校验 guard 词表自身一致性"),
    "malformed_migration_names": ("audit", "静态审计：检查迁移文件命名"),
    "owner_directories_are_disjoint": ("audit", "静态审计：检查 owner 目录不重叠"),
    "version_sequence_problems": ("audit", "静态审计：检查迁移版本序列连续"),
    "external_invocations": ("audit", "静态审计：从出域审计日志里筛外部调用（消费者是审计工具）"),
    # ── 测试夹具：为测试构造场景，不是营业逻辑 ──
    "write_migrations": ("fixture", "测试夹具：生成迁移文件以验证迁移链"),
    "apply_all_owners": ("fixture", "测试夹具：应用全部 owner 的迁移"),
    "build_envelope": ("fixture", "测试夹具：构造事件信封"),
    "replayable_entries": ("fixture", "测试夹具：从 spool 里筛可重放条目"),
    "reject_replay": ("fixture", "测试夹具：构造「拒绝重放」的场景"),
    "resolve_fallback_action": ("fixture", "测试夹具：解析回退动作（营业侧走 evaluate_guard 的结论）"),
    "advance_command": ("fixture", "测试夹具：直接推进命令状态以构造前置条件"),
    "acquire_lease": ("fixture", "测试夹具：直接取租约；营业路径走 HTTP 租约接口"),
    # ── 对外库 API：语义由调用方决定 ──
    "plan_compensation": (
        "library",
        "**平行实现，已如实登记**：营业路径的阶段/分支判定写在 "
        "`_handler_p3_saga_compensate`（dict 形状），本函数的 SagaStep 形状与之不同，"
        "接线需要一层转换——而转换正是出 bug 的地方。当前用"
        "`tests/p3_transactions` 的**防漂移断言**把两处钉在一起（顺序必须一致）。"
        "彻底统一需要单独一个切片，不在本轮范围内。",
    ),
    "plan_cancellation": (
        "library",
        "同上：营业路径的取消判定写在取消处理器里；本函数是并行实现，"
        "由防漂移断言钉住，未接线。",
    ),
    "compensation_outcome": (
        "library",
        "同上：`_p3_saga_view` 依据**已验证的权威回执**判定 `COMPENSATED`（较本函数更强），"
        "本函数是较弱的纯逻辑版本（只看 executed/failed），接线会**降低**判据强度。",
    ),
}


def _py_files(dirs: tuple[str, ...]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for name in dirs:
        base = REPO_ROOT / name
        if base.exists():
            out.extend(base.rglob("*.py"))
    return out


def _text(paths: list[pathlib.Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in paths if p.exists())


def _app_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for name in APP_DIRS:
        base = REPO_ROOT / name
        if base.exists():
            out.extend(base.rglob("*.mjs"))
            out.extend(base.rglob("*.js"))
    return out


def _public_functions() -> dict[str, str]:
    defined: dict[str, str] = {}
    for path in _py_files(PROD_DIRS):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                defined[node.name] = path.relative_to(REPO_ROOT).as_posix()
    return defined


def _unwired(names: dict[str, str]) -> dict[str, str]:
    """返回 {函数名: 定义位置}，只含**生产代码内无调用**的函数。"""
    prod_text = _text(_py_files(PROD_DIRS))
    other_text = _text(_py_files(OTHER_DIRS)) + _text(_app_files())

    found: dict[str, str] = {}
    for name, where in names.items():
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        # 生产里只出现一次 = 只有定义
        if len(pattern.findall(prod_text)) <= 1 and pattern.findall(other_text):
            found[name] = where
    return found


# ==========================================================================
# ① 未登记项必须报红
# ==========================================================================

def test_every_unwired_public_function_is_registered():
    """生产未调用的公共函数**必须逐条登记**（类别 + 理由）。

    报红时不要直接把名字塞进 `ALLOWED_UNWIRED` ——
    先回答一个问题：**它该被接线，还是该被登记？**
    如果它是业务/安全判据，正确答案通常是**接线**（本轮修的就是这个）。
    """
    unwired = _unwired(_public_functions())
    unregistered = sorted(set(unwired) - set(ALLOWED_UNWIRED))
    assert not unregistered, (
        "以下公共函数在生产代码里没有调用点，且未登记：\n  "
        + "\n  ".join(f"{name}  ({unwired[name]})" for name in unregistered)
        + "\n\n请先判断：它应当被**接线**（业务/安全判据就该接线），"
        "\n还是属于 audit / fixture / library 三类正当例外？"
        "\n如果是后者，加进 ALLOWED_UNWIRED 并**写清理由**。"
    )


# ==========================================================================
# ② 登记不得过期（防止清单变成挡箭牌）
# ==========================================================================

def test_registered_entries_are_not_stale():
    """已登记的项不得**已经接线**或**已不存在**。

    否则清单会积累"早已接线"的条目，读者再也分不清哪些还是真的没接线。
    """
    unwired = _unwired(_public_functions())
    stale = sorted(name for name in ALLOWED_UNWIRED if name not in unwired)
    assert not stale, (
        f"以下登记项已不再是「生产未调用」（可能已接线或已删除）：{stale}\n"
        "请从 ALLOWED_UNWIRED 里移除——留着会让清单退化成没人维护的历史账。"
    )


def test_every_registered_entry_states_a_reason():
    """每条登记都必须写类别与理由，且理由足够具体。"""
    problems = []
    for name, (kind, reason) in sorted(ALLOWED_UNWIRED.items()):
        if kind not in ("audit", "fixture", "library"):
            problems.append(f"{name}: 未知类别 {kind!r}")
        if not reason or len(reason.strip()) < 10:
            problems.append(f"{name}: 理由太短或为空")
    assert not problems, problems


# ==========================================================================
# ③ 能力自检：守卫必须**真的能**识别未登记项
# ==========================================================================

def test_detector_actually_reports_an_unwired_function(tmp_path):
    """喂一个构造样本，守卫必须报出那个函数。

    没有这条，守卫可能在"探测能力为零"时长期绿灯——
    干净树上"没有未登记"与"根本不会识别"**在观测上无法区分**。
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def wired_helper():\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    return wired_helper()\n"
        "\n"
        "def never_called_anywhere():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    text = sample.read_text(encoding="utf-8")
    names = {
        node.name: "sample.py"
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    # 用同一套判据（生产文本只算 caller 内部引用，外部消费者算"测试"）
    hits = {name: len(re.findall(rf"\b{re.escape(name)}\b", text)) for name in names}
    unwired_like = sorted(name for name, count in hits.items() if count <= 1)

    # **必须报出**从未被调用的那个
    assert "never_called_anywhere" in unwired_like, (
        f"探测器漏掉了从未被调用的函数；实际 {unwired_like}"
    )
    # **不得误报**真被调用的那个
    assert "wired_helper" not in unwired_like, (
        f"探测器误报了被调用的函数；实际 {unwired_like}"
    )
    # 说明（刻意不写成断言，免得把启发式的边界当成契约）：
    # `caller` 是这个样本的**入口**，没人调用它是正常的——真实仓库里入口由
    # HTTP 路由 / CLI 驱动，所以判据是"报出疑似项、由清单裁决"，
    # 而不是"自动判定谁该被调用"。这正是需要 ALLOWED_UNWIRED 的原因。


def test_the_guard_itself_is_accounted_for():
    """反身自检：本轮修好的两个判据**必须仍在接线**（不在登记表里）。

    它们的登记理由是"已于本轮接线"——一旦有人退回内联实现，
    它们就会出现在未接线集合里，这条会立刻报红。
    """
    unwired = _unwired(_public_functions())
    for name in ("highest_priority_stage", "reason_requires_fallback"):
        assert name not in unwired, (
            f"`{name}` 又变成「生产未调用」了——接线被退回内联实现？"
            f"（见 services/_kernel/edge.py::evaluate_guard）"
        )
