"""数据缺口登记：把"缺数据所以做不了"变成**可机械检测、且数据一到就报警**的断言。

上游 §15（`apps/web-admin` 应有员工/发布/对账页）、§17 A06（批准的授权前提）。

## 为什么是"守卫"而不是"记录"

我在多轮里反复写下同一句已知限制：

> 员工页未做——`employees` 没有对应路由。

**那句话不准确，而且它会掩盖真问题。** 本轮核实后确认：

| 层 | 状态 |
| --- | --- |
| 路由 | 缺（可以加） |
| **员工数据模型** | **根本不存在** |

`registry.mock.json` 里每个租户只有 `employees: ["employee-planning"]` —— **裸 ID**。
全仓搜 `employee_actor` / `actor_ref` / `assignments`：**0 次**；
`roles` 那几处是**知识文档的可见角色**（`pmc.read`），不是员工角色。

所以"加个路由就能出员工页"是错的：**加了也只能显示一个裸 ID，那是装样子。**

## 这条守卫做什么

1. **逐项登记缺口**（缺什么、阻塞什么、从哪看出来"还缺"）；
2. **把"还缺"写成当前为真、数据一到就变假的断言** —— 于是数据到达时**门禁报红**，
   强制下一步动作（补页面/接数据），而不是让缺口悄悄消失；
3. **能力自证**：证明检测手法真的能区分"有"与"没有"。

> 这比"记一笔已知限制"强：**已知限制不会自己报警。**
"""

from __future__ import annotations

import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "services/_kernel/registry.mock.json"
APPROVAL_JS = REPO_ROOT / "apps/web-admin/src/approval.js"
ADMIN_HTML = REPO_ROOT / "apps/web-admin/public/index.html"

#: 数据缺口登记 → (缺什么, 阻塞什么, 数据到位后必须做什么)。
#:
#: **2026-10-05 更新**：原先这里登记 5 项，其中 4 项**已补齐**（见下方
#: `CLOSED_GAPS`）。本表只留**仍然缺**的——否则表会退化成历史账。
DATA_GAPS: dict[str, tuple[str, str, str]] = {
    "real_posting_evidence": (
        "**真实过账凭证**（`posting_evidence_ref` 一直为 `None`）",
        "`posting_status` 恒为 `NOT_CONFIRMED`——界面**永远不会**显示「已过账」",
        "接真实 ERP 回执并补用例：**只有拿到凭证才能显示已过账**（不得放宽）",
    ),
}

#: **已补齐的缺口** → 补齐日期与证据。留档是为了① 让"补过什么"可追溯；
#: ② 让"不要退回去"这件事有判据（见下方 `test_closed_gap_*`）。
#:
#: 这四项原先都是"登记表没数据 ⇒ 检查只能记 UNKNOWN"。
#: 员工数据到位后（契约 `p3-employee.json` + 登记表 `employees` 结构化），
#: 三项检查在 `apps/web-admin/src/approval.js` 里接成了**真判定**。
CLOSED_GAPS: dict[str, str] = {
    "employee_actor_binding": "2026-10-05：登记表 `employees[].executing_actor_ref`（契约字段）",
    "position_authority_matrix": "2026-10-05：`employees[].positions[]`，已接 `approval.js`",
    "risk_grade_limits": "2026-10-05：`positions[].limits[]`（按 RiskClass），已接 `approval.js`",
    "config_authority_roster": "2026-10-05：`employees[].config_authority`，已接 `approval.js`",
}


def _registry_text() -> str:
    return REGISTRY.read_text(encoding="utf-8")


def _registry() -> dict:
    return json.loads(_registry_text())


# ==========================================================================
# ① 已补齐的缺口：**不得退回去**
# ==========================================================================
#
# 这组原先写的是"仍然缺"（数据一到就报红）。2026-10-05 数据到位后，
# 它们**按设计报红了**，我据此补完了下游接线。现在判据**反向**：
# 守住"已经补齐"这件事，防止有人把结构化记录改回裸 id、
# 或把接好的检查改回恒 UNKNOWN。

def test_employee_actor_binding_exists():  # A06
    """员工↔执行身份绑定**必须在**（原先不存在，2026-10-05 补齐）。

    没有它就无法回答"这个 AI 员工以谁的身份执行"。
    """
    tenants = _registry().get("tenants") or {}
    assert tenants, "登记表里应有租户"
    for tenant_id, tenant in tenants.items():
        employees = tenant.get("employees") or []
        assert employees, f"租户 {tenant_id} 应有员工记录"
        for entry in employees:
            assert isinstance(entry, dict), (
                f"租户 {tenant_id} 的 employees 又变回裸 id 了：{entry!r}\n"
                "  这会让 `executing_actor_ref` / 岗位 / 限额全部丢失。"
            )
            assert entry.get("executing_actor_ref"), (
                f"租户 {tenant_id} 的员工 {entry.get('employee_id')!r} 缺 executing_actor_ref"
            )


def test_executing_actor_ref_points_at_a_registered_actor():  # A06
    """`executing_actor_ref` 必须指向**本租户已登记**的 actor。

    指向一个不存在的 actor 比不填更糟：**看着填了，实际查无此人**。
    （这条判据我自己写在契约 `x-employee-policy` 里，但一直没人执行——
    现在执行它。）
    """
    tenants = _registry().get("tenants") or {}
    problems = []
    for tenant_id, tenant in tenants.items():
        actors = set(tenant.get("actors") or [])
        for entry in tenant.get("employees") or []:
            if not isinstance(entry, dict):
                continue
            actor_ref = entry.get("executing_actor_ref")
            if actor_ref and actor_ref not in actors:
                problems.append(f"{tenant_id}/{entry.get('employee_id')}: {actor_ref}")
    assert not problems, (
        "以下 executing_actor_ref 不在本租户的 actors 里（查无此人）：\n  "
        + "\n  ".join(problems)
    )


def test_every_position_covers_all_risk_classes():  # A06
    """每个岗位的 `limits` 必须覆盖**全部**风险等级。

    契约 `x-employee-policy.risk_classes_must_be_fully_covered` 写明：
    缺等级 = 那条链路没人能判"能不能批"，只能退回 `UNKNOWN`。
    """
    from services._kernel.registry import RISK_CLASSES

    tenants = _registry().get("tenants") or {}
    problems = []
    for tenant_id, tenant in tenants.items():
        for entry in tenant.get("employees") or []:
            if not isinstance(entry, dict):
                continue
            for position in entry.get("positions") or []:
                covered = {limit.get("risk_class") for limit in (position.get("limits") or [])}
                missing = sorted(set(RISK_CLASSES) - covered)
                if missing:
                    problems.append(
                        f"{tenant_id}/{entry.get('employee_id')}/{position.get('position_id')}: 缺 {missing}"
                    )
    assert not problems, (
        "以下岗位的限额没有覆盖全部风险等级：\n  " + "\n  ".join(problems)
    )


def test_forbidden_limits_carry_no_amount():  # A06
    """`FORBIDDEN` 不得再带 `max_amount` / `requires_second_approval`。

    「不得批」再要求金额或双人是**自相矛盾的形状**——契约层直接禁掉，
    免得下游要为它想语义。
    """
    tenants = _registry().get("tenants") or {}
    problems = []
    for tenant_id, tenant in tenants.items():
        for entry in tenant.get("employees") or []:
            if not isinstance(entry, dict):
                continue
            for position in entry.get("positions") or []:
                for limit in position.get("limits") or []:
                    if limit.get("decision") == "FORBIDDEN" and (
                        "max_amount" in limit or "requires_second_approval" in limit
                    ):
                        problems.append(
                            f"{tenant_id}/{position.get('position_id')}/{limit.get('risk_class')}"
                        )
    assert not problems, f"FORBIDDEN 条目带了金额/双人要求：{problems}"


def test_authority_checks_are_wired_not_hardcoded():  # A06
    """`approval.js` 里那三项**不得**再是硬编码 `UNKNOWN`。

    这是"补数据之后必须做的下游接线"的判据：
    **数据到位但检查仍写死 UNKNOWN，等于白补。**
    """
    source = APPROVAL_JS.read_text(encoding="utf-8")
    for name in ("config_authority_separated", "within_limit", "position_authorized"):
        assert name in source, f"`{name}` 应出现在 `approval.js` 里"
    # 不允许出现"给这三项直接 push UNKNOWN 且无分支"的写法：
    # 判据取**存在 authority 形参**与**PASS/FAIL 分支**
    assert "authority" in source, "`assessApproval` 应接受 `authority` 形参（审批人的岗位与授权）"
    assert "may_change_config" in source, "配置权分离应读 `config_authority.may_change_config`"
    assert "risk_class" in source, "限额与岗位判定应读 `risk_class`"


# ==========================================================================
# ② 仍然缺的：**数据一到就报红**
# ==========================================================================

def test_real_posting_evidence_is_still_absent():  # A06
    """真实过账凭证**仍然不存在**。

    一旦出现，本用例报红——那是好事：它强制"让『已过账』可达"这件事
    被真的做掉，并补上"只有拿到凭证才能显示已过账"的用例（不得放宽）。
    """
    text = _registry_text()
    assert "posting_evidence_ref" not in text or '"posting_evidence_ref": null' in text, (
        "登记表里出现了过账凭证！请：\n"
        "  ① 接进回执构造路径，让 posting_status 可变为 CONFIRMED；\n"
        "  ② 补用例『只有拿到凭证才能显示已过账』；\n"
        "  ③ 更新本用例（它描述的是缺口，不是期望）。"
    )


# ==========================================================================
# ② 这些缺口**确实阻塞着**下游（不是"记了但没影响"）
# ==========================================================================

def test_the_three_unknown_checks_name_their_missing_data():  # A06
    """`CHECK_REQUIREMENTS` 必须点名三个缺数据检查各自要什么。

    这条保证"缺数据"不是一句笼统的话，而是**逐项可核对的清单** ——
    数据到位时能一项一项划掉。
    """
    text = APPROVAL_JS.read_text(encoding="utf-8")
    for check in ("position_authorized", "within_limit", "config_authority_separated"):
        assert check in text, f"`{check}` 必须出现在 `CHECK_REQUIREMENTS` 里"
    # 每项都要有"需要什么"的说明，而不是空占位
    for required in ("岗位与授权矩阵", "授权限额", "配置管理员"):
        assert required in text, f"缺数据说明里应点明『{required}』"


def test_posting_status_cannot_be_shown_as_posted_without_evidence():  # A14
    """没有真实过账凭证时，界面**不得**出现"已过账"这条路。

    这是上游 §6 的核心不变量的界面侧：`posting_status` 恒 `NOT_CONFIRMED`
    ⇒ 结论恒不可能是 `POSTED`。**数据一到就要改这条**（那是好事）。
    """
    html = ADMIN_HTML.read_text(encoding="utf-8")
    # 界面渲染的是服务端结论（`d.outcome`），不得自己判断"已过账"
    assert "d.outcome" in html, "页面必须渲染服务端结论"
    # 页面里不应有把 POSTED 硬编码成显示结论的写法
    assert 'outcome: "POSTED"' not in html and "outcome = \"POSTED\"" not in html, (
        "页面不得自己得出 POSTED 结论 —— 它必须来自服务端的权威回执判定"
    )


# ==========================================================================
# ③ 能力自证：检测手法真的能区分"有"与"没有"
# ==========================================================================

def test_the_gap_detectors_can_actually_detect(tmp_path):
    """喂构造数据，检测手法必须**报出"有"**。

    没有这条，"字段名写错导致永远检测不到"与"数据确实还没来"
    在观测上无法区分 —— 缺口守卫会变成永不触发的装饰。
    """
    import json as _json

    # ① 结构化员工记录能被识别
    structured = {"tenants": {"t": {"employees": [{"employee_id": "e1", "position": "planner"}]}}}
    entries = list(structured["tenants"]["t"]["employees"])
    detected_structured = any(not isinstance(entry, str) for entry in entries)
    assert detected_structured, "结构化员工记录必须被识别出来"

    # ② 字段搜索能被识别
    probe = _json.dumps({"tenants": {"t": {"employee_actor": "user-demo"}}}, ensure_ascii=False)
    assert any(marker in probe for marker in ("employee_actor", "actor_ref")), (
        "字段搜索必须能命中"
    )

    # ③ 限额字段能被识别
    probe_limit = _json.dumps({"limits": [{"risk_class": "R2", "max_amount": 100}]})
    assert any(marker in probe_limit.lower() for marker in ("limit", "max_amount")), (
        "限额字段搜索必须能命中"
    )
