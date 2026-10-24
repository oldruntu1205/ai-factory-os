/**
 * 后台列表页的**视图模型**（上游 §6 / §17 A14）。
 *
 * 这一层只做"把判定结果变成可显示的行/筛选/排序"，
 * **不自己判断任何业务结论**——所有结论都来自 `status.js` 的 `deriveDisplayStatus`。
 *
 * ## 为什么单独一层
 *
 * 列表页最容易犯的错是**自己数一遍**：按 `task.status === 'SUCCEEDED'` 统计"成功数"。
 * 那个数字会与逐行显示的结论不一致（因为逐行走了回执判定），
 * 于是同一屏上两个数字互相矛盾，而且**没人知道哪个对**。
 *
 * 所以这里只做两件事：调用同一个判定出口、把结果排序与筛选。
 *
 * 零依赖：不引入 UI 框架，浏览器可直接 `<script type="module">` 加载。
 * 生产若要用 React/TypeScript（上游建议），替换的是**渲染**，
 * 本模块与 `status.js` 原样复用。
 */

import { deriveDisplayStatus, summarize } from "./status.js";

/** 显示口径的排序优先级：**把需要人看的排前面**。 */
export const OUTCOME_PRIORITY = Object.freeze([
  "UNKNOWN",       // 结果未知：可能已执行，必须查
  "NEEDS_HUMAN",   // 需人工
  "FAILED",        // 失败
  "NOT_POSTED",    // 未过账（含处理中）
  "NOT_APPLICABLE",
  "POSTED",        // 已过账：最不需要关注，排最后
]);

/**
 * 把一条任务记录变成列表行。
 *
 * @param {object} record 来自 API 的任务视图（含可选 command/receipt）
 * @returns {object} 行模型
 */
export function toRow(record = {}) {
  const display = deriveDisplayStatus(record);
  return {
    taskId: record.task_id ?? null,
    tenantId: record.tenant_id ?? null,
    employeeId: record.employee_id ?? null,
    taskStatus: record.taskStatus ?? record.status ?? null,
    commandStatus: record.commandStatus ?? null,
    taskUpdatedAt: record.taskUpdatedAt ?? record.updated_at ?? null,
    taskStatusSource: record.taskStatusSource ?? record.status_source ?? null,
    outcome: display.outcome,
    label: display.label,
    reason: display.reason,
    evidenceRef: display.evidenceRef,
    // 行上必须能看到"这个结论是怎么来的"——上游明文要求带来源与更新时间
    sources: display.sources,
    authoritative: display.authoritative,
  };
}

/**
 * 构造列表页视图：行 + 汇总 + 筛选器。
 *
 * 汇总由 `summarize` 产出（与行同一判定出口），**不在这一层另算**。
 *
 * @param {object[]} records
 * @param {{outcome?: string|null, includeCompleted?: boolean}} [options]
 */
export function buildListView(records = [], options = {}) {
  const rows = records.map(toRow);
  const summary = summarize(records);

  const outcome = options.outcome ?? null;
  const visible = outcome ? rows.filter((row) => row.outcome === outcome) : rows.slice();

  visible.sort((a, b) => {
    const pa = OUTCOME_PRIORITY.indexOf(a.outcome);
    const pb = OUTCOME_PRIORITY.indexOf(b.outcome);
    if (pa !== pb) return pa - pb;
    // 同优先级按更新时间倒序：先看最新的
    const ta = a.taskUpdatedAt ?? "";
    const tb = b.taskUpdatedAt ?? "";
    if (ta !== tb) return tb.localeCompare(ta);
    return String(a.taskId ?? "").localeCompare(String(b.taskId ?? ""));
  });

  return {
    rows: visible,
    totalRows: rows.length,
    summary,
    filter: { outcome },
    // 空列表要区分"没有数据"与"筛选后为空"——两者给用户的下一步动作不同
    emptyReason:
      rows.length === 0
        ? "尚无任务"
        : visible.length === 0
          ? `没有「${outcome}」结论的任务`
          : null,
  };
}

/**
 * 汇总卡片的文案（**不改变数值**，只负责把口径说清楚）。
 *
 * 关键：`POSTED` 的数字旁边必须写明"仅凭权威回执"，否则看的人会以为
 * 那是"任务成功数"——两者在口径上完全不同。
 *
 * @param {{total: number, byOutcome: Record<string, number>}} summary
 */
export function summarizeLabels(summary) {
  const byOutcome = summary?.byOutcome ?? {};
  const card = (outcome, title, note) => ({
    outcome,
    title,
    count: byOutcome[outcome] ?? 0,
    note,
  });
  return [
    card("POSTED", "已过账", "仅凭权威回执判定"),
    card("NOT_POSTED", "未过账", "含处理中与已取消"),
    card("UNKNOWN", "结果未知", "须查状态，不可换键重发"),
    card("NEEDS_HUMAN", "需人工", "含补偿失败"),
    card("FAILED", "已失败", ""),
    card("NOT_APPLICABLE", "不涉及过账", "无账务动作"),
  ];
}

/**
 * 渲染成 HTML 字符串（无框架）。
 *
 * 刻意**转义所有来自数据源的文本**：任务/命令字段来自后端，
 * 直接内插会有注入风险，而"后台里只给内部人看"不是理由。
 *
 * @param {object} view
 * @returns {string}
 */
export function renderRows(view) {
  if (!view.rows.length) {
    return `<p class="empty">${escapeHtml(view.emptyReason ?? "无数据")}</p>`;
  }
  const body = view.rows
    .map(
      (row) => `
      <tr data-outcome="${escapeHtml(row.outcome)}">
        <td>${escapeHtml(row.taskId ?? "")}</td>
        <td><span class="label">${escapeHtml(row.label)}</span></td>
        <td>${escapeHtml(row.reason)}</td>
        <td>${escapeHtml(row.taskStatus ?? "")}</td>
        <td>${escapeHtml(row.commandStatus ?? "")}</td>
        <td class="src">${escapeHtml(row.taskStatusSource ?? "—")}</td>
        <td class="ts">${escapeHtml(row.taskUpdatedAt ?? "—")}</td>
        <td>${row.evidenceRef ? `<code>${escapeHtml(row.evidenceRef)}</code>` : "—"}</td>
      </tr>`,
    )
    .join("");
  return `<table><thead><tr>
      <th>任务</th><th>结论</th><th>理由</th><th>任务状态</th><th>命令状态</th>
      <th>状态来源</th><th>更新时间</th><th>过账证据</th>
    </tr></thead><tbody>${body}</tbody></table>`;
}

/**
 * HTML 转义。允许空值。
 * @param {unknown} value
 */
export function escapeHtml(value) {
  // `String(value ?? "")`：缺失值渲染成**空**，而不是字面量 `"null"` / `"undefined"`。
  //
  // 2026-10-05 与页面内联的 `esc` 对齐时发现两份分叉：
  // 本函数原来写 `String(value)`，于是 `escapeHtml(null) === "null"`——
  // 用户会在表格里看到 `null` 五个字符。页面那份有 `?? ""`，所以不显示。
  //
  // 两份都是**转义过**的，所以当时不是 XSS，是显示缺陷；
  // 但**行为不一致本身就说明它没被设计过**。选 `?? ""` 是因为界面语义是"没有值"。
  // 一致性由 `tests/infra/test_escape_parity.py` 用 node 真跑两侧来守。
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
