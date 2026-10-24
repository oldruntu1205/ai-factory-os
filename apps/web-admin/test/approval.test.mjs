/**
 * 批准页判定的门禁（上游 §7 风险授权 / §10.1 / §17 A06）。
 *
 * 重点守三件事：
 * 1. **自然语言不得冒充授权**（上游明文）；
 * 2. **缺数据的检查必须记为 UNKNOWN，不得当成通过**——
 *    把"没法核实"显示成"可以批准"，比不显示更危险；
 * 3. 已决定/已过期的批准**不得**再显示为可决定（契约：事实追加不可变）。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CHECK_RESULTS,
  DECIDABILITY,
  assessApproval,
  decidabilityLabels,
  looksLikeNaturalLanguageApproval,
  normalizeDecision,
  summarizeApprovals,
} from "../src/approval.js";

const NOW = "2026-10-05T12:00:00Z";

/** 一条"该有的都有"的待决定批准（不含角色/限额——那部分本来就缺）。 */
function pendingApproval(overrides = {}) {
  return {
    approval_id: "approval-1",
    approved: null,
    approver_ref: "user-approver",
    requester_ref: "user-demo",
    valid_until: "2026-10-06T00:00:00Z",
    binding: { object_id: "schedule-demo", tool_id: "pmc.dispatch.publish" },
    ...overrides,
  };
}

// ==========================================================================
// 一、自然语言不得冒充授权
// ==========================================================================

test("中文「批准/同意/通过」不构成授权", () => {
  for (const text of ["批准", "同意", "同意批准", "通过", "可以执行", "没问题"]) {
    const result = looksLikeNaturalLanguageApproval(text);
    assert.equal(result.naturalLanguage, true, `${text} 应被识别为自然语言批准`);
  }
});

test("英文 approval 类措辞同样不构成授权", () => {
  for (const text of ["ok", "OK", "approved", "approve", "yes", "go ahead"]) {
    assert.equal(
      looksLikeNaturalLanguageApproval(text).naturalLanguage,
      true,
      `${text} 应被识别为自然语言批准`,
    );
  }
});

test("普通说明文字不被误判", () => {
  for (const text of ["已核对排程版本", "见工单 12345", "等待物料到货后再议"]) {
    assert.equal(
      looksLikeNaturalLanguageApproval(text).naturalLanguage,
      false,
      `${text} 不该被误判为授权`,
    );
  }
});

test("**说明里写了「批准」即拒绝**，即使 approved 字段是布尔", () => {
  // 这正是要防的界面误导：填了意见就等于批准。
  const result = normalizeDecision({ approved: true, comment: "同意，批准执行" });
  assert.equal(result.ok, false);
  assert.equal(result.code, "NATURAL_LANGUAGE_APPROVAL_REJECTED");
  assert.match(result.reason, /不构成授权/);
});

test("approved 不是布尔即拒绝（字符串 'true'、数字、'yes' 都不算）", () => {
  for (const value of ["true", 1, "yes", null, undefined, {}]) {
    const result = normalizeDecision({ approved: value });
    assert.equal(result.ok, false, `approved=${JSON.stringify(value)} 不该被接受`);
    assert.equal(result.code, "DECISION_NOT_STRUCTURED");
  }
});

test("结构化的布尔决定被接受，说明文字原样保留", () => {
  const result = normalizeDecision({ approved: true, comment: "已核对排程版本 7" });
  assert.equal(result.ok, true);
  assert.equal(result.approved, true);
  assert.equal(result.comment, "已核对排程版本 7");
});

test("否决同样是结构化决定", () => {
  const result = normalizeDecision({ approved: false });
  assert.equal(result.ok, true);
  assert.equal(result.approved, false);
  assert.equal(result.comment, null);
});

// ==========================================================================
// 二、缺数据的检查必须是 UNKNOWN，绝不能当成通过
// ==========================================================================

test("无角色/限额数据时，三项检查记为 UNKNOWN", () => {
  const { checks } = assessApproval({ approval: pendingApproval(), decidedAt: NOW });
  const byName = Object.fromEntries(checks.map((c) => [c.name, c]));
  for (const name of ["config_authority_separated", "within_limit", "position_authorized"]) {
    assert.equal(byName[name].result, "UNKNOWN", `${name} 应记为 UNKNOWN（缺数据）`);
  }
});

test("**有 UNKNOWN 检查时结论是 UNKNOWN，不是 DECIDABLE**", () => {
  const result = assessApproval({ approval: pendingApproval(), decidedAt: NOW });
  assert.notEqual(result.decidability, "DECIDABLE");
  assert.equal(result.decidability, "UNKNOWN");
  assert.match(result.label, /待核实/);
});

test("UNKNOWN 结论必须点名缺什么数据", () => {
  const result = assessApproval({ approval: pendingApproval(), decidedAt: NOW });
  assert.ok(result.requiresData.length >= 3, "应列出所需数据");
  const joined = result.requiresData.join(" ");
  assert.match(joined, /角色|岗位/);
  assert.match(joined, /限额/);
});

test("缺数据的检查在标签里明说「不得当作可批准」", () => {
  const labels = decidabilityLabels({ total: 1, byDecidability: { UNKNOWN: 1 } });
  const unknown = labels.find((l) => l.key === "UNKNOWN");
  assert.match(unknown.note, /不得当作可批准|缺/);
});

test("**「待核实」单独计数，不并进「可决定」**", () => {
  const approvals = [pendingApproval(), pendingApproval({ approval_id: "a2" })];
  const summary = summarizeApprovals(approvals, NOW);
  assert.equal(summary.byDecidability.DECIDABLE ?? 0, 0);
  assert.equal(summary.byDecidability.UNKNOWN, 2);
});

// ==========================================================================
// 三、明确阻塞优先于未知
// ==========================================================================

test("审批人等于发起人 ⇒ BLOCKED（有数据支撑的职责分离）", () => {
  const result = assessApproval({
    approval: pendingApproval({ approver_ref: "user-demo", requester_ref: "user-demo" }),
    decidedAt: NOW,
  });
  assert.equal(result.decidability, "BLOCKED");
  assert.ok(result.blockReasons.some((r) => r.includes("approver_is_not_requester")));
});

test("**已成立的批准不得再显示为可决定**（事实追加不可变）", () => {
  const granted = assessApproval({ approval: pendingApproval({ approved: true }), decidedAt: NOW });
  assert.equal(granted.decidability, "BLOCKED");
  assert.ok(granted.blockReasons.some((r) => r.includes("不能再改判")));

  const denied = assessApproval({ approval: pendingApproval({ approved: false }), decidedAt: NOW });
  assert.equal(denied.decidability, "BLOCKED");
  assert.ok(denied.blockReasons.some((r) => r.includes("已被否决")));
});

test("已过期的批准 ⇒ BLOCKED", () => {
  const result = assessApproval({
    approval: pendingApproval({ valid_until: "2026-10-04T00:00:00Z" }),
    decidedAt: NOW,
  });
  assert.equal(result.decidability, "BLOCKED");
  assert.ok(result.blockReasons.some((r) => r.includes("失效")));
});

test("缺 binding ⇒ BLOCKED（不知道批的是什么）", () => {
  const result = assessApproval({ approval: pendingApproval({ binding: null }), decidedAt: NOW });
  assert.equal(result.decidability, "BLOCKED");
  assert.ok(result.blockReasons.some((r) => r.includes("binding")));
});

test("**明确阻塞压倒未知**：既有 FAIL 又有 UNKNOWN 时报 BLOCKED", () => {
  const result = assessApproval({
    approval: pendingApproval({ approver_ref: "user-demo", requester_ref: "user-demo" }),
    decidedAt: NOW,
  });
  assert.equal(result.decidability, "BLOCKED");
  assert.ok(result.unknownChecks.length > 0, "本例同时存在 UNKNOWN 检查");
});

// ==========================================================================
// 四、不给"现在"就不判过期（结论必须可复现）
// ==========================================================================

test("未提供当前时刻时，有效期检查记为 UNKNOWN 而不是用本机时钟", () => {
  const result = assessApproval({ approval: pendingApproval() });
  const validity = result.checks.find((c) => c.name === "validity");
  assert.equal(validity.result, "UNKNOWN");
  assert.match(validity.reason, /不可复现|未提供当前时刻/);
});

test("同一输入重复评估得到完全相同结论（可复现）", () => {
  const approval = pendingApproval();
  const a = JSON.stringify(assessApproval({ approval, decidedAt: NOW }));
  const b = JSON.stringify(assessApproval({ approval, decidedAt: NOW }));
  assert.equal(a, b);
});

// ==========================================================================
// 五、枚举与穷举
// ==========================================================================

test("所有检查结论都在登记枚举内", () => {
  const cases = [
    pendingApproval(),
    pendingApproval({ approved: true }),
    pendingApproval({ binding: null }),
    pendingApproval({ valid_until: "2026-10-04T00:00:00Z" }),
    pendingApproval({ approver_ref: null }),
    {},
  ];
  for (const approval of cases) {
    for (const check of assessApproval({ approval, decidedAt: NOW }).checks) {
      assert.ok(CHECK_RESULTS.includes(check.result), `未登记的检查结论 ${check.result}`);
    }
    assert.ok(DECIDABILITY.includes(assessApproval({ approval, decidedAt: NOW }).decidability));
  }
});

test("**没有任何输入能得出 DECIDABLE**——因为角色/限额数据尚未就位", () => {
  // 这是"诚实性"的断言：在当前数据条件下，界面**不该**显示"可决定"。
  // 一旦角色与限额数据接入，这条用例应当被显式改掉（而不是悄悄失效）。
  const cases = [
    pendingApproval(),
    pendingApproval({ approver_ref: "a", requester_ref: "b", valid_until: "2099-01-01T00:00:00Z" }),
    { binding: { object_id: "o" }, approver_ref: "x", requester_ref: "y", approved: null },
  ];
  for (const approval of cases) {
    assert.notEqual(
      assessApproval({ approval, decidedAt: NOW }).decidability,
      "DECIDABLE",
      "在缺角色/限额数据的情况下不得显示为可决定",
    );
  }
});

test("空记录不抛异常，且结论为不可决定或待核实", () => {
  const result = assessApproval({});
  assert.ok(["BLOCKED", "UNKNOWN"].includes(result.decidability));
});

test("summarizeApprovals 与逐条判定同源", () => {
  const approvals = [pendingApproval(), pendingApproval({ approved: true }), {}];
  const summary = summarizeApprovals(approvals, NOW);
  const recomputed = approvals.map((a) => assessApproval({ approval: a, decidedAt: NOW }).decidability);
  const counted = Object.entries(summary.byDecidability).flatMap(([k, n]) => Array(n).fill(k));
  assert.deepEqual(counted.sort(), recomputed.sort());
});

// ==========================================================================
// 岗位 / 限额 / 配置权：**有数据之后**的真判定（2026-10-05 接线）
// ==========================================================================
//
// 这三项原先恒记 `UNKNOWN`（登记表没有岗位/限额数据）。
// 员工数据到位后（契约 `p3-employee.json` + 登记表），它们改成了真判定。
//
// 下面这组守的是**判定的方向性**：
// 把"越权"判成通过，比把它判成"待核实"危险得多。

/** 一个具备某风险等级限额的审批人（形状取自登记表 tenant-demo）。 */
function plannerAuthority({ limits, mayChangeConfig = false } = {}) {
  return {
    employee_id: "employee-planning",
    executing_actor_ref: "user-demo",
    config_authority: { may_change_config: mayChangeConfig, scopes: [] },
    positions: [
      {
        position_id: "position-planner",
        title: "计划员",
        delegated_scopes: ["pmc.read"],
        limits: limits ?? [
          { risk_class: "R0", decision: "AUTO" },
          { risk_class: "R1", decision: "AUTO" },
          { risk_class: "R2", decision: "APPROVE", max_amount: "10000.00" },
          { risk_class: "R3", decision: "FORBIDDEN" },
        ],
      },
    ],
  };
}

const checkOf = (result, name) => result.checks.find((c) => c.name === name);

test("不传 authority 时三项仍记 UNKNOWN（不因缺数据而放行）", () => {
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R3" };
  const result = assessApproval({ approval, decidedAt: NOW });
  for (const name of ["config_authority_separated", "within_limit", "position_authorized"]) {
    assert.equal(checkOf(result, name).result, "UNKNOWN", `${name} 缺数据时必须记 UNKNOWN`);
  }
  assert.notEqual(result.decidability, "DECIDABLE", "缺数据不得判为可决定");
});

test("R3 被岗位明确禁止 ⇒ position_authorized 必须 FAIL（不是 UNKNOWN）", () => {
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R3" };
  const result = assessApproval({ approval, decidedAt: NOW, authority: plannerAuthority() });

  assert.equal(checkOf(result, "position_authorized").result, "FAIL");
  assert.equal(checkOf(result, "within_limit").result, "FAIL");
  assert.equal(result.decidability, "BLOCKED", "有明确禁止时必须 BLOCKED");
});

test("R2 有 APPROVE 限额 ⇒ within_limit 与 position_authorized 都 PASS", () => {
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R2" };
  const result = assessApproval({ approval, decidedAt: NOW, authority: plannerAuthority() });

  assert.equal(checkOf(result, "within_limit").result, "PASS");
  assert.equal(checkOf(result, "position_authorized").result, "PASS");
  // 理由里必须带上结论与上限，否则读者无法核对
  assert.match(checkOf(result, "within_limit").reason, /APPROVE/);
  assert.match(checkOf(result, "within_limit").reason, /10000\.00/);
});

test("岗位未覆盖该风险等级 ⇒ UNKNOWN（数据不足），**不是** PASS", () => {
  // 只定义了 R0/R1，被问 R3
  const authority = plannerAuthority({ limits: [{ risk_class: "R1", decision: "AUTO" }] });
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R3" };
  const result = assessApproval({ approval, decidedAt: NOW, authority });

  assert.equal(checkOf(result, "within_limit").result, "UNKNOWN");
  assert.equal(checkOf(result, "position_authorized").result, "FAIL", "没有任何覆盖 R3 的授权 ⇒ 不得批准");
  assert.notEqual(result.decidability, "DECIDABLE");
});

test("审批人同时具备配置修改权 ⇒ config_authority_separated 必须 FAIL", () => {
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R2" };
  const result = assessApproval({
    approval,
    decidedAt: NOW,
    authority: plannerAuthority({ mayChangeConfig: true }),
  });

  assert.equal(checkOf(result, "config_authority_separated").result, "FAIL");
  assert.equal(result.decidability, "BLOCKED");
});

test("审批人无配置权 ⇒ config_authority_separated PASS", () => {
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R2" };
  const result = assessApproval({ approval, decidedAt: NOW, authority: plannerAuthority() });
  assert.equal(checkOf(result, "config_authority_separated").result, "PASS");
});

test("未声明 may_change_config ⇒ UNKNOWN（不猜）", () => {
  const authority = {
    positions: [{ position_id: "p", limits: [{ risk_class: "R2", decision: "APPROVE" }] }],
    config_authority: {},
  };
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R2" };
  const result = assessApproval({ approval, decidedAt: NOW, authority });
  assert.equal(checkOf(result, "config_authority_separated").result, "UNKNOWN");
});

test("未被授予任何岗位 ⇒ position_authorized FAIL", () => {
  const authority = { positions: [], config_authority: { may_change_config: false } };
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R2" };
  const result = assessApproval({ approval, decidedAt: NOW, authority });
  assert.equal(checkOf(result, "position_authorized").result, "FAIL");
});

test("多岗位取**最宽**结论（与契约「员工权限 = 所任岗位并集」一致）", () => {
  const authority = {
    config_authority: { may_change_config: false },
    positions: [
      { position_id: "p1", limits: [{ risk_class: "R2", decision: "FORBIDDEN" }] },
      { position_id: "p2", limits: [{ risk_class: "R2", decision: "APPROVE", max_amount: "5000.00" }] },
    ],
  };
  const approval = { binding: { object_id: "o" }, approver_ref: "x", risk_class: "R2" };
  const result = assessApproval({ approval, decidedAt: NOW, authority });

  assert.equal(checkOf(result, "within_limit").result, "PASS", "一个岗位禁止、另一个允许 ⇒ 取并集允许");
  assert.match(checkOf(result, "within_limit").reason, /5000\.00/);
});
