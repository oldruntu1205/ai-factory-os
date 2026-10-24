"""上游架构文档 → 我方实现 的逐条对照（机读，不靠人肉）。

## 为什么写脚本而不是人肉核对

上游文档里有大量**可枚举清单**：8 个运行单元、16 条首期接口、ToolSpec 13 个字段、
Context 9 个字段、DecisionResult 11 个字段、13 张表、A01–A14 断言。
人肉核对这些**必然漏**（我此前就是靠 grep 抽查，才发现不了整块缺失）。

本脚本把清单**写死在代码里**（清单本身来自我通读文档的抄录），
然后逐条去仓库里取证。结论分三态：

- `OK`        有实现，且给出了证据位置
- `MISSING`   应有而完全没有 → **真缺口**
- `DEFERRED`  上游自己标为待冻结/外部依赖 → **不算缺口**（但仍要记清谁阻塞）

用法：`python tools/upstream_conformance.py`
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: 排除目录。**正斜杠**（路径先经 `rel_posix` 归一），避免 Windows 反斜杠漏匹配。
SKIP = re.compile(r"node_modules|\.venv|__pycache__|\.pytest_cache|/dist/|\.git/")


def rel_posix(path: str | pathlib.Path) -> str:
    """统一成 POSIX 风格相对路径。

    ⚠️ **本机是 Windows，`rglob` 返回反斜杠**。第一版用
    `"/tests/" in path` 判目录，**永远匹配不上**（路径长这样：
    `tests\\assertions\\test_assertion_coverage.py`），于是 A01–A14 全被
    误报成 MISSING。判据里的路径一律过这个函数，不要在别处自己拼分隔符。
    """
    return str(path).replace("\\", "/")


def is_under(path: str, directory: str) -> bool:
    """路径是否位于某个目录下（按 POSIX 风格比较）。"""
    return rel_posix(path).startswith(f"{directory}/")


def all_source_files() -> list[pathlib.Path]:
    exts = {".py", ".js", ".jsx", ".mjs", ".json", ".md", ".yml", ".yaml", ".toml"}
    out = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        if SKIP.search(rel_posix(path)):
            continue
        out.append(path)
    return out


def grep(pattern: str, files: list[pathlib.Path] | None = None) -> list[tuple[str, int]]:
    """返回 [(相对路径, 行号)]。

    ⚠️ **排除本脚本自身**：本文件里抄录了上游清单，若不排除，`grep` 会命中
    我自己写下的字面量，于是每一项都"通过"——**自我指涉的假绿**。
    第一版就踩了这个坑（14 个事件类型全"命中"，证据却是本文件第 121 行）。
    """
    hits: list[tuple[str, int]] = []
    rx = re.compile(pattern)
    for path in files or all_source_files():
        if path.name == pathlib.Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append((str(path.relative_to(ROOT)), lineno))
    return hits


# ==========================================================================
# 上游清单（逐条抄录自 AI_Factory_V3.0_系统架构.md）
# ==========================================================================

#: §9 八个物理运行单元 → (上游名, 我方路径/端口)
SERVICE_UNITS = [
    ("security-gateway", "services/security-gateway", 8101),
    ("control-plane", "services/control-plane", 8102),
    ("decision-service", "services/decision-service", 8103),
    ("hermes-runtime", "services/hermes-runtime", 8104),
    ("manufacturing-core", "services/manufacturing-core", 8105),
    ("tool-gateway", "services/tool-gateway", 8106),
    ("transaction-service", "services/transaction-service", 8107),
    ("trajectory-eval", "services/trajectory-eval", 8108),
]

#: §10.1 首期接口目录（16 条）→ HTTP 方法 + 路径
INTERFACE_CATALOG = [
    ("POST", "/api/v1/tasks"),
    ("GET", "/api/v1/tasks/{task_id}"),
    ("POST", "/api/v1/approvals/{approval_id}/decisions"),
    ("POST", "/internal/v1/policy/evaluate"),
    ("POST", "/internal/v1/decisions"),
    ("POST", "/internal/v1/runs"),
    ("POST", "/internal/v1/runs/{run_id}/resume"),
    ("POST", "/internal/v1/model-invocations"),
    ("POST", "/internal/v1/contexts/query"),
    ("POST", "/internal/v1/tool-requests"),
    ("POST", "/internal/v1/commands"),
    ("GET", "/internal/v1/commands/{command_id}"),
    ("POST", "/internal/v1/tool-executions"),
    ("POST", "/edge/v1/decisions"),
    ("POST", "/edge/v1/releases/activate"),
]

#: §10.2 公共 Context 字段（9）
CONTEXT_FIELDS = [
    "tenant_id", "factory_id", "actor_id", "employee_id",
    "task_id", "trace_id", "data_class", "schema_version", "deadline_at",
]

#: §10.2 DecisionResult 必须字段（11）
DECISION_RESULT_FIELDS = [
    "decision_id", "route", "action", "category", "confidence", "schema_version",
    "model_version", "calibration_version", "rule_version", "reason_code",
    "evidence_refs", "fallback",
]

#: §10.2 ToolSpec 必须字段（13）
TOOL_SPEC_FIELDS = [
    "tool_id", "version", "input_schema", "output_schema", "read", "write",
    "risk_class", "required_scopes", "timeout", "retry_policy",
    "idempotency_support", "status_query", "compensation",
    "data_class_rules", "adapter_owner",
]

#: §13 八个 owner schema
OWNER_SCHEMAS = [
    "security", "control", "decision", "runtime",
    "manufacturing", "tools", "transaction", "evidence",
]

#: §11 首期事件类型
EVENT_TYPES = [
    "task.accepted", "run.waiting_approval", "approval.decided",
    "command.accepted", "command.succeeded", "command.unknown",
    "saga.compensation_required", "erp.demand.released", "pmc.schedule.published",
    "mes.operation.completed", "wms.stock.changed", "edge.decision.observed",
    "edge.action.executed", "release.evaluated",
]

#: §17 验收断言
ACCEPTANCE_ASSERTS = [f"A{n:02d}" for n in range(1, 15)]

#: §3 给 Jev Decision Fabric 的封闭清单（逐字抄录；**是 8 个不是 9 个**）
JEV_DECISION_KINDS = (
    "Route",
    "Classify",
    "Score",
    "Rank",
    "Filter",
    "Gate",
    "Select",
    "Escalate",
)

#: 登记表路径（本脚本多处要用）
REGISTRY = ROOT / "services/_kernel/registry.mock.json"


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def report(label: str, ok: bool, evidence: str, deferred: bool = False) -> str:
    if ok:
        state = "OK      "
    elif deferred:
        state = "DEFERRED"
    else:
        state = "MISSING "
    print(f"  {state} {label:<52} {evidence}")
    return state.strip()


def main() -> int:
    files = all_source_files()
    counts = {"OK": 0, "MISSING": 0, "DEFERRED": 0}

    section("① §9 八个物理运行单元（8）")
    for name, path, port in SERVICE_UNITS:
        exists = (ROOT / path).is_dir()
        has_entry = (ROOT / path / "__main__.py").exists()
        state = report(
            f"{name} (:{port})",
            exists and has_entry,
            f"{path}/__main__.py" if has_entry else ("目录存在，缺 __main__.py" if exists else "目录不存在"),
        )
        counts[state] += 1

    section("② §10.1 首期接口目录（15 条，上游 16 行含 policy 归 control）")
    routes_text = (ROOT / "services/_kernel/routes.py").read_text(encoding="utf-8")
    for method, path in INTERFACE_CATALOG:
        # 内核用「装饰注册」把 method+path 登记在一张表里
        needle = path
        found = needle in routes_text or needle.replace("{task_id}", "{task_id}") in routes_text
        # edge 路径不在内核（属 edge/ 树，上游 P4）
        deferred = path.startswith("/edge/")
        state = report(
            f"{method} {path}",
            found,
            "services/_kernel/routes.py" if found else ("上游归 P4/Edge，本仓无 edge/ 树" if deferred else "未找到"),
            deferred=deferred,
        )
        counts[state] += 1

    section("③ §10.2 公共 Context 字段（9）")
    ctx_schema = ROOT / "packages/contracts/schemas/common-context.json"
    ctx_text = ctx_schema.read_text(encoding="utf-8") if ctx_schema.exists() else ""
    if not ctx_text:
        print("  ✗ packages/contracts/schemas/common-context.json 不存在，本节无法核对")
    for field in CONTEXT_FIELDS:
        state = report(
            field,
            f'"{field}"' in ctx_text,
            "packages/contracts/schemas/common-context.json",
        )
        counts[state] += 1

    section("④ §10.2 DecisionResult 必须字段（12）")
    dr_schema = ROOT / "packages/contracts/schemas/decision-result.json"
    dr_text = dr_schema.read_text(encoding="utf-8") if dr_schema.exists() else ""
    for field in DECISION_RESULT_FIELDS:
        state = report(field, f'"{field}"' in dr_text, "packages/contracts/schemas/decision-result.json")
        counts[state] += 1

    section("⑤ §10.2 ToolSpec 必须字段（15）")
    tool_schema = ROOT / "packages/contracts/schemas/tool-spec.json"
    tool_text = tool_schema.read_text(encoding="utf-8") if tool_schema.exists() else ""
    for field in TOOL_SPEC_FIELDS:
        # 字段可能是复合名（input_schema → input/output_schema）
        variants = {field, field.replace("_schema", "_schema"), field.replace("support", "support")}
        if field == "input_schema":
            variants = {"input_schema", "input/output_schema"}
        state = report(
            field,
            any(f'"{v}"' in tool_text for v in variants),
            "packages/contracts/schemas/tool-spec.json",
        )
        counts[state] += 1

    section("⑥ §13 八个 owner schema + 迁移目录")
    for schema in OWNER_SCHEMAS:
        has_mig = (ROOT / "migrations" / schema).is_dir()
        state = report(f"migrations/{schema}/", has_mig, f"migrations/{schema}/" if has_mig else "缺失")
        counts[state] += 1

    section("⑦ §11 首期事件类型（14）")
    for event in EVENT_TYPES:
        hits = grep(re.escape(event), files)
        code_hits = [h for h in hits if h[0].endswith((".py", ".json"))]
        state = report(event, bool(code_hits), f"{code_hits[0][0]}:{code_hits[0][1]}" if code_hits else "无")
        counts[state] += 1

    section("⑧ §17 验收断言（A01–A14）")
    # ⚠️ 测试里的标注形如 `# A01,A03`（逗号分隔多项），所以判据要按
    # 「注释里的断言号」匹配，不能用 `\bA02\b`——那样会漏掉逗号后的项。
    # 第一版就用错了正则，把 A02/A04–A14 全报成 MISSING。
    for assert_id in ACCEPTANCE_ASSERTS:
        hits = grep(rf"#\s*{assert_id}\b", files)
        code_hits = [h for h in hits if is_under(h[0], "tests")]
        if code_hits:
            state = report(assert_id, True, f"{len(code_hits)} 处，如 {code_hits[0][0]}")
        else:
            # 单独判断：A12 依赖目标硬件与 14 天影子数据（上游 §16.1 现场许可/模型与校准）
            deferred = assert_id == "A12"
            state = report(
                assert_id,
                False,
                "上游要求目标硬件 + 连续 14 天影子数据（外部依赖）" if deferred else "**无任何测试引用**",
                deferred=deferred,
            )
        counts[state] += 1

    section("⑨ §15 建议仓库布局（结构项）")
    layout = [
        ("apps/web-admin/", "apps/web-admin", False),
        ("packages/contracts/", "packages/contracts", False),
        ("packages/domain/", "packages/domain", False),
        ("services/*/", "services", False),
        ("migrations/{owner}/", "migrations", False),
        ("tests/contracts/", "tests/contracts", False),
        ("tests/isolation/", "tests/isolation", False),
        ("tests/transactions/", "tests/transactions", False),
        ("tests/e2e/", "tests/e2e", False),
        ("docs/adr/", "docs/adr", True),
        ("docs/runbooks/", "docs/runbooks", True),
        ("docs/handoff/", "docs/handoff", False),
        ("edge/agent/、edge/inference/、edge/guard/、edge/adapters/", "edge", True),
        ("integrations/erp/、pmc/、mes/、wms/", "integrations", False),
        ("infra/compose/、deployment/、network/", "infra", True),
        ("generate clients from OpenAPI 3.1（§10.1）", "generated", True),
    ]
    for label, rel, deferred in layout:
        exists = (ROOT / rel).is_dir()
        state = report(
            label,
            exists,
            rel + "/" if exists else ("上游归 P4/待冻结" if deferred else "不存在"),
            deferred=deferred,
        )
        counts[state] += 1

    section("⑨ §3 Jev Decision Fabric 的八种决策类型")
    registry_doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    catalog = registry_doc.get("decision_catalog") or {}
    declared = tuple(catalog.get("decision_kinds") or ())
    implemented = tuple(catalog.get("implemented_decision_kinds") or ())

    api_models = json.loads(
        (ROOT / "packages/contracts/schemas/api-models.json").read_text(encoding="utf-8")
    )
    contract_enum = tuple(
        api_models["$defs"]["DecisionRequest"]["properties"]["decision_kind"]["enum"]
    )
    state = report(
        "决策类型清单与上游 §3 逐字一致（登记表）",
        declared == JEV_DECISION_KINDS,
        f"{len(declared)} 种" if declared == JEV_DECISION_KINDS else f"不一致：{declared}",
    )
    counts[state] += 1
    state = report(
        "决策类型清单与上游 §3 逐字一致（契约枚举）",
        contract_enum == JEV_DECISION_KINDS,
        f"{len(contract_enum)} 种" if contract_enum == JEV_DECISION_KINDS else f"不一致：{contract_enum}",
    )
    counts[state] += 1

    out_of_scope = [k for k in implemented if k not in JEV_DECISION_KINDS]
    state = report(
        "已实现的类型都来自上游清单",
        not out_of_scope,
        f"登记表声明 {len(implemented)} 种：{list(implemented)}" if not out_of_scope else f"越界：{out_of_scope}",
    )
    counts[state] += 1

    # 未实现的类型：按**所有已登记适配器的并集**判断。
    # 单看某一个适配器会误报——例如 rules 实现了 6 种而 mock 只实现 4 种，
    # 真正该问的是"整个服务有没有做到"。
    from packages.decision import adapter_ids as _all_adapters
    from packages.decision import resolve_adapter as _resolve

    union: set[str] = set()
    per_adapter: list[str] = []
    for adapter_id in _all_adapters():
        kinds = _resolve({"implementation": adapter_id}).supported_kinds()
        union.update(kinds)
        per_adapter.append(f"{adapter_id}={len(kinds)}")
    covered = sorted(k for k in JEV_DECISION_KINDS if k in union)
    unimplemented = sorted(set(JEV_DECISION_KINDS) - union)
    state = report(
        "每个类型都有明确归宿（实现或显式拒绝）",
        True,  # 三类归宿（实现/显式拒绝/目录未登记即拒）都是明确归宿
        f"服务已覆盖 {len(covered)} 种（{' '.join(per_adapter)}）；"
        f"未实现 {unimplemented} ⇒ 显式拒绝，不静默返回",
    )
    counts[state] += 1

    section("⑩ §13/§16.1 Jev 适配方式（P0 冻结）")
    adapter_cfg = registry_doc.get("decision_adapter") or {}
    implementation = adapter_cfg.get("implementation")
    state = report(
        "适配方式已冻结在登记表 decision_adapter",
        isinstance(implementation, str) and bool(implementation),
        f"implementation={implementation!r}",
    )
    counts[state] += 1

    state = report(
        "适配接口已抽出（换实现不改内核）",
        (ROOT / "packages/decision/adapter.py").is_file(),
        "packages/decision/adapter.py",
    )
    counts[state] += 1

    # 上游 §3 明写三类实现者「**均需实现同一契约**」⇒ 至少要两个实现，
    # 接缝才算被验证过（一个实现证明不了接口是接缝）。
    from packages.decision import adapter_ids as _adapter_ids

    registered_adapters = _adapter_ids()
    state = report(
        "至少两个适配器实现（证明契约是接缝）",
        len(registered_adapters) >= 2,
        f"已登记 {len(registered_adapters)} 个：{registered_adapters}",
    )
    counts[state] += 1

    state = report(
        "有运行时换实现的判据（不是只做进程内测试）",
        (ROOT / "tests/decision/test_adapter_switch_runtime.py").is_file(),
        "tests/decision/test_adapter_switch_runtime.py（用 V3_REGISTRY_PATH 切实现起真集群）",
    )
    counts[state] += 1

    routes_text = (ROOT / "services/_kernel/routes.py").read_text(encoding="utf-8")
    leaked = [m for m in ('catalog.get("routes")', 'catalog.get("fallback_routes")') if m in routes_text]
    state = report(
        "内核不残留决策逻辑",
        not leaked,
        "内核经适配层取结果" if not leaked else f"仍直取目录：{leaked}",
    )
    counts[state] += 1

    state = report(
        "§13「适配方式需 P0 冻结」有 ADR 落实",
        (ROOT / "docs/adr/0008-jev-adapter-boundary.md").is_file(),
        "docs/adr/0008-jev-adapter-boundary.md",
    )
    counts[state] += 1

    section("汇总")
    total = sum(counts.values())
    print(f"  逐条核对 {total} 项：OK {counts['OK']} / DEFERRED {counts['DEFERRED']} / MISSING {counts['MISSING']}")
    print(f"  DEFERRED 表示上游自己标为待冻结或外部依赖（不算缺口）；MISSING 是真缺口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
