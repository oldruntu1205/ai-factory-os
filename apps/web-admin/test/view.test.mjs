/**
 * 列表页视图模型的门禁。
 *
 * 重点守两件事：
 * 1. **汇总数字与逐行结论同源**——列表页不得自己按 `task.status` 数一遍；
 * 2. **渲染必须转义**——后端字段直接内插会有注入风险。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { deriveDisplayStatus, summarize } from "../src/status.js";
import {
  OUTCOME_PRIORITY,
  buildListView,
  escapeHtml,
  renderRows,
  summarizeLabels,
  toRow,
} from "../src/view.js";

function postedReceipt() {
  return {
    receipt_id: "r-1",
    posting_status: "POSTED",
    posting_evidence_ref: "ev-1",
    authority_verified: true,
    confirmed_at: "2026-10-05T00:00:00Z",
    status_source: "transaction-service",
  };
}

// ==========================================================================
// 一、行模型：结论来自 status.js，不自己判断
// ==========================================================================

test("行模型的结论与 deriveDisplayStatus 完全一致", () => {
  const record = { task_id: "t-1", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED" };
  const row = toRow(record);
  const display = deriveDisplayStatus(record);
  assert.equal(row.outcome, display.outcome);
  assert.equal(row.label, display.label);
  assert.deepEqual(row.sources, display.sources);
});

test("行模型保留来源与更新时间（供界面显示结论出处）", () => {
  const row = toRow({
    task_id: "t-1",
    taskStatus: "RUNNING",
    taskStatusSource: "control-plane",
    taskUpdatedAt: "2026-10-05T02:00:00Z",
  });
  assert.equal(row.taskStatusSource, "control-plane");
  assert.equal(row.taskUpdatedAt, "2026-10-05T02:00:00Z");
});

test("行模型兼容契约字段名（status / updated_at）", () => {
  const row = toRow({ task_id: "t-1", status: "FAILED", updated_at: "2026-10-05T03:00:00Z" });
  assert.equal(row.taskStatus, "FAILED");
  assert.equal(row.taskUpdatedAt, "2026-10-05T03:00:00Z");
});

// ==========================================================================
// 二、汇总与逐行同源（不得两套口径）
// ==========================================================================

test("汇总数字与逐行结论同源", () => {
  const records = [
    { task_id: "a", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED" },
    { task_id: "b", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED", receipt: postedReceipt() },
    { task_id: "c", commandStatus: "UNKNOWN" },
  ];
  const view = buildListView(records);
  assert.deepEqual(view.summary, summarize(records));
  const counted = view.rows.reduce((acc, row) => {
    acc[row.outcome] = (acc[row.outcome] ?? 0) + 1;
    return acc;
  }, {});
  for (const [outcome, n] of Object.entries(counted)) {
    assert.equal(view.summary.byOutcome[outcome], n, `${outcome} 计数与行不一致`);
  }
});

test("**「已过账」的数字不含仅编排成功的任务**", () => {
  // 这是 A14 在列表页的落点：4 条任务全部 SUCCEEDED，但只有 1 条有权威回执。
  const records = [
    { task_id: "a", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED" },
    { task_id: "b", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED" },
    { task_id: "c", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED" },
    { task_id: "d", taskStatus: "SUCCEEDED", commandStatus: "SUCCEEDED", receipt: postedReceipt() },
  ];
  const view = buildListView(records);
  assert.equal(view.summary.byOutcome.POSTED, 1);
  assert.equal(view.summary.byOutcome.NOT_POSTED, 3);
});

// ==========================================================================
// 三、排序：需要人看的排前面
// ==========================================================================

test("结果未知/需人工/失败 排在已过账之前", () => {
  const records = [
    { task_id: "posted", taskStatus: "SUCCEEDED", receipt: postedReceipt() },
    { task_id: "unknown", commandStatus: "UNKNOWN" },
    { task_id: "failed", taskStatus: "FAILED" },
    { task_id: "human", commandStatus: "MANUAL_REVIEW" },
  ];
  const ids = buildListView(records).rows.map((r) => r.taskId);
  assert.deepEqual(ids, ["unknown", "human", "failed", "posted"]);
});

test("同优先级按更新时间倒序", () => {
  const records = [
    { task_id: "old", taskStatus: "RUNNING", taskUpdatedAt: "2026-10-05T01:00:00Z" },
    { task_id: "new", taskStatus: "RUNNING", taskUpdatedAt: "2026-10-05T05:00:00Z" },
  ];
  assert.deepEqual(
    buildListView(records).rows.map((r) => r.taskId),
    ["new", "old"],
  );
});

test("排序优先级表必须覆盖全部显示口径", () => {
  const known = new Set(OUTCOME_PRIORITY);
  const records = [
    { taskStatus: "SUCCEEDED", receipt: postedReceipt() },
    { taskStatus: "SUCCEEDED" },
    { commandStatus: "UNKNOWN" },
    { commandStatus: "MANUAL_REVIEW" },
    { taskStatus: "FAILED" },
    { taskStatus: "SUCCEEDED", receipt: { authority_verified: true, posting_status: "NOT_APPLICABLE" } },
  ];
  for (const record of records) {
    const { outcome } = deriveDisplayStatus(record);
    assert.ok(known.has(outcome), `显示口径 ${outcome} 未登记排序优先级`);
  }
});

// ==========================================================================
// 四、筛选与空态
// ==========================================================================

test("按结论筛选只保留该结论的行", () => {
  const records = [
    { task_id: "a", taskStatus: "SUCCEEDED" },
    { task_id: "b", commandStatus: "UNKNOWN" },
  ];
  const view = buildListView(records, { outcome: "UNKNOWN" });
  assert.equal(view.rows.length, 1);
  assert.equal(view.rows[0].taskId, "b");
  // 汇总仍是**全集**的汇总：筛选不改变总体态势
  assert.equal(view.summary.total, 2);
});

test("空列表与筛选后为空给出不同说明", () => {
  assert.match(buildListView([]).emptyReason, /尚无任务/);
  const filtered = buildListView([{ taskStatus: "RUNNING" }], { outcome: "FAILED" });
  assert.match(filtered.emptyReason, /没有.*FAILED|没有.*已失败/);
});

// ==========================================================================
// 五、渲染必须转义（防注入）
// ==========================================================================

test("来自后端的文本被转义", () => {
  const view = buildListView([
    {
      task_id: '<img src=x onerror="alert(1)">',
      taskStatus: "FAILED",
      taskStatusSource: "<script>bad()</script>",
    },
  ]);
  const html = renderRows(view);
  assert.ok(!html.includes("<img src=x"), "任务 id 未转义");
  assert.ok(!html.includes("<script>"), "状态来源未转义");
  assert.ok(html.includes("&lt;img"), "应当出现转义后的文本");
});

test("escapeHtml 覆盖五种危险字符", () => {
  assert.equal(escapeHtml(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
});

test("空列表渲染空态而不是空表格", () => {
  const html = renderRows(buildListView([]));
  assert.match(html, /class="empty"/);
  assert.ok(!html.includes("<table>"));
});

test("表格表头包含『状态来源』与『更新时间』两列", () => {
  // 上游明文要求"显示需带状态来源和更新时间"——列必须在
  const html = renderRows(buildListView([{ taskStatus: "RUNNING" }]));
  assert.ok(html.includes("状态来源"));
  assert.ok(html.includes("更新时间"));
});

// ==========================================================================
// 六、汇总卡片文案：口径必须说清楚
// ==========================================================================

test("「已过账」卡片注明仅凭权威回执", () => {
  const cards = summarizeLabels({ total: 1, byOutcome: { POSTED: 1 } });
  const posted = cards.find((c) => c.outcome === "POSTED");
  assert.match(posted.note, /权威回执/);
});

test("「结果未知」卡片提醒不可换键重发", () => {
  const cards = summarizeLabels({ total: 1, byOutcome: { UNKNOWN: 1 } });
  const unknown = cards.find((c) => c.outcome === "UNKNOWN");
  assert.match(unknown.note, /查状态|换键/);
});

test("缺失的结论计数为 0 而不是 undefined", () => {
  const cards = summarizeLabels({ total: 0, byOutcome: {} });
  for (const card of cards) {
    assert.equal(typeof card.count, "number");
    assert.equal(card.count, 0);
  }
});
