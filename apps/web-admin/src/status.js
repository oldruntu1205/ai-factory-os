/**
 * 后台的**状态显示契约**（上游 §6 三条中心调用链 + §17 A14）。
 *
 * > Task 只保存编排状态投影，Command 的最终副作用状态归 Transaction；
 * > **控制台显示需带状态来源和更新时间，防止 UI 显示成功而 ERP 未过账。**
 *
 * ## 这个模块为什么是纯函数
 *
 * "显示什么"是个**判定**，不是渲染细节。把它抽成纯函数有三个好处：
 *
 * 1. 判定可以被**单元测试**逐条钉住（包括"看起来成功但其实没落账"的反例）；
 * 2. 判定与 UI 框架无关，换成任何渲染层都复用同一份口径；
 * 3. 判定不依赖网络——真实数据来自 API，但"怎么读这些数据"是可复现的。
 *
 * ## 一条压倒性原则
 *
 * **只有权威回执能说"已过账"。** 命令状态、任务状态、运行状态**都不是**账务事实：
 * - `task.status = SUCCEEDED` 只表示"编排跑完了"；
 * - `command.status = SUCCEEDED` 只表示"执行侧认为成功了"；
 * - 只有 `receipt.authority_verified === true` 且 `posting_status === "POSTED"`
 *   才是"ERP/账务真的过了账"。
 *
 * 上游明文要求"防止 UI 显示成功而 ERP 未过账"——所以这里**默认一切为
 * 未确认**，除非有权威回执明确说已过账。反过来写（默认成功、有异常才降级）
 * 会让"没拿到回执"这种情况显示成成功，正是要防的那件事。
 *
 * @module status
 */

/** 任务状态（契约 `state-machines.json#/$defs/Task`）。 */
export const TASK_STATES = Object.freeze([
  "ACCEPTED",
  "RUNNING",
  "WAITING_APPROVAL",
  "WAITING_HUMAN",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
]);

/** 命令状态（契约 `state-machines.json#/$defs/Command`）。 */
export const COMMAND_STATES = Object.freeze([
  "ACCEPTED",
  "AUTHORIZED",
  "EXECUTING",
  "SUCCEEDED",
  "FAILED",
  "UNKNOWN",
  "MANUAL_REVIEW",
]);

/** 过账状态（契约 `p15-receipt.json`）。 */
export const POSTING_STATUSES = Object.freeze([
  "NOT_CONFIRMED",
  "NOT_APPLICABLE",
  "POSTED",
]);

/**
 * 显示口径（**本模块自己的**枚举，不是契约枚举）。
 *
 * 刻意与契约状态分开：契约描述"系统处于什么状态"，
 * 显示口径描述"人能据此得出什么结论"——两者混用会导致
 * 把编排状态误当成账务结论。
 */
export const DISPLAY_OUTCOMES = Object.freeze([
  "NOT_POSTED",       // 未过账（含"还在跑""等批准"）
  "POSTED",           // 已过账（**只有权威回执能得出**）
  "FAILED",           // 已失败
  "UNKNOWN",          // 结果未知（超时等），需对账
  "NEEDS_HUMAN",      // 需人工介入
  "NOT_APPLICABLE",   // 本命令不涉及账务
]);

/** 任务状态 → 是否"还在进行中"。 */
const IN_FLIGHT_TASK_STATES = new Set([
  "ACCEPTED",
  "RUNNING",
  "WAITING_APPROVAL",
  "WAITING_HUMAN",
]);

/** 命令状态 → 是否"还没到终态"。 */
const IN_FLIGHT_COMMAND_STATES = new Set(["ACCEPTED", "AUTHORIZED", "EXECUTING"]);

/**
 * 从回执里读出"权威地"过账结论。
 *
 * `authority_verified !== true` 时**一律视为未确认**——未经验证的执行方自报
 * 不算权威（上游要求"任务成功只由权威执行回执驱动"）。
 *
 * @param {object|null|undefined} receipt
 * @returns {{posted: boolean, notApplicable: boolean, reason: string, evidenceRef: string|null}}
 */
export function readAuthorityPosting(receipt) {
  if (!receipt || typeof receipt !== "object") {
    return {
      posted: false,
      notApplicable: false,
      reason: "尚未取得执行回执",
      evidenceRef: null,
    };
  }
  if (receipt.authority_verified !== true) {
    return {
      posted: false,
      notApplicable: false,
      reason: "回执未经权威验证（执行方自报不算账务事实）",
      evidenceRef: null,
    };
  }
  const evidenceRef =
    typeof receipt.posting_evidence_ref === "string" && receipt.posting_evidence_ref
      ? receipt.posting_evidence_ref
      : null;

  if (receipt.posting_status === "POSTED") {
    // 权威 + 已过账：这才允许显示"已过账"
    return {
      posted: true,
      notApplicable: false,
      reason: "权威回执确认已过账",
      evidenceRef,
    };
  }
  if (receipt.posting_status === "NOT_APPLICABLE") {
    return {
      posted: false,
      notApplicable: true,
      reason: "该命令按业务口径不涉及账务过账",
      evidenceRef,
    };
  }
  return {
    posted: false,
    notApplicable: false,
    reason: "权威回执确认**未**过账",
    evidenceRef,
  };
}

/**
 * 推导界面上该显示什么结论。
 *
 * 输入只取该有的东西：任务状态、命令状态、回执。**不要传"我们觉得成功了"**。
 *
 * @param {object} input
 * @param {string} [input.taskStatus]    契约 Task 状态
 * @param {string} [input.commandStatus] 契约 Command 状态
 * @param {object} [input.receipt]       权威回执（可为空）
 * @param {string} [input.taskUpdatedAt] 任务投影的更新时间（契约必填）
 * @param {string} [input.commandUpdatedAt]
 * @param {string} [input.taskStatusSource]
 * @param {string} [input.commandStatusSource]
 * @returns {{
 *   outcome: string,
 *   label: string,
 *   reason: string,
 *   sources: Array<{name: string, status: string|null, source: string|null, updatedAt: string|null}>,
 *   evidenceRef: string|null,
 *   authoritative: boolean,
 * }}
 */
export function deriveDisplayStatus(input = {}) {
  const {
    taskStatus = null,
    commandStatus = null,
    receipt = null,
    taskUpdatedAt = null,
    commandUpdatedAt = null,
    taskStatusSource = null,
    commandStatusSource = null,
  } = input ?? {};

  // 所有结论都带来源与更新时间（上游明文要求"显示需带状态来源和更新时间"）。
  const sources = [
    {
      name: "task",
      status: taskStatus,
      source: taskStatusSource,
      updatedAt: taskUpdatedAt,
    },
    {
      name: "command",
      status: commandStatus,
      source: commandStatusSource,
      updatedAt: commandUpdatedAt,
    },
    {
      name: "receipt",
      status: receipt ? readAuthorityPosting(receipt).reason : null,
      source: receipt?.status_source ?? null,
      updatedAt: receipt?.confirmed_at ?? receipt?.recorded_at ?? null,
    },
  ];

  const posting = readAuthorityPosting(receipt);

  // ① 权威回执优先：它说已过账就是已过账（哪怕命令状态看起来还在跑）。
  if (posting.posted) {
    return {
      outcome: "POSTED",
      label: "已过账",
      reason: posting.reason,
      sources,
      evidenceRef: posting.evidenceRef,
      authoritative: true,
    };
  }

  // ② 不涉及账务：显式说明，不显示成"未过账"（两者含义完全不同）。
  if (posting.notApplicable) {
    return {
      outcome: "NOT_APPLICABLE",
      label: "不涉及过账",
      reason: posting.reason,
      sources,
      evidenceRef: posting.evidenceRef,
      authoritative: true,
    };
  }

  // ③ 结果未知：**必须单独显示**，不能含糊成"处理中"。
  //    未知意味着"可能已经执行了"，需要人工对账，而不是等它自己好。
  if (commandStatus === "UNKNOWN") {
    return {
      outcome: "UNKNOWN",
      label: "结果未知，需对账",
      reason: "命令进入 UNKNOWN：可能已执行，须查状态而不是换键重发",
      sources,
      evidenceRef: null,
      authoritative: false,
    };
  }

  // ④ 需人工：补偿失败等。
  if (commandStatus === "MANUAL_REVIEW" || taskStatus === "WAITING_HUMAN") {
    return {
      outcome: "NEEDS_HUMAN",
      label: "需人工介入",
      reason:
        taskStatus === "WAITING_HUMAN"
          ? "任务等待人工处理"
          : "命令进入人工复核队列",
      sources,
      evidenceRef: null,
      authoritative: false,
    };
  }

  // ⑤ 明确失败。
  if (taskStatus === "FAILED" || commandStatus === "FAILED") {
    return {
      outcome: "FAILED",
      label: "已失败",
      reason: taskStatus === "FAILED" ? "任务失败" : "命令失败",
      sources,
      evidenceRef: null,
      authoritative: false,
    };
  }

  // ⑥ 已取消：不是失败，也不是成功——单独说明。
  if (taskStatus === "CANCELLED") {
    return {
      outcome: "NOT_POSTED",
      label: "已取消",
      reason: "任务已取消",
      sources,
      evidenceRef: null,
      authoritative: false,
    };
  }

  // ⑦ 还在进行中：**默认落到这里**——没有权威回执之前，一律不显示成功。
  if (
    IN_FLIGHT_TASK_STATES.has(taskStatus) ||
    IN_FLIGHT_COMMAND_STATES.has(commandStatus) ||
    (!taskStatus && !commandStatus)
  ) {
    let reason = "处理中";
    if (taskStatus === "WAITING_APPROVAL") reason = "等待批准";
    else if (taskStatus === "WAITING_HUMAN") reason = "等待人工处理";
    else if (taskStatus === "ACCEPTED" && commandStatus === null) {
      reason = "已受理，尚未提交执行";
    } else if (commandStatus === "EXECUTING") reason = "执行中";
    return {
      outcome: "NOT_POSTED",
      label: "未过账",
      reason,
      sources,
      evidenceRef: null,
      authoritative: false,
    };
  }

  // ⑧ 编排说成功、但没有权威回执：**恰恰是要防的那种显示**。
  //    这里显式说明"编排已完成，但账务未确认"，而不是笼统写"处理中"。
  if (taskStatus === "SUCCEEDED" || commandStatus === "SUCCEEDED") {
    return {
      outcome: "NOT_POSTED",
      label: "未过账",
      reason: posting.reason || "编排已完成，但未取得权威过账回执",
      sources,
      evidenceRef: null,
      authoritative: false,
    };
  }

  // ⑨ 未知状态：不猜。宁可显示"无法判定"也不要给一个偶发正确的结论。
  return {
    outcome: "UNKNOWN",
    label: "无法判定",
    reason: `未识别的状态组合（task=${taskStatus ?? "null"} command=${commandStatus ?? "null"}）`,
    sources,
    evidenceRef: null,
    authoritative: false,
  };
}

/**
 * 汇总一批任务的显示结论（列表页用）。
 *
 * 单独抽出来是为了让"计数"也只从同一个判定出口来——
 * 否则列表页可能自己按 `task.status` 数一遍，与逐行显示的口径不一致。
 *
 * @param {Array<object>} inputs
 * @returns {{total: number, byOutcome: Record<string, number>}}
 */
export function summarize(inputs = []) {
  const byOutcome = {};
  for (const item of inputs) {
    const { outcome } = deriveDisplayStatus(item);
    byOutcome[outcome] = (byOutcome[outcome] ?? 0) + 1;
  }
  return { total: inputs.length, byOutcome };
}

/**
 * 配置写入权与批准权是否分离（上游 §10.1「管理权与业务批准权分开」）。
 *
 * 同一个身份既能改配置又能批准业务写入，等于把"设置规则的人"
 * 与"批准例外的人"合成一个——审计上无法区分。
 *
 * @param {{configActors?: string[]|Set<string>, approverActors?: string[]|Set<string>}} input
 * @returns {{separated: boolean, overlapping: string[]}}
 */
export function checkAuthoritySeparation(input = {}) {
  const config = new Set(input.configActors ?? []);
  const approver = new Set(input.approverActors ?? []);
  const overlapping = [...config].filter((actor) => approver.has(actor)).sort();
  return { separated: overlapping.length === 0, overlapping };
}
