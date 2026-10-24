/**
 * 批准页的**可批准性判定**（上游 §7 风险授权 / §10.1 控制 API / §17 A06）。
 *
 * > Business Control：风险分类、策略判定、审批、对象版本和职责分离；
 * > 执行时再次授权。**不把模型自报置信度或自然语言"批准"当作授权。**
 *
 * ## 这个模块的态度：能验的验，缺数据的**说缺**
 *
 * 上游要求 R3 具备"明确岗位批准、职责分离、限额"。核查登记表后发现：
 * 里面只有 `actors` 与 `delegated_scopes`，**没有角色、没有岗位、没有限额**——
 * 这些属上游 §16.1 明列的外部依赖（"身份和批准岗位：SSO、tenant/factory 边界、
 * 角色、职责分离、限额"），当前**没有数据来源**。
 *
 * 所以这里**不去实现一个假的限额检查**（拿不到阈值就编一个，等于给人
 * "已经管控了"的错觉），而是：
 *
 * - 有数据支撑的判定 ⇒ 真判（可决定 / 阻塞）；
 * - 依赖角色与限额的判定 ⇒ 结论记为 `unknown`，并在 `requiresData` 里
 *   **点名缺什么**，界面上就显示"此项待外部冻结"。
 *
 * 这样"已核实"与"无法核实"在界面上一眼可分——与状态契约里
 * "已过账 / 未过账"的处理方式一致。
 *
 * @module approval
 */

/** 可批准性结论（**本模块的**枚举，不是契约枚举）。 */
export const DECIDABILITY = Object.freeze([
  "DECIDABLE",        // 现在可以决定（或已决定）
  "BLOCKED",          // 有明确理由不能决定
  "UNKNOWN",          // 关键输入缺失，无法判定（**不得当成可以**）
]);

/** 逐项检查的结论。 */
export const CHECK_RESULTS = Object.freeze([
  "PASS",
  "FAIL",
  "NOT_APPLICABLE",
  "UNKNOWN",          // 缺数据
]);

/**
 * 自然语言"批准"的识别。
 *
 * 上游明文：**不把自然语言"批准"当作授权**。这条最容易在 UI 层失守——
 * 一个"意见"输入框 + 一个"通过"按钮，很容易做成"填了意见就算批准了"。
 *
 * 所以这里把"看起来像自然语言批准"的输入识别出来并**明确拒绝**：
 * 授权只能来自**结构化的决定**（`approved: true/false` + 决定人身份），
 * 不能来自一段话。
 */
const NATURAL_LANGUAGE_APPROVAL = [
  /同意/i,
  /同意批准/,
  /批准/,
  /通过/,
  /可以执行/,
  /没问题/,
  /\bok(ay)?\b/i,
  /\bapprove[ds]?\b/i,
  /\byes\b/i,
  /\bgo ahead\b/i,
];

/**
 * 判断一个"决定"是否为自然语言而非结构化授权。
 *
 * @param {unknown} value 用户填在"意见/说明"里的内容
 * @returns {{naturalLanguage: boolean, matched: string|null}}
 */
export function looksLikeNaturalLanguageApproval(value) {
  if (typeof value !== "string") {
    return { naturalLanguage: false, matched: null };
  }
  const text = value.trim();
  if (!text) {
    return { naturalLanguage: false, matched: null };
  }
  for (const pattern of NATURAL_LANGUAGE_APPROVAL) {
    const hit = text.match(pattern);
    if (hit) {
      return { naturalLanguage: true, matched: hit[0] };
    }
  }
  return { naturalLanguage: false, matched: null };
}

/**
 * 把表单提交转成**结构化决定**，并挡住自然语言冒充授权。
 *
 * @param {object} form
 * @param {unknown} [form.approved]   必须是布尔（`true` 通过 / `false` 否决）
 * @param {unknown} [form.comment]    说明文字（**不构成授权**）
 * @returns {{ok: true, approved: boolean, comment: string|null}
 *          | {ok: false, code: string, reason: string}}
 */
export function normalizeDecision(form = {}) {
  const { approved, comment = null } = form ?? {};

  // ① 必须是真正的布尔。"true" 字符串、1、'yes' 都不算——
  //    宽松接受会让"随手打了个字"变成一次授权。
  if (typeof approved !== "boolean") {
    return {
      ok: false,
      code: "DECISION_NOT_STRUCTURED",
      reason:
        "决定必须是结构化的 approved=true/false；字符串、数字或自然语言都不构成授权",
    };
  }

  // ② 说明文字里出现"批准/同意/通过"之类的字样 ⇒ 拒绝。
  //    不是为了禁言，而是防止"填了说明就算批准"这种界面误导。
  const natural = looksLikeNaturalLanguageApproval(comment);
  if (natural.naturalLanguage) {
    return {
      ok: false,
      code: "NATURAL_LANGUAGE_APPROVAL_REJECTED",
      reason:
        `说明文字里的「${natural.matched}」不构成授权；` +
        "授权只来自 approved=true/false 这个结构化字段",
    };
  }

  return { ok: true, approved, comment: typeof comment === "string" && comment.trim() ? comment.trim() : null };
}

/**
 * 逐项检查一条批准记录当前是否可决定。
 *
 * @param {object} input
 * @param {object} input.approval            批准记录（契约 `ApprovalBinding`/`Fact`）
 * @param {string|null} [input.decidedAt]    "现在"（**由调用方传入**，便于测试与复现）
 * @param {object|null} [input.authority]    审批人的**岗位与授权**（来自登记表）：
 *   契约 `p3-employee.json` 的 `EmployeeRecord`。**不传 = 没数据**，
 *   三项检查如实记 `UNKNOWN`——**绝不因为"没给"就放行**。
 * @returns {{
 *   decidability: string,
 *   label: string,
 *   checks: Array<{name: string, result: string, reason: string}>,
 *   blockReasons: string[],
 *   requiresData: string[],
 *   unknownChecks: string[],
 * }}
 */
export function assessApproval(input = {}) {
  const { approval = {}, decidedAt = null, authority = null } = input ?? {};
  const checks = [];

  const push = (name, result, reason) => checks.push({ name, result, reason });

  /**
   * 审批人是否**恰好**在一个声称具备某能力的岗位里。
   *
   * `forbiddenOnly` 用来区分两种"找不到"：
   * - 该岗位明确写了 `FORBIDDEN`（**有数据说不行**）；
   * - 根本没有覆盖该风险等级的岗位（**数据不足**）。
   * 这两者结论不同（FAIL vs UNKNOWN），不能合并。
   */
  const positions = Array.isArray(authority?.positions) ? authority.positions : null;
  const limitsOf = (riskClass) =>
    positions
      ?.flatMap((position) => (Array.isArray(position.limits) ? position.limits : []))
      .filter((limit) => limit?.risk_class === riskClass) ?? [];

  // ① 记录完整性：缺关键字段就无法安全判定
  const hasBinding = Boolean(approval.binding && typeof approval.binding === "object");
  push(
    "binding_present",
    hasBinding ? "PASS" : "FAIL",
    hasBinding ? "批准已绑定到具体的动作与对象" : "批准缺少 binding，无法确定它批的是什么",
  );

  const approverRef = approval.approver_ref ?? null;
  push(
    "approver_recorded",
    approverRef ? "PASS" : "FAIL",
    approverRef ? "已记录的批准人可用于职责分离核对" : "尚未记录批准人",
  );

  // ② 是否已决定（决定不可改判是契约要求）
  const alreadyDecided = approval.approved === true || approval.approved === false;
  if (alreadyDecided) {
    push(
      "not_yet_decided",
      "FAIL",
      approval.approved === true
        ? "该批准已成立；事实追加不可变，不能再改判"
        : "该批准已被否决；事实追加不可变，不能再改判",
    );
  } else {
    push("not_yet_decided", "PASS", "尚未作出决定");
  }

  // ③ 有效期：过期批准不得再决定 / 不得用于执行
  const validUntil = approval.valid_until ?? null;
  if (!validUntil) {
    push("validity", "UNKNOWN", "批准记录未提供 valid_until，无法判断是否过期");
  } else if (decidedAt && String(validUntil) <= String(decidedAt)) {
    push("validity", "FAIL", `批准已于 ${validUntil} 失效`);
  } else if (decidedAt) {
    push("validity", "PASS", `有效期至 ${validUntil}`);
  } else {
    // 没给"现在"就不判过期——用本机时钟会让结果不可复现
    push("validity", "UNKNOWN", "未提供当前时刻，无法判断是否过期（避免用本机时钟导致结论不可复现）");
  }

  // ④ 职责分离之一（**有数据支撑**）：审批人不得是发起人。
  //    这与后端 `review_command` 的判据一致（`approver_ref != requester`）。
  const requesterRef = approval.requester_ref ?? approval.binding?.actor_id ?? null;
  if (!approverRef || !requesterRef) {
    push(
      "approver_is_not_requester",
      "UNKNOWN",
      "缺少审批人或发起人标识，无法核对职责分离",
    );
  } else if (approverRef === requesterRef) {
    push("approver_is_not_requester", "FAIL", "审批人与发起人是同一人");
  } else {
    push("approver_is_not_requester", "PASS", "审批人与发起人不同");
  }

  // ⑤ 职责分离之二：管理权与业务批准权分开（上游 §10.1）。
  //
  //    **2026-10-05 起有数据支撑**：登记表里有了 `config_authority`
  //    （契约 `p3-employee.json`）。所以这里不再是恒 `UNKNOWN`。
  //
  //    判据刻意保守：**只有拿到审批人记录、且它明确说"不许改配置"才 PASS**。
  //    "查不到这个人"是 UNKNOWN（数据不足），不是 PASS——
  //    把"没查到"当成"没问题"正是越权的入口。
  if (!authority) {
    push(
      "config_authority_separated",
      "UNKNOWN",
      "未提供审批人的岗位与授权记录，无法核对「管理权与业务批准权分开」",
    );
  } else if (authority.config_authority?.may_change_config === false) {
    push(
      "config_authority_separated",
      "PASS",
      "审批人不具备配置修改权，管理权与业务批准权已分开",
    );
  } else if (authority.config_authority?.may_change_config === true) {
    push(
      "config_authority_separated",
      "FAIL",
      "审批人同时具备配置修改权——管理权与业务批准权未分开（上游 §10.1 明令分开）",
    );
  } else {
    push(
      "config_authority_separated",
      "UNKNOWN",
      "审批人记录未声明 config_authority.may_change_config，无法判定是否已分开",
    );
  }

  // ⑥ 限额：按风险等级核对是否在授权额度内。
  //
  //    **这里只做"该等级下这个岗位能不能批"的判定**，不做金额比较——
  //    因为批准记录里没有金额字段，**没有可比的东西**。
  //    编一个金额出来比大小，比不判更糟。
  const riskClass = approval.risk_class ?? approval.binding?.risk_class ?? null;
  if (!positions || !riskClass) {
    push(
      "within_limit",
      "UNKNOWN",
      "缺少审批人岗位记录或该动作的风险等级，无法核对该等级下的授权限额",
    );
  } else {
    const limits = limitsOf(riskClass);
    if (limits.length === 0) {
      push(
        "within_limit",
        "UNKNOWN",
        `审批人的岗位未覆盖风险等级 ${riskClass}——该等级下没有可依据的授权结论`,
      );
    } else if (limits.every((limit) => limit.decision === "FORBIDDEN")) {
      push(
        "within_limit",
        "FAIL",
        `审批人的岗位在风险等级 ${riskClass} 上被明确禁止批准`,
      );
    } else {
      // 取该等级下"最宽"的那条结论作为岗位的实际权限（多岗位取并集，
      // 与契约「员工权限 = 所任岗位并集」一致）
      const order = { AUTO: 3, APPROVE: 2, FORBIDDEN: 1 };
      const best = limits.reduce((a, b) =>
        (order[b.decision] ?? 0) > (order[a.decision] ?? 0) ? b : a,
      );
      const amount = best.max_amount ? `，单笔上限 ${best.max_amount}` : "";
      const second = best.requires_second_approval ? "，且要求双人复核" : "";
      push(
        "within_limit",
        "PASS",
        `审批人岗位在风险等级 ${riskClass} 上的结论为 ${best.decision}${amount}${second}`,
      );
    }
  }

  // ⑦ 岗位批准：批准人须属于被授权岗位，且该等级下允许其批准。
  //
  //    与 ⑥ 的区别：⑥ 问"这个等级的限额允不允许"，
  //    ⑦ 问"这个人有没有被授予任何岗位来批这件事"。
  if (!positions) {
    push(
      "position_authorized",
      "UNKNOWN",
      "未提供审批人的岗位记录，无法核对批准人是否属于授权岗位",
    );
  } else if (positions.length === 0) {
    push("position_authorized", "FAIL", "审批人未被授予任何岗位，不具备批准资格");
  } else if (!riskClass) {
    push(
      "position_authorized",
      "UNKNOWN",
      "缺少该动作的风险等级，无法判断审批人的岗位是否覆盖该等级",
    );
  } else {
    const authorized = limitsOf(riskClass).some((limit) => limit.decision !== "FORBIDDEN");
    push(
      "position_authorized",
      authorized ? "PASS" : "FAIL",
      authorized
        ? `审批人的岗位覆盖风险等级 ${riskClass} 且未被禁止`
        : `审批人的岗位在风险等级 ${riskClass} 上均被禁止`,
    );
  }

  // ── 汇总 ──────────────────────────────────────────────────────────────
  const blockReasons = checks.filter((c) => c.result === "FAIL").map((c) => `${c.name}: ${c.reason}`);
  const unknownChecks = checks.filter((c) => c.result === "UNKNOWN").map((c) => c.name);

  let decidability;
  let label;
  if (blockReasons.length > 0) {
    // 有明确理由不能决定 ⇒ BLOCKED（**压倒 UNKNOWN**：已知的阻塞比未知更确定）
    decidability = "BLOCKED";
    label = "不可决定";
  } else if (unknownChecks.length > 0) {
    // 没有已知阻塞，但有关键输入缺失 ⇒ UNKNOWN，**不得当成可以**
    decidability = "UNKNOWN";
    label = "待核实";
  } else {
    decidability = "DECIDABLE";
    label = "可决定";
  }

  return {
    decidability,
    label,
    checks,
    blockReasons,
    unknownChecks,
    requiresData: unknownChecks.map((name) => CHECK_REQUIREMENTS[name] ?? "需要外部数据"),
  };
}

/** 每个"缺数据"的检查需要什么才能完成（给界面上的说明用）。 */
const CHECK_REQUIREMENTS = Object.freeze({
  config_authority_separated: "角色/岗位数据（谁是配置管理员、谁是业务批准人）",
  within_limit: "按风险等级的授权限额",
  position_authorized: "岗位与授权矩阵",
  validity: "记录里的 valid_until 与一个可信的当前时刻",
  approver_is_not_requester: "审批人与发起人标识",
});

/**
 * 汇总一批批准（列表页用）。
 *
 * 与状态契约的处理一致：**计数与逐行同源**，且"待核实"单独计数——
 * 绝不能把 `UNKNOWN` 并进"可决定"，那会让未管控的部分看起来已被管控。
 *
 * @param {object[]} approvals
 * @param {string|null} [decidedAt]
 */
export function summarizeApprovals(approvals = [], decidedAt = null) {
  const byDecidability = {};
  for (const approval of approvals) {
    const { decidability } = assessApproval({ approval, decidedAt });
    byDecidability[decidability] = (byDecidability[decidability] ?? 0) + 1;
  }
  return { total: approvals.length, byDecidability };
}

/**
 * 可批准性结论的展示文案（口径必须写清楚）。
 */
export function decidabilityLabels(summary) {
  const counts = summary?.byDecidability ?? {};
  return [
    {
      key: "DECIDABLE",
      title: "可决定",
      count: counts.DECIDABLE ?? 0,
      note: "已知检查全部通过",
    },
    {
      key: "BLOCKED",
      title: "不可决定",
      count: counts.BLOCKED ?? 0,
      note: "存在明确阻塞理由",
    },
    {
      key: "UNKNOWN",
      title: "待核实",
      count: counts.UNKNOWN ?? 0,
      note: "**缺角色/限额数据，不得当作可批准**",
    },
  ];
}
