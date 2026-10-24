/**
 * 状态显示契约的门禁（上游 §17 A14 + §6「防止 UI 显示成功而 ERP 未过账」）。
 *
 * 零外部依赖：用 Node 内置的 `node:test`，`node --test` 直接跑。
 * 这样判定逻辑的门禁**不需要 npm 安装任何东西**——不会因为 registry
 * 不通而让门禁失效（"测不了所以先合进去"是最坏的妥协）。
 *
 * 这组用例的重点是**反例**：那些"看起来成功、其实没落账"的组合。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  COMMAND_STATES,
  DISPLAY_OUTCOMES,
  POSTING_STATUSES,
  TASK_STATES,
  checkAuthoritySeparation,
  deriveDisplayStatus,
  readAuthorityPosting,
  summarize,
} from "../src/status.js";

/** 构造一份**权威且已过账**的回执。 */
function postedReceipt(overrides = {}) {
  return {
    receipt_id: "receipt-1",
    command_id: "command-1",
    outcome: "SUCCEEDED",
    business_success: true,
    posting_status: "POSTED",
    posting_evidence_ref: "evidence-posting-1",
    authority_verified: true,
    authority_receipt_ref: "authority-1",
    authority_receipt_hash: "sha256:deadbeef",
    confirmed_at: "2026-10-05T00:00:00Z",
    recorded_at: "2026-10-05T00:00:01Z",
    command_status: "SUCCEEDED",
    status_source: "transaction-service",
    ...overrides,
  };
}

// ==========================================================================
// 一、压倒性原则：只有权威回执能说"已过账"
// ==========================================================================

test("权威且已过账的回执 ⇒ 显示已过账", () => {
  const display = deriveDisplayStatus({
    taskStatus: "SUCCEEDED",
    commandStatus: "SUCCEEDED",
    receipt: postedReceipt(),
  });
  assert.equal(display.outcome, "POSTED");
  assert.equal(display.authoritative, true);
  assert.equal(display.evidenceRef, "evidence-posting-1");
});

test("**编排已完成但没有回执** ⇒ 不得显示已过账", () => {
  // 这正是上游要防的那件事：task 说成功，账务其实没动。
  const display = deriveDisplayStatus({
    taskStatus: "SUCCEEDED",
    commandStatus: null,
    receipt: null,
  });
  assert.notEqual(display.outcome, "POSTED");
  assert.equal(display.outcome, "NOT_POSTED");
  assert.equal(display.authoritative, false);
  assert.match(display.reason, /未取得|未确认|未过账/);
});

test("命令说成功但回执**未过账** ⇒ 不得显示已过账", () => {
  const display = deriveDisplayStatus({
    taskStatus: "SUCCEEDED",
    commandStatus: "SUCCEEDED",
    receipt: postedReceipt({ posting_status: "NOT_CONFIRMED" }),
  });
  assert.notEqual(display.outcome, "POSTED");
});

test("回执**未经权威验证**时，即便自称已过账也不算", () => {
  // 执行方自报不算账务事实——上游要求"任务成功只由权威执行回执驱动"。
  const display = deriveDisplayStatus({
    taskStatus: "SUCCEEDED",
    commandStatus: "SUCCEEDED",
    receipt: postedReceipt({ authority_verified: false }),
  });
  assert.notEqual(display.outcome, "POSTED");
  assert.match(display.reason, /未.*验证|自报/);
});

test("权威回执优先于命令状态：命令还在跑但回执已确认过账", () => {
  // 回执是账务事实，命令状态是执行侧投影——两者冲突时以回执为准。
  const display = deriveDisplayStatus({
    taskStatus: "RUNNING",
    commandStatus: "EXECUTING",
    receipt: postedReceipt(),
  });
  assert.equal(display.outcome, "POSTED");
});

test("不涉及账务的命令显示为「不涉及过账」，而不是「未过账」", () => {
  // 两者含义完全不同：一个是"本该过账但没成"，一个是"压根没有账务动作"。
  const display = deriveDisplayStatus({
    taskStatus: "SUCCEEDED",
    commandStatus: "SUCCEEDED",
    receipt: postedReceipt({ posting_status: "NOT_APPLICABLE" }),
  });
  assert.equal(display.outcome, "NOT_APPLICABLE");
  assert.notEqual(display.outcome, "NOT_POSTED");
});

test("readAuthorityPosting 对空回执返回未确认而非抛出", () => {
  for (const empty of [null, undefined, {}]) {
    const result = readAuthorityPosting(empty);
    assert.equal(result.posted, false);
    assert.ok(result.reason.length > 0);
  }
});

// ==========================================================================
// 二、UNKNOWN 必须单独显示（不能含糊成"处理中"）
// ==========================================================================

test("命令进入 UNKNOWN ⇒ 显示需对账，而不是处理中", () => {
  const display = deriveDisplayStatus({
    taskStatus: "RUNNING",
    commandStatus: "UNKNOWN",
    receipt: null,
  });
  assert.equal(display.outcome, "UNKNOWN");
  assert.match(display.label, /对账|未知/);
  assert.match(display.reason, /查状态|换键|对账/);
});

test("UNKNOWN 即便任务已被标为 SUCCEEDED 也仍然显示需对账", () => {
  // 编排层可能先行收敛，但命令结果未知是**事实层面**的问题，不能盖住。
  const display = deriveDisplayStatus({
    taskStatus: "SUCCEEDED",
    commandStatus: "UNKNOWN",
    receipt: null,
  });
  assert.equal(display.outcome, "UNKNOWN");
});

// ==========================================================================
// 三、状态来源与更新时间必须出现在结论里
// ==========================================================================

test("每条结论都带来源与更新时间", () => {
  const display = deriveDisplayStatus({
    taskStatus: "RUNNING",
    taskStatusSource: "control-plane",
    taskUpdatedAt: "2026-10-05T01:00:00Z",
    commandStatus: "EXECUTING",
    commandStatusSource: "transaction-service",
    commandUpdatedAt: "2026-10-05T01:00:05Z",
    receipt: postedReceipt(),
  });
  assert.equal(display.sources.length, 3);
  const names = display.sources.map((s) => s.name).sort();
  assert.deepEqual(names, ["command", "receipt", "task"]);
  const task = display.sources.find((s) => s.name === "task");
  assert.equal(task.source, "control-plane");
  assert.equal(task.updatedAt, "2026-10-05T01:00:00Z");
  const command = display.sources.find((s) => s.name === "command");
  assert.equal(command.updatedAt, "2026-10-05T01:00:05Z");
});

test("缺少来源信息时不伪造，如实留空", () => {
  const display = deriveDisplayStatus({ taskStatus: "ACCEPTED" });
  const task = display.sources.find((s) => s.name === "task");
  assert.equal(task.source, null);
  assert.equal(task.updatedAt, null);
});

// ==========================================================================
// 四、其余状态组合
// ==========================================================================

test("等待批准 ⇒ 未过账且理由是等待批准", () => {
  const display = deriveDisplayStatus({ taskStatus: "WAITING_APPROVAL", commandStatus: null });
  assert.equal(display.outcome, "NOT_POSTED");
  assert.equal(display.reason, "等待批准");
});

test("失败 ⇒ 显示已失败（不是未过账）", () => {
  assert.equal(deriveDisplayStatus({ taskStatus: "FAILED" }).outcome, "FAILED");
  assert.equal(deriveDisplayStatus({ commandStatus: "FAILED" }).outcome, "FAILED");
});

test("取消 ⇒ 显示已取消（既不是失败也不是成功）", () => {
  const display = deriveDisplayStatus({ taskStatus: "CANCELLED" });
  assert.equal(display.label, "已取消");
  assert.notEqual(display.outcome, "FAILED");
  assert.notEqual(display.outcome, "POSTED");
});

test("需人工 ⇒ 单独一类", () => {
  assert.equal(deriveDisplayStatus({ commandStatus: "MANUAL_REVIEW" }).outcome, "NEEDS_HUMAN");
  assert.equal(deriveDisplayStatus({ taskStatus: "WAITING_HUMAN" }).outcome, "NEEDS_HUMAN");
});

test("执行中 ⇒ 未过账且理由是执行中", () => {
  const display = deriveDisplayStatus({ taskStatus: "RUNNING", commandStatus: "EXECUTING" });
  assert.equal(display.outcome, "NOT_POSTED");
  assert.equal(display.reason, "执行中");
});

test("完全无输入 ⇒ 未过账（默认不成功）", () => {
  const display = deriveDisplayStatus({});
  assert.equal(display.outcome, "NOT_POSTED");
  assert.equal(display.authoritative, false);
});

test("未识别的状态组合 ⇒ 无法判定，不猜一个结论", () => {
  const display = deriveDisplayStatus({ taskStatus: "NOT_A_STATE" });
  assert.equal(display.outcome, "UNKNOWN");
  assert.match(display.label, /无法判定/);
});

// ==========================================================================
// 五、判定必须覆盖契约的全部枚举值（防遗漏）
// ==========================================================================

test("契约里的每个任务状态都有明确结论（不得落到「无法判定」）", () => {
  // 注意判据：`UNKNOWN` **是**合法结论（结果未知就是未知）。
  // 这里排除的是"未识别的状态组合"那条兜底路径——契约里已声明的状态
  // 不应该走到它，否则等于实现了却没被识别。
  for (const status of TASK_STATES) {
    const display = deriveDisplayStatus({ taskStatus: status });
    assert.notEqual(
      display.label,
      "无法判定",
      `任务状态 ${status} 落到了兜底路径——契约状态必须被显式处理`,
    );
    assert.ok(DISPLAY_OUTCOMES.includes(display.outcome));
  }
});

test("契约里的每个命令状态都有明确结论（不得落到「无法判定」）", () => {
  for (const status of COMMAND_STATES) {
    const display = deriveDisplayStatus({ commandStatus: status });
    assert.notEqual(
      display.label,
      "无法判定",
      `命令状态 ${status} 落到了兜底路径——契约状态必须被显式处理`,
    );
    assert.ok(DISPLAY_OUTCOMES.includes(display.outcome));
  }
});

test("UNKNOWN 命令状态显示的是「结果未知，需对账」而不是笼统的未知", () => {
  const display = deriveDisplayStatus({ commandStatus: "UNKNOWN" });
  assert.equal(display.outcome, "UNKNOWN");
  assert.match(display.label, /对账/);
});

test("任务成功 + 命令成功的组合仍然只显示未过账（无反例遗漏）", () => {
  // 穷举"看起来最成功"的组合，断言在没有回执时**一律不得** POSTED。
  for (const taskStatus of ["SUCCEEDED", "RUNNING"]) {
    for (const commandStatus of ["SUCCEEDED", "EXECUTING", "AUTHORIZED", "ACCEPTED"]) {
      const display = deriveDisplayStatus({ taskStatus, commandStatus, receipt: null });
      assert.notEqual(
        display.outcome,
        "POSTED",
        `task=${taskStatus} command=${commandStatus} 无回执却显示已过账`,
      );
    }
  }
});

test("任何输入下都不产生契约枚举之外的过账状态", () => {
  for (const posting of POSTING_STATUSES) {
    const display = deriveDisplayStatus({
      taskStatus: "SUCCEEDED",
      commandStatus: "SUCCEEDED",
      receipt: postedReceipt({ posting_status: posting }),
    });
    assert.ok(DISPLAY_OUTCOMES.includes(display.outcome));
  }
});

// ==========================================================================
// 六、汇总只走同一个判定出口
// ==========================================================================

test("summarize 的计数与逐行判定一致", () => {
  const items = [
    { taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED", receipt: null },
    { taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED", receipt: postedReceipt() },
    { commandStatus: "UNKNOWN" },
    { taskStatus: "WAITING_APPROVAL" },
  ];
  const summary = summarize(items);
  assert.equal(summary.total, 4);
  assert.equal(summary.byOutcome.NOT_POSTED, 2);
  assert.equal(summary.byOutcome.POSTED, 1);
  assert.equal(summary.byOutcome.UNKNOWN, 1);
  // 逐行结果必须与计数来自同一次判定
  const recomputed = items.map((i) => deriveDisplayStatus(i).outcome);
  const counted = Object.entries(summary.byOutcome).flatMap(([outcome, n]) =>
    Array(n).fill(outcome),
  );
  assert.deepEqual(counted.sort(), recomputed.sort());
});

// ==========================================================================
// 七、管理权与批准权分离（上游 §10.1）
// ==========================================================================

test("配置权与批准权完全不重叠 ⇒ 已分离", () => {
  const result = checkAuthoritySeparation({
    configActors: ["user-config-a"],
    approverActors: ["user-approver-b"],
  });
  assert.equal(result.separated, true);
  assert.deepEqual(result.overlapping, []);
});

test("同一身份既能改配置又能批准 ⇒ 未分离，且点名是谁", () => {
  const result = checkAuthoritySeparation({
    configActors: ["user-a", "user-b"],
    approverActors: ["user-b"],
  });
  assert.equal(result.separated, false);
  assert.deepEqual(result.overlapping, ["user-b"]);
});

test("空集合视为已分离（没有人有双重权限）", () => {
  assert.equal(checkAuthoritySeparation({}).separated, true);
});
