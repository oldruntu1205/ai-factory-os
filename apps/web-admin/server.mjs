/**
 * V3 管理后台的本地服务（**零依赖**，只用 Node 内置模块）。
 *
 * 它做两件事：
 *
 * 1. **托管 UI**：把 `public/` 下的静态文件发给浏览器；
 * 2. **同源代理后端**：把 `/api/v3/*` 转发到真实的 V3 服务，
 *    这样浏览器不必跨域，也就**不需要** CORS 配置、
 *    更不需要把身份或密钥写进前端。
 *
 * ## 为什么不直接让浏览器打后端
 *
 * 后端在 8102 等端口，UI 在另一个端口 ⇒ 跨域。要么给后端加 CORS
 * （等于为调试方便放宽生产边界），要么让前端直连并携带身份——
 * 后者会把"服务身份"暴露在浏览器里。同源代理两个问题都没有。
 *
 * ## 为什么不用框架
 *
 * 本机 npm registry 连通性未确认（见 `package.json` 的 `//` 字段），
 * 而"装不上依赖的工程"比"零依赖的可运行服务"更糟：前者会被当成已完成。
 * 渲染逻辑仍走 `src/status.js` / `src/view.js` 那套**已被 44 条用例钉住**的纯函数。
 *
 * 用法：
 *   node apps/web-admin/server.mjs                     # 默认 8300
 *   V3_ADMIN_PORT=8400 V3_CONTROL_PLANE_PORT=8102 node apps/web-admin/server.mjs
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { deriveDisplayStatus } from "./src/status.js";
import { assessApproval } from "./src/approval.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(HERE, "public");

/** 仓库根（`apps/web-admin/` 的上两级）——用于读演示数据等仓库内文件。 */
const REPO_ROOT = path.join(HERE, "..", "..");

const ADMIN_PORT = Number(process.env.V3_ADMIN_PORT ?? 8300);

/** 各后端单元端口（与代码里的默认端口一致，可用环境变量覆盖）。 */
const BACKENDS = {
  control: Number(process.env.V3_PORT_CONTROL_PLANE ?? 8102),
  transaction: Number(process.env.V3_PORT_TRANSACTION_SERVICE ?? 8107),
  manufacturing: Number(process.env.V3_PORT_MANUFACTURING_CORE ?? 8105),
  // B4（用户侧写路径）需要**规范化由可信服务完成**（上游 §6.2：
  // 「实际命令的规范化由可信服务完成，哈希不能只相信 Agent 提供值」），
  // 而规范化端点在 tool-gateway（8106）。所以这里必须登记它。
  tool: Number(process.env.V3_PORT_TOOL_GATEWAY ?? 8106),
};

/**
 * **服务身份**只存在于这里（服务端），不进浏览器。
 *
 * 两个头职责不同，实测踩过：
 * - `Authorization: Bearer …` 才是**身份凭证**（`_has_identity` 只认它）；
 * - `X-V3-Identity` 是"这个身份代表谁"的**租户归属**（`_identity_tenant`）。
 *
 * 最初我只发了后者 ⇒ 每次都 401「缺少或无效的服务身份凭证」。
 * 这是同源代理的额外价值：凭证留在服务端，浏览器页面里既没有 token 也没有内部地址。
 */
const SERVICE_IDENTITY = process.env.V3_ADMIN_IDENTITY ?? "svc-hermes-runtime";
const SERVICE_TOKEN = process.env.V3_ADMIN_TOKEN ?? "p0-test";

/**
 * 受控派发要求的调用方身份（后端常量 `CONTROLLED_EXECUTION_CALLER`）。
 *
 * 工具侧的受控执行入口**只认**这一个身份，目的正是"runtime 或外部不得直接派发"。
 *
 * BFF 临时换身份去调它**不是提权**：凭证不变，只是"代表谁"换成事务身份；
 * 那个端点自己的核对（执行凭证、租约、fencing、对象锁）一项没少。
 * 反过来，若把这一步放到浏览器里做，才是把"谁有权派发"交给了客户端。
 */
const CONTROLLED_EXECUTION_CALLER = "svc-transaction-service";

const CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

/** 演示用固定上下文（真实部署应由 SSO 提供，见上游 §16.1 外部依赖）。 */
function demoContext(traceId) {
  return {
    tenant_id: "tenant-demo",
    factory_id: "factory-demo",
    actor_id: "user-demo",
    employee_id: "employee-planning",
    task_id: null,
    trace_id: traceId,
    data_class: "C2",
    schema_version: "1.0.0",
    deadline_at: "2026-12-31T00:00:00Z",
  };
}

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
}

/** 转发一个 GET 到后端单元，返回 {status, body}。 */
async function callBackend(service, pathname, context, { method = "GET", body = null, identityOverride = null } = {}) {
  const port = BACKENDS[service];
  if (!port) {
    throw new Error(`未知后端单元：${service}`);
  }
  // **两个头缺一不可**（见 SERVICE_IDENTITY 处的说明）：
  // - Authorization 是身份凭证；缺它一律 401。
  // - X-V3-Identity 是租户归属；缺它无法确定"代表哪个租户"。
  // 头名大小写与契约一致（实测小写会被判成"缺少凭证"）。
  //
  // `identityOverride` 只用于**受控派发**：那个端点只接受 transaction-service 身份。
  // 凭证（Authorization）不变，只换"代表谁"——这不是绕过鉴权，见常量处的说明。
  const headers = {
    Authorization: `Bearer ${SERVICE_TOKEN}`,
    "X-V3-Identity": identityOverride ?? SERVICE_IDENTITY,
  };
  const init = { method, headers };
  if (method === "GET") {
    headers["X-Context"] = JSON.stringify(context);
  } else {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify({ ...context, context, ...(body ?? {}) });
  }
  const response = await fetch(`http://127.0.0.1:${port}${pathname}`, init);
  const text = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = { raw: text };
  }
  return { status: response.status, body: parsed };
}

/**
 * 取一条任务的完整视图：任务投影 + 命令 + 权威回执，**分别标注来源**。
 *
 * 三者的来源不同（control-plane / transaction-service），这正是
 * "结论必须带状态来源"的由来——把它们混成一个字段就说不清谁说的。
 */
/**
 * 统一的上下文来源。
 *
 * ## 为什么要有这个函数（而不是各处直接调 `demoContext`）
 *
 * 两类读者需要**不同**的上下文：
 * - **管理台**（`/api/v3/*`）：运维查看，用固定演示身份即可；
 * - **用户侧**（`/v3-user/*`）：必须用**登录用户所属公司**解析出的身份。
 *
 * 原先 9 处都硬调 `demoContext`，用户侧就无法带上真实身份。
 * 现改为：**传了 `identityContext` 就用它，没传才退回演示身份**
 * —— 于是管理台行为**一字不变**，用户侧能各自带身份。
 */
function contextFor(identityContext, traceId, extra = {}) {
  return { ...(identityContext ?? demoContext(traceId)), ...extra };
}

async function resolveTask(taskId, identityContext = null) {
  const traceId = `trace-admin-${Date.now().toString(36)}`;
  const context = contextFor(identityContext, traceId, { task_id: taskId });

  const task = await callBackend("control", `/api/v1/tasks/${encodeURIComponent(taskId)}`, context);

  // 任务视图里的 `command_refs` 是**字符串数组**（内核在组装视图时按任务从
  // 命令账本派生，已按时间排序）。这里取**最早那条**去问执行侧——
  // 一次业务动作可能有多条命令（重试/补偿），所以返回值里把总数一并给出，
  // 界面要能说清"这里只显示了第一条"，不能让人以为只有一条。
  const commandRefs =
    task.status === 200 && Array.isArray(task.body?.command_refs)
      ? task.body.command_refs.map((ref) => (typeof ref === "string" ? ref : ref?.command_id)).filter(Boolean)
      : [];
  const commandId = commandRefs.length ? commandRefs[0] : null;

  let command = null;
  let executionRecord = null;
  if (commandId) {
    command = await callBackend(
      "transaction",
      `/internal/v1/commands/${encodeURIComponent(commandId)}`,
      context,
    );
    const record = await callBackend(
      "transaction",
      `/internal/v1/commands/${encodeURIComponent(commandId)}/execution-record`,
      context,
    );
    executionRecord = record.status === 200 ? record.body : null;
  }

  // **权威回执**：只有它能使"已过账"成立（上游 §6 / A14）。
  //
  // ⚠️ 键名是 `authority_receipt`，**不是** `receipt`（2026-10-06 修的真 bug）。
  //
  // 这里曾写成 `executionRecord?.receipt ?? command?.body?.receipt`，
  // 而 P3 执行记录里那个字段叫 `authority_receipt` ⇒ **回执明明在，这里永远取到 null**
  // ⇒ 界面永远显示「未过账」。实测：命令已 `SUCCEEDED`、`posting_status=POSTED`、
  // 回执 `authority_verified=true`，而任务结论页仍报「未过账」。
  //
  // 这一处特别值得记：它**不会报错**，只会让最关键的结论恒定错误 ——
  // 而"只有权威回执能说已过账"正是本项目的第一条显示契约。
  //
  // 刻意**不留裸 `receipt` 的回退**：回退会让"确实没有回执"与"回执在别的键上"
  // 变成同一个结果，正好掩盖这类缺陷。读不到就该是读不到。
  // （`apps/web-admin/test/execution-receipt.test.mjs` 守着这一条。）
  const receipt = executionRecord?.authority_receipt ?? command?.body?.authority_receipt ?? null;

  const display = deriveDisplayStatus({
    taskStatus: task.status === 200 ? task.body?.status ?? null : null,
    commandStatus: command?.status === 200 ? command.body?.status ?? null : null,
    receipt,
    taskUpdatedAt: task.status === 200 ? task.body?.updated_at ?? null : null,
    taskStatusSource: task.status === 200 ? task.body?.status_source ?? null : null,
    commandUpdatedAt: command?.status === 200 ? command.body?.updated_at ?? null : null,
    commandStatusSource: command?.status === 200 ? command.body?.status_source ?? null : null,
  });

  /*
   * **查不到任务时不得沿用"处理中"。**
   *
   * 这是我自己在联调里抓到的一处编造：`deriveDisplayStatus` 对"没有任何状态输入"
   * 的默认答案是「未过账 / 处理中」——对"查不到这条任务"来说这是**错的**，
   * 而且错得危险：看的人会以为"任务在跑"，实际是**这条任务不存在**。
   *
   * 所以有明确失败原因时，结论必须改成"无法判定 + 说清原因"。
   * 宁可显示"查不到"，也不要给一个偶发正确的猜测。
   */
  if (task.status !== 200) {
    display.outcome = "UNKNOWN";
    display.label = "无法判定";
    display.reason =
      task.status === 404
        ? `任务 ${taskId} 不存在（或不属于当前租户）`
        : `任务读取失败（HTTP ${task.status}：${task.body?.message ?? "未知原因"}）`;
    display.authoritative = false;
  }

  return {
    taskId,
    task: task.status === 200 ? task.body : null,
    taskError: task.status === 200 ? null : { status: task.status, ...task.body },
    commandId,
    // 该任务名下的**全部**命令（界面要能说清"只显示了第一条"）
    commandIds: commandRefs,
    commandCount: commandRefs.length,
    command: command?.status === 200 ? command.body : null,
    commandError: command && command.status !== 200 ? { status: command.status, ...command.body } : null,
    receipt,
    display,
  };
}

/** 取一条批准的完整视图 + 可批准性判定。 */
async function resolveApproval(approvalId, identityContext = null) {
  const traceId = `trace-admin-ap-${Date.now().toString(36)}`;
  const context = contextFor(identityContext, traceId);

  const fact = await callBackend(
    "control",
    `/api/v1/approvals/${encodeURIComponent(approvalId)}/fact`,
    context,
  );

  // 判定**必须传入"现在"**——用本机时钟会让结论不可复现（见 approval.js 的设计说明）。
  const now = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const assessment =
    fact.status === 200
      ? assessApproval({ approval: fact.body, decidedAt: now })
      : assessApproval({ approval: {}, decidedAt: now });

  return {
    approvalId,
    fact: fact.status === 200 ? fact.body : null,
    factError: fact.status === 200 ? null : { status: fact.status, ...fact.body },
    assessment,
    assessedAt: now,
  };
}

/**
 * 按 command_id 取命令的**权威事实**（业务成功 / 过账状态 / 权威回执）。
 *
 * ## 哪个接口才是权威源（联调实测纠正过一次）
 *
 * 两个接口返回**两种形状**，各有各的契约，不能混：
 *
 * | 接口 | 契约 | 有没有 `posting_status` / `authority_receipt` |
 * | --- | --- | --- |
 * | `GET /internal/v1/commands/{id}` | `command-status.json`（**16 字段，additionalProperties: false**） | **没有** |
 * | `GET /internal/v1/commands/{id}/execution-record` | `p3-command.json#/$defs/View` | **有** |
 *
 * 我一开始读了前者、并试图把后者字段塞进去，结果被
 * `test_responses_conform_to_contract_schemas` 当场抓住（契约违规）。
 * 正确做法是**读对接口**，而不是改宽契约。
 *
 * 顺带修掉一个真实 500：前者对 P3 路径创建的命令记录硬取 `record["source_system"]`，
 * 直接 `KeyError` 掐断连接——现在改成 `.get()` 了。
 */
/**
 * 缓存一条**真实存在**的任务 id，供"工厂只读链路"的非空校验使用。
 *
 * ## 为什么不能硬编码（一次真实缺陷，2026-10-05）
 *
 * 这里原本写死 `task_id: "task-demo"`。它**不是真实任务**——
 * 只是恰好非空，能绕过 `_p3_get_context` 的「工厂只读链路要求 task_id 非空」。
 *
 * 后果很隐蔽：演示环境每次重启都会产生**新的** task_id。
 * 于是读取接口仍会返回 400（后端按 task_id 归属做校验），
 * 而界面只看到"读不到"，看不出是因为**上下文里那个任务根本不存在**。
 *
 * 正确做法是**解析一个真实任务**；解析不到就如实报错，
 * 而不是塞一个看着合理的假值让请求"形式上通过"。
 */
let cachedTaskId = null;

async function resolveTaskId(identityContext = null) {
  // 演示缓存只对**管理台**（演示身份）有效：用户侧各公司各有自己的任务，
  // 共用一份缓存会让 A 公司拿到 B 公司的 task_id。
  if (!identityContext && cachedTaskId) return cachedTaskId;
  const traceId = `trace-admin-task-${Date.now().toString(36)}`;
  const context = contextFor(identityContext, traceId);

  // 先看演示数据：`tools/v3_dev_cluster.py demo` 会把真实产生的 id 写在这里
  // （同样只对管理台的演示身份适用）
  if (!identityContext) {
    try {
      const demo = JSON.parse(
        fs.readFileSync(path.join(REPO_ROOT, ".v3-dev", "demo.json"), "utf-8"),
      );
      if (demo?.task_id) {
        cachedTaskId = demo.task_id;
        return cachedTaskId;
      }
    } catch {
      // 文件不存在/不合法：继续往下找
    }
  }

  // 建一条真实任务作为上下文（演示环境专用；失败则如实返回 null）
  const created = await callBackend("control", "/api/v1/tasks", context, {
    method: "POST",
    body: {
      ...context,
      context,
      idempotency_key:
        (identityContext ? "user" : "admin") + `-context-${Date.now().toString(36)}`,
      input_ref: "schedule-input-demo",
      input_schema: { type: "object" },
    },
  });
  if (created.status === 202 && created.body?.task_id) {
    // **只在管理台（演示身份）缓存**：用户侧各公司各有自己的任务，
    // 缓存会让"下一个请求换了个公司"时拿到上一个公司的 task_id。
    if (!identityContext) cachedTaskId = created.body.task_id;
    return created.body.task_id;
  }
  return null;
}

async function resolveCommand(commandId) {
  const traceId = `trace-admin-cmd-${Date.now().toString(36)}`;
  // **P1 Context 要求 `task_id` 非空**（`_p3_get_context`：工厂只读链路）。
  // 用**真实任务**，不用假值 —— 见 `resolveTaskId` 的说明。
  const taskId = await resolveTaskId();
  if (!taskId) {
    return {
      commandId,
      error: {
        status: 409,
        code: "NO_TASK_CONTEXT",
        message:
          "拿不到真实任务作为上下文（工厂只读链路要求 task_id 非空）。"
          + "请先运行 tools/v3_dev_cluster.py demo，或提交一次任务。",
      },
    };
  }
  const context = { ...demoContext(traceId), task_id: taskId };

  const record = await callBackend(
    "transaction",
    `/internal/v1/commands/${encodeURIComponent(commandId)}/execution-record`,
    context,
  );
  const recordBody = record.status === 200 ? record.body : null;

  // 权威回执：`View` 里直接带
  const receipt = recordBody?.authority_receipt ?? null;

  const display = deriveDisplayStatus({
    commandStatus: recordBody?.status ?? null,
    receipt,
    commandUpdatedAt: recordBody?.updated_at ?? null,
    commandStatusSource: recordBody?.status_source ?? null,
  });

  if (record.status !== 200) {
    display.outcome = "UNKNOWN";
    display.label = "无法判定";
    display.reason =
      record.status === 404
        ? `命令 ${commandId} 不存在`
        : `命令读取失败（HTTP ${record.status}：${record.body?.message ?? "未知原因"}）`;
    display.authoritative = false;
  }

  return {
    commandId,
    // 字段名沿用 `View` 契约：`business_success` / `posting_status` 都是**服务端算好的**，
    // 界面只负责显示，不自己判断。
    command: recordBody,
    commandError: record.status === 200 ? null : { status: record.status, ...record.body },
    receipt,
    businessSuccess: recordBody?.business_success ?? null,
    postingStatus: recordBody?.posting_status ?? null,
    postingEvidenceRef: recordBody?.posting_evidence_ref ?? null,
    display,
  };
}

/**
 * 批准决定人身份 —— **默认不配置**（写路径默认关闭）。
 *
 * ## 为什么不复用 `SERVICE_IDENTITY`
 *
 * 那个身份是 `svc-hermes-runtime`（**服务**身份）。用它去写批准等于
 * "服务替人做决定"，而本项目已把这条判死：
 * 授权只能来自**明确的、结构化的**决定，且必须记名（上游 §6 / §7）。
 *
 * ## 为什么默认关闭、必须显式配置
 *
 * 后端要求决定人是一个**真正有权限的 actor**。后台如果擅自换成
 * `user-approver` 之类的身份去写，那就是**静默越权**——
 * 界面上看不出"是谁批的"，审计上也无从追溯。
 *
 * 所以：**不配置就拒绝写**，并明确告诉使用者怎么开、开了之后是谁在批。
 * 这比"悄悄用某个身份让它能跑"要诚实得多。
 */
const APPROVER_ACTOR = process.env.V3_ADMIN_APPROVER_ACTOR ?? null;
const APPROVER_TENANT = process.env.V3_ADMIN_APPROVER_TENANT ?? "tenant-demo";

const APPROVAL_DECISION_PATH = (approvalId) =>
  `/api/v1/approvals/${encodeURIComponent(approvalId)}/decisions`;

/**
 * 提交一次批准决定。
 *
 * ## 三条硬约束（都由这里把住）
 *
 * 1. **未配置决定人 ⇒ 拒绝**（不静默借用别的身份）；
 * 2. **决定必须是结构化的**：`approved` 真布尔；
 *    `comment` 里出现「批准/同意/通过」一律拒绝——上游明文
 *    「不把自然语言"批准"当作授权」。这条判定复用
 *    `src/approval.js` 的 `normalizeDecision`（有 23 条用例钉住），
 *    而不是在服务端重写一份。
 * 3. **绑定必须与平台已记录的那份一致**：见下。
 *
 * ## 关于绑定：我**无法**替调用方读出它（实测事实）
 *
 * - `GET /api/v1/approvals/{id}/fact` 对**未决定**的批准返回
 *   `409 APPROVAL_NOT_DECIDED`（"该批准已建立绑定但尚未作出决定"），
 *   **响应里不含绑定**；
 * - 全仓**没有**"读取待决批准绑定"的 GET 接口；
 * - `approvals` 表里只有**已决定**的批准。
 *
 * 所以绑定只能由**知情操作方**提供（谁发起批准谁就有绑定）。
 * 这不是漏洞：后端 `_handler_approval_decision` 会拿它与该批准
 * **已记录的 `binding_hash`** 比对，不符即 409 —— 前端改了字段也没用。
 * 这里做的只是**结构性预检**（字段齐不齐），真正的校验在后端。
 *
 * **未提供 ⇒ 明确拒绝**，而不是猜一份。假装能读回来才是危险的。
 */
async function submitApprovalDecision(approvalId, form) {
  if (!APPROVER_ACTOR) {
    return {
      ok: false,
      status: 403,
      code: "APPROVER_NOT_CONFIGURED",
      reason:
        "写路径未启用：未配置决定人。授权必须记名，" +
        "后台不得自行借用某个身份替人做决定。",
      how: "启动时设置 V3_ADMIN_APPROVER_ACTOR=<真实 actor_id>（本地演示可用 user-approver）",
    };
  }

  // 复用界面判定的同一份口径（服务端与前端不得各写一套）
  const { normalizeDecision } = await import("./src/approval.js");
  const decision = normalizeDecision(form);
  if (!decision.ok) {
    return { ok: false, status: 400, code: decision.code, reason: decision.reason };
  }

  const binding = form.binding;
  if (!binding || typeof binding !== "object") {
    return {
      ok: false,
      status: 400,
      code: "BINDING_REQUIRED",
      reason:
        "缺少绑定。平台没有「读取待决批准绑定」的接口" +
        "（/fact 对未决定的批准返回 409 且不含绑定），" +
        "所以绑定必须由知情操作方提供——它会被后端与该批准已记录的绑定比对。",
      required: ["object_id", "tool_id", "tool_version", "payload_hash", "policy_version"],
    };
  }
  const required = ["object_id", "tool_id", "tool_version", "payload_hash", "policy_version"];
  const missing = required.filter((key) => !binding[key]);
  if (missing.length) {
    return {
      ok: false,
      status: 400,
      code: "BINDING_INCOMPLETE",
      reason: `绑定缺少字段，不能凭残缺绑定做决定：${missing}`,
    };
  }

  const traceId = `trace-admin-approve-${Date.now().toString(36)}`;
  // **决定人决定 context.actor_id**：绑定里的 context 会被后端用来构造批准事实。
  const context = {
    ...demoContext(traceId),
    actor_id: APPROVER_ACTOR,
    tenant_id: APPROVER_TENANT,
  };

  const body = {
    ...context,
    context,
    approval_id: approvalId,
    approved: decision.approved,
    object_id: binding.object_id,
    expected_version: binding.expected_version ?? 0,
    tool_id: binding.tool_id,
    tool_version: binding.tool_version,
    command_type: binding.command_type ?? binding.tool_id,
    action_scope: binding.action_scope ?? [],
    payload_ref: binding.payload_ref ?? binding.object_id,
    payload_hash: binding.payload_hash,
    payload_schema: binding.payload_schema,
    normalization_ref: binding.normalization_ref,
    policy_version: binding.policy_version,
    valid_until: "2099-01-01T00:00:00Z",
  };

  const result = await callBackend("control", APPROVAL_DECISION_PATH(approvalId), context, {
    method: "POST",
    body,
  });
  return {
    ok: result.status === 200,
    status: result.status,
    // **必须回报实际决定人**：界面要能显示"是谁批的"
    actorId: APPROVER_ACTOR,
    tenantId: APPROVER_TENANT,
    approved: result.status === 200 ? result.body?.approved ?? null : null,
    code: result.body?.code ?? null,
    // 后端拒绝时原样透出它的可定位错误码与说明
    reason: result.status === 200 ? "决定已记录" : result.body?.message ?? "决定被拒绝",
    detail: result.status === 200 ? null : result.body,
  };
}

/**
 * 边缘发布的 7 个状态（上游 §12）。
 *
 * **与 `packages/domain/release.py` 必须一致**——但这里只用于**渲染时间轴**
 * 与**给用户列出合法下一步**；真正的把关在后端
 * （`edge.release_transition_allowed`）。前端算错只会少给一个按钮，
 * 不会让非法转换通过。
 */
const RELEASE_STATES = ["DRAFT", "SCHEMA_FROZEN", "CALIBRATED", "SHADOW", "APPROVED", "ACTIVE", "REVOKED"];
const RELEASE_TERMINAL = "REVOKED";

/** 读一条发布台账（控制台只读视图）。 */
async function resolveRelease(releaseId, identityContext = null) {
  const traceId = `trace-admin-release-${Date.now().toString(36)}`;
  // 与命令/批准同理：工厂只读链路要求 `task_id` 非空，用**真实任务**而非假值。
  const taskId = await resolveTaskId(identityContext);
  if (!taskId) {
    return {
      releaseId,
      error: {
        status: 409,
        code: "NO_TASK_CONTEXT",
        message: "拿不到真实任务作为上下文（工厂只读链路要求 task_id 非空）。",
      },
    };
  }
  const context = { ...demoContext(traceId), task_id: taskId };
  const result = await callBackend(
    "control",
    `/api/v1/edge/releases/${encodeURIComponent(releaseId)}`,
    context,
  );
  if (result.status !== 200) {
    return { releaseId, error: { status: result.status, ...(result.body ?? {}) } };
  }
  const record = result.body ?? {};
  return {
    releaseId,
    release: record,
    // 时间轴：标出当前处于哪一格
    timeline: RELEASE_STATES.map((state) => ({
      state,
      current: state === record.status,
      passed: RELEASE_STATES.indexOf(state) < RELEASE_STATES.indexOf(record.status ?? ""),
    })),
    isTerminal: record.status === RELEASE_TERMINAL,
    // **只列出合法下一步**：相邻一格前进；非终态可随时撤销
    legalNext: legalNextStates(record.status),
  };
}

/**
 * 从当前状态算出**合法**的下一步（供界面只显示合法按钮）。
 *
 * 与后端 `packages/domain/release.py::release_transition_allowed` 同规则：
 * 相邻前进一格；非终态可**直接撤销**到 `REVOKED`（撤销是"停止使用"，
 * 不该要求先推到某一步）；终态不能再出发。
 *
 * 注意：**这只影响界面给不给按钮，不是安全边界**——
 * 后端会独立校验（见 `submitReleaseTransition` 的说明）。
 */
function legalNextStates(current) {
  if (!current || current === RELEASE_TERMINAL) return [];
  const index = RELEASE_STATES.indexOf(current);
  if (index < 0) return [];
  const next = [];
  const forward = index + 1 < RELEASE_STATES.length ? RELEASE_STATES[index + 1] : null;
  // **前进一格恰好就是终态时，不要再追加一次**（2026-10-05 修）
  //
  // `ACTIVE` 的下一个状态本来就是 `REVOKED`。原实现无条件
  // `next.push(RELEASE_TERMINAL)`，于是 `ACTIVE` 得到
  // `["REVOKED", "REVOKED"]` ⇒ 界面上出现**两个"撤销"按钮**。
  //
  // 这个 bug 是**跨语言一致性检查**发现的：后端
  // `release_transition_allowed` 给出 `ACTIVE -> [REVOKED]`（一项），
  // 前端给出两项 ⇒ 两份实现漂移。
  if (forward) next.push(forward);
  if (!next.includes(RELEASE_TERMINAL)) next.push(RELEASE_TERMINAL);
  return next;
}

/**
 * 推进发布台账一格（CAS 相邻转换）。
 *
 * ## 决定"能推到哪"的是**后端**，不是这里
 *
 * 界面只把 `legalNextStates` 算出来的按钮给人点；但请求体里的 `to` 是
 * **客户端可改的**。所以这里做两件事：
 *
 * 1. **本地预检**：`to` 必须在本端算出的合法集合里（挡住手改请求的**明显**跳态）；
 * 2. **仍然把决定权交给后端**：`from` 取**服务端刚读到的** `status`，
 *    而不是请求体里传来的——否则前端可以声称"我在 DRAFT"而实际在 SHADOW。
 *
 * 即便本地预检被绕过（例如界面与后端状态不一致），后端
 * `edge.release_transition_allowed` 仍会拒绝跳态与终态出发，
 * 并回 `EDGE_RELEASE_TRANSITION_NOT_ALLOWED`。**安全边界在后端。**
 */
async function submitReleaseTransition(releaseId, target, form = {}, identityContext = null) {
  const traceId = `trace-admin-release-${Date.now().toString(36)}`;
  const taskId = await resolveTaskId(identityContext);
  if (!taskId) {
    return {
      ok: false,
      status: 409,
      code: "NO_TASK_CONTEXT",
      reason: "拿不到真实任务作为上下文（工厂只读链路要求 task_id 非空）。",
    };
  }
  const context = { ...demoContext(traceId), task_id: taskId };

  // `from` 必须取**服务端当前状态**，不采信客户端
  const current = await callBackend(
    "control",
    `/api/v1/edge/releases/${encodeURIComponent(releaseId)}`,
    context,
  );
  if (current.status !== 200) {
    return {
      ok: false,
      status: current.status,
      code: current.body?.code ?? "RELEASE_UNREADABLE",
      reason: `读不到发布台账，无法安全构造转换：${current.body?.message ?? ""}`,
    };
  }
  // `from` 必须取**服务端当前状态**，不采信客户端。
  //
  // ## 这条是刻意的设计，不是巧合（一个变异教训，2026-10-05）
  //
  // 我曾为它加过一条变异（"改成采信 `form.from`"），结果**逃逸** —— 因为
  // 本函数**从不读取请求体的 `from`**，那个改动在观测上不可区分（惰性）。
  //
  // 换句话说：这里的安全性来自**路由不接受 `from` 字段**，
  // 而不是来自某行判据。要让"采信客户端"变成可注入的缺陷，得先改路由 ——
  // 那本身容易被评审看出来。该变异因此被删除（它测不出东西）。
  //
  // 真正的安全边界仍然后端：`edge.release_transition_allowed` 独立校验
  // `current -> target` 是否相邻，与这里的值无关。
  const from = current.body?.status;
  const version = current.body?.record_version;
  const legal = legalNextStates(from);
  if (!legal.includes(target)) {
    return {
      ok: false,
      status: 409,
      code: "EDGE_RELEASE_TRANSITION_NOT_ALLOWED",
      reason: `只能按七态顺序相邻推进，或从非终态直接撤销；REVOKED 是终态。当前 ${from}，合法下一步：${legal.join(" / ") || "无"}`,
    };
  }

  const body = {
    ...context,
    context,
    release_id: releaseId,
    from,
    to: target,
    expected_version: version,
    gate_evidence: form.gateEvidence ?? [],
    evidence_refs: form.evidenceRefs ?? [],
    // 只有走到 APPROVED 才可能带批准引用；其余状态后端会清空
    approval_ref: target === "APPROVED" ? form.approvalRef ?? null : null,
  };

  const result = await callBackend(
    "control",
    `/internal/v1/edge/releases/${encodeURIComponent(releaseId)}/transitions`,
    context,
    { method: "POST", body },
  );
  return {
    ok: result.status === 200,
    status: result.status,
    from,
    to: target,
    code: result.body?.code ?? null,
    reason: result.status === 200 ? `已推进 ${from} → ${target}` : result.body?.message ?? "转换被拒绝",
    detail: result.status === 200 ? null : result.body,
  };
}

/**
 * 读一条命令的 **UNKNOWN 收敛记录**（对账）。
 *
 * ## 这一页为什么必要
 *
 * 上游把 UNKNOWN 定为**必须查状态**的状态（不能换键重发、不能直接补偿）。
 * 界面上「UNKNOWN = 结果未知，需对账」只是一个标签；**对账要给人看的是**：
 * 查了没有、源系统怎么说、允许做什么、禁止做什么。
 *
 * 没有这一页，运维看到 UNKNOWN 只能翻日志或直接打接口——
 * 而"禁止动作"恰恰最需要被看见（它是防误操作的清单）。
 *
 * ## 404 是**有意义的结果**
 *
 * 命令没有 `reconciliation_ref` 时后端返回 404 `RECONCILIATION_NOT_FOUND`。
 * 那表示"该命令尚未进入 UNKNOWN 收敛"，**不是错误**——
 * 所以如实呈现为一种状态，而不是当成失败弹红。
 */
async function resolveReconciliation(commandId, identityContext = null) {
  const traceId = `trace-admin-recon-${Date.now().toString(36)}`;
  const taskId = await resolveTaskId(identityContext);
  if (!taskId) {
    return {
      commandId,
      error: {
        status: 409,
        code: "NO_TASK_CONTEXT",
        message: "拿不到真实任务作为上下文（工厂只读链路要求 task_id 非空）。",
      },
    };
  }
  const context = { ...demoContext(traceId), task_id: taskId };

  const result = await callBackend(
    "transaction",
    `/internal/v1/commands/${encodeURIComponent(commandId)}/reconciliation`,
    context,
  );

  if (result.status === 404) {
    return {
      commandId,
      // **不是错误**：该命令没有收敛记录
      noRecord: true,
      code: result.body?.code ?? null,
      message: result.body?.message ?? null,
    };
  }
  if (result.status !== 200) {
    return { commandId, error: { status: result.status, ...(result.body ?? {}) } };
  }

  const view = result.body ?? {};
  // 把"禁止动作"与"候选动作"分离出来，界面要**先**展示禁止项
  return {
    commandId,
    record: view,
    sourceOutcome: view.source_outcome ?? null,
    nextAction: view.next_action ?? null,
    candidates: Array.isArray(view.candidate_actions) ? view.candidate_actions : [],
    prohibited: Array.isArray(view.prohibited_actions) ? view.prohibited_actions : [],
    // 重试候选必须用**同一幂等键**（换键重发被上游明确禁止）
    retryKey: view.retry_idempotency_key ?? null,
    retryNeedsFreshReview: view.retry_requires_fresh_review ?? null,
    queryVerified: view.query_verified ?? null,
    queryAttempts: view.query_attempt_count ?? null,
  };
}

/**
 * 演示身份的**兜底值**：只在 `resolveIdentity` 拿不到真实身份时用于
 * **管理台**（`/api/v3/*`）与 `/v3-user/health` 的展示。
 *
 * ⚠️ **用户侧（`/v3-user/*` 的数据端点）不使用它**——那里必须走
 * `resolveIdentity`，未登录即 401、公司未映射即 403。
 * 这条区分很要紧：兜底身份用在数据端点上，就等于**让未登录者看到演示租户的数据**。
 */
const USER_FACING_IDENTITY = Object.freeze({
  tenant_id: "tenant-demo",
  factory_id: "factory-demo",
  employee_id: "employee-planning",
  label: "演示租户 / 演示工厂 / 计划员工（兜底，非真实登录身份）",
});

/**
 * 用户侧**建任务**（浏览器面 `POST /api/v1/tasks`，受理即返回 202）。
 *
 * `idempotency_key` 由**服务端**生成：让页面自己造键，等于把"同键不同 payload
 * 即冲突"这条保护交给最不了解语义的一层。
 */
/**
 * 用户侧**发起一次需要批准的业务写入**（上游 §6.2 的受控写入链前两步）。
 *
 * ## 这条链路为什么这么长

 * 上游 §6.2 把受控写入定成一条链：
 *
 *     规范化 → 策略判定 → 建立批准绑定 → 批准 → 持久化命令 → 执行 → 权威回执
 *
 * 本函数只做**前两步**（规范化 + 建立待批准绑定），因为：
 * ① **规范化必须由可信服务完成**——「实际命令的规范化由可信服务完成，
 *    哈希不能只相信 Agent 提供值」（§6.2）。所以这里**不接收**调用方给的
 *    payload 哈希，只把 `object_id`/`expected_version` 交给 Tool Gateway；
 * ② **批准是另一个人的事**（§7 职责分离），不能由发起人在同一次请求里完成。
 *    所以这里只**建立待批准**，把 `approval_id` 交给界面，由**审批人**去决定。
 *
 * ## 一个刻意的设计：BFF 不替用户决定风险等级
 *
 * 上游 §7：「风险级别由**可信工具元数据**与业务规则计算，**不由模型填写决定**」。
 * 所以这里的 `risk_class` 只是**转达工具目录里已有的值**给策略端点，
 * 真正的判定仍由控制面做（它也会核对工具元数据）。
 */
async function requestApproval(form = {}, identityContext = null) {
  const traceId = `trace-user-write-${Date.now().toString(36)}`;
  let context = contextFor(identityContext, traceId);
  const toolId = String(form.tool_id ?? "").trim() || "pmc.dispatch.publish";
  const objectId = String(form.object_id ?? "").trim();
  const expectedVersion = Number.parseInt(String(form.expected_version ?? "1"), 10);

  if (!objectId) {
    return { ok: false, status: 400, code: "MISSING_OBJECT_ID", message: "请填写对象 ID（object_id）。" };
  }
  if (!Number.isInteger(expectedVersion) || expectedVersion < 0) {
    return { ok: false, status: 400, code: "BAD_EXPECTED_VERSION", message: "期望版本必须是非负整数。" };
  }

  // ⓪ 规范化端点要求 `task_id` **非空**（工厂只读链路的硬要求，实测 400）。
  //    对用户来说"先建一条任务再发起写入"是没有意义的负担——
  //    **一次业务写入本来就该挂在一条任务下**，所以这里自动建。
  let taskId = String(form.task_id ?? "").trim();
  let taskCreated = false;
  if (!taskId) {
    const created = await callBackend("control", "/api/v1/tasks", context, {
      method: "POST",
      body: {
        idempotency_key: `user-write-task-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
        input_ref: String(form.input_ref ?? "").trim() || `write-${objectId}`,
        input_schema: { type: "object" },
      },
    });
    if (created.status !== 202 || !created.body?.task_id) {
      return {
        ok: false,
        status: created.status,
        code: created.body?.code ?? "TASK_CREATE_FAILED",
        message: created.body?.message ?? "为这次写入建立任务失败",
      };
    }
    taskId = created.body.task_id;
    taskCreated = true;
  }
  // 后续每一步都挂在这条任务上（命令与批准都会记住它，任务↔命令关联即由此成立）
  context = { ...context, task_id: taskId };

  // ① 规范化：**由 Tool Gateway 算出 binding 与 payload 哈希**，BFF 与用户都不碰哈希
  const normalized = await callBackend("tool", "/internal/v1/tool-requests/normalize", context, {
    method: "POST",
    body: {
      object_id: objectId,
      expected_version: expectedVersion,
      tool_id: toolId,
      tool_version: String(form.tool_version ?? "1.0.0-mock"),
      command_type: toolId,
      input_ref: String(form.input_ref ?? "").trim() || `write-${objectId}`,
      input_schema: { schema_ref: "command.json", schema_version: "1.0.0", schema_hash: "sha256:mock" },
    },
  });
  if (normalized.status !== 200 || !normalized.body?.normalization_ref) {
    return {
      ok: false,
      status: normalized.status,
      code: normalized.body?.code ?? "NORMALIZE_FAILED",
      message: normalized.body?.message ?? "规范化失败",
    };
  }
  const normalizationRef = normalized.body.normalization_ref;

  // ② 策略判定：拿到权威策略结论（决定是否需要批准、风险等级是什么）
  const riskClass = (normalized.body.binding ?? {}).risk_class ?? null;
  const policy = await callBackend("control", "/internal/v1/policy/evaluate", context, {
    method: "POST",
    body: {
      tool_id: toolId,
      tool_version: String(form.tool_version ?? "1.0.0-mock"),
      risk_class: riskClass,
      object_id: objectId,
      delegated_scopes: [],
    },
  });
  if (policy.status !== 200) {
    return {
      ok: false,
      status: policy.status,
      code: policy.body?.code ?? "POLICY_EVALUATION_FAILED",
      message: policy.body?.message ?? "策略判定失败",
    };
  }

  // ③ 建立**待批准**绑定（`approved` 仍为 null；决定权在审批人）
  const binding = await callBackend("control", "/internal/v1/approvals/bindings", context, {
    method: "POST",
    body: {
      normalization_ref: normalizationRef,
      policy_evaluation_ref: policy.body.evaluation_ref,
      idempotency_key: `user-write-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      valid_until: "2026-12-31T00:00:00Z",
    },
  });
  if (binding.status !== 200 || !binding.body?.approval_id) {
    return {
      ok: false,
      status: binding.status,
      code: binding.body?.code ?? "APPROVAL_BINDING_FAILED",
      message: binding.body?.message ?? "建立批准绑定失败",
    };
  }

  return {
    ok: true,
    approval_id: binding.body.approval_id,
    task_id: taskId,
    task_created: taskCreated,
    object_id: objectId,
    tool_id: toolId,
    risk_class: riskClass,
    // 策略说的**权威结论**照实转达，不加工
    allowed: policy.body.allowed,
    approval_required: policy.body.approval_required,
    reason_code: policy.body.reason_code,
    policy_version: policy.body.policy_version,
    payload_hash: binding.body.binding?.payload_hash ?? null,
    valid_until: binding.body.valid_until ?? null,
    // 批准时要逐项绑定的六项（界面从待批准清单里带过来，不让人凭印象填）
    binding: binding.body.binding ?? null,
    // 这次写入**能不能真的执行**——在发起时就问准入端点，别等批准完才发现走不通。
    // 高风险写入要求幂等/状态查询/补偿三项能力位齐备；缺一项，
    // 执行前复核就会以 `reason-capability-not-admitted` 拒绝（实测）。
    admission: await describeAdmission(toolId, context),
    note:
      "已建立待批准（尚未批准）。批准必须由另一个人作出——" +
      "发起人自己批准会被执行前复核对拒绝（DUTIES_NOT_SEPARATED，上游 §7 职责分离）。",
  };
}

/**
 * 查一个工具的**写入准入**结论（能力位是否齐备）。
 *
 * ## 为什么在"发起"阶段就要查
 *
 * 高风险写入（R2/R3）在执行前复核时要求三项能力位齐备，缺一项就拒。
 * 如果等到用户批准完才告诉他走不通，那是一次无效的审批——
 * **让人白批一张票，比不让他批更糟**。
 *
 * 所以发起时就把结论摆出来：这个操作现在能不能执行、缺什么。
 * 查不到就如实说"查不到"，**不猜**。
 */
async function describeAdmission(toolId, context) {
  const result = await callBackend(
    "tool",
    `/internal/v1/tools/${encodeURIComponent(toolId)}/write-admission`,
    context,
  );
  if (result.status !== 200 || !result.body) {
    return { known: false, note: `准入结论暂不可读（HTTP ${result.status}）——不代表可以执行。` };
  }
  const body = result.body;
  return {
    known: true,
    // 契约键名是 `unattended_high_risk_write_allowed`；低风险读写本就不受此限。
    unattended_high_risk_allowed: body.unattended_high_risk_write_allowed === true,
    risk_class: body.tool_spec?.risk_class ?? null,
    // 缺哪些能力位要说清——「不能执行」必须带原因
    missing_capabilities: body.tool_spec
      ? ["idempotency_support", "status_query", "compensation"].filter(
          (bit) => body.tool_spec[bit] !== true,
        )
      : [],
    requires_adapter_ledger: body.adapter_ledger_required === true,
    requires_object_lock: body.serial_object_lock_required === true,
    requires_manual_reconciliation: body.manual_reconciliation_required === true,
    reason_ref: body.reason_ref ?? null,
  };
}

/**
 * 用户侧**把已批准的命令推进到执行**（上游 §6.2 后半段）。
 *
 * ## 顺序不是随便定的
 *
 *     取租约 → 执行前复核（要 lease_ref）→ 执行（要 review_ref）→ 受控派发
 *
 * 两处依赖是**硬**的，顺序反了就报错（实测）：
 * ① 复核端点**要求** `lease_ref`/`fencing_token` ⇒ 必须先取租约；
 * ② 执行端点**要求** `review_ref` ⇒ 必须先复核。
 *
 * ## 为什么停在"派发"，不等回执
 *
 * 权威回执是**源系统**给的事实（ERP 出了什么事，由 ERP 说）。
 * 本 BFF **不会**替 ERP 造一条回执——那正是本项目第一条显示契约要防的事：
 * 「只有权威回执能说已过账」。所以这里做到"已派发"，把状态如实交出去，
 * 等真实回执到达（回执入口是 `POST /internal/v1/commands/{id}/receipts`）。
 *
 * 演示环境里没有真 ERP，用 `tools/v3_source_receipt.py` 作为**源系统替身**
 * 补一条回执（那条命令必须由有凭据的人执行，不经浏览器）。
 */
async function continueExecution(approvalId, identityContext = null) {
  const traceId = `trace-user-exec-${Date.now().toString(36)}`;
  let context = contextFor(identityContext, traceId);
  const id = String(approvalId ?? "").trim();
  if (!id) {
    return { ok: false, status: 400, code: "MISSING_APPROVAL_ID", message: "缺少批准单编号。" };
  }

  // ⓪ 先确保这条批准对应的执行单已提交（幂等；顺序由内核决定，见该函数说明）
  const ensured = await ensureCommandForApproval(id, null, identityContext);
  if (ensured.ok === false) return ensured;
  const commandId = ensured.command_id;
  if (!commandId) {
    return {
      ok: false,
      status: 500,
      code: "COMMAND_ID_MISSING",
      message: "内部错误：提交执行单后没有拿到编号。",
      debug: JSON.stringify(ensured).slice(0, 400),
    };
  }

  // ⚠️ **读命令之前就要把 task_id 带上**。
  //
  // 工厂只读链路要求 `task_id` 非空（实测 400 `REQUEST_INVALID`
  // object_ref=task_id）。而命令自己的 task_id 只有在读出来之后才知道——
  // 是个鸡生蛋问题。解法：`ensureCommandForApproval` **同时返回它建命令时用的
  // task_id**（那来自批准绑定的 context），这样第一次读就带得上。
  // 第一版把这一步放在了读取之后，于是永远读不到（实测 404 COMMAND_NOT_FOUND）。
  if (ensured.task_id) context = { ...context, task_id: ensured.task_id };

  // ① 读命令当前状态与记录版本（取租约要 CAS，版本必须是最新的）
  const before = await readExecutionRecord(commandId, context);
  if (!before) {
    return {
      ok: false,
      status: 404,
      code: "COMMAND_NOT_FOUND",
      message: "执行单不存在或不属于本企业。",
      command_id: commandId,
    };
  }
  if (before.status === "SUCCEEDED") {
    return {
      ok: true,
      already_done: true,
      approval_id: id,
      command_id: commandId,
      status: before.status,
      business_success: before.business_success === true,
      posting_status: before.posting_status ?? "NOT_CONFIRMED",
      authority_receipt_ref: before.authority_receipt?.authority_receipt_ref ?? null,
      note: "这条已经执行完成（以权威回执为准），无需重复推进。",
    };
  }
  if (before.status !== "AUTHORIZED") {
    return {
      ok: false,
      status: 409,
      code: "COMMAND_NOT_EXECUTABLE",
      message: `执行单当前状态是 ${before.status}，不能从这一步继续推进。`,
      current_status: before.status,
      approval_id: id,
      command_id: commandId,
    };
  }
  // 命令要挂在自己的任务下（Context 一致性）
  if (before.context?.task_id) context = { ...context, task_id: before.context.task_id };

  // ② 取租约（CAS：并发时只有一个 worker 拿得到）
  const workerRef = `worker-web-${Date.now().toString(36)}`;
  const lease = await callBackend("transaction", `/internal/v1/commands/${encodeURIComponent(commandId)}/leases`, context, {
    method: "POST",
    body: {
      command_id: commandId,
      worker_ref: workerRef,
      expected_record_version: before.record_version,
      previous_lease_ref: null,
    },
  });
  if (lease.status !== 200 || !lease.body?.lease_ref) {
    return {
      ok: false,
      status: lease.status,
      code: lease.body?.code ?? "LEASE_ACQUIRE_FAILED",
      message: lease.body?.message ?? "取执行租约失败",
      approval_id: id,
      command_id: commandId,
    };
  }

  // ③ 执行前复核（§12：AUTHORIZED 不是永续许可，执行前必须再审一次）
  const review = await callBackend("control", "/internal/v1/approvals/pre-execution-review", context, {
    method: "POST",
    body: {
      command_id: commandId,
      command_record_version: lease.body.record_version,
      lease_ref: lease.body.lease_ref,
      fencing_token: lease.body.fencing_token,
    },
  });
  if (review.status !== 200 || !review.body) {
    return {
      ok: false,
      status: review.status,
      code: review.body?.code ?? "PRE_EXECUTION_REVIEW_FAILED",
      message: review.body?.message ?? "执行前复核失败",
      approval_id: id,
      command_id: commandId,
    };
  }
  if (review.body.allowed !== true) {
    // **最关键的如实转达**：复核不通过时要把**哪个条件没满足**说清，
    // 而不是只丢一句"不允许"。`reason_ref` 是机器码，页面翻成人话。
    const CHECK_KEYS = [
      "approval_valid",
      "payload_hash_matches",
      "policy_version_matches",
      "action_scope_matches",
      "object_version_matches",
      "current_permission_valid",
      "delegation_valid",
      "duties_separated",
      "capabilities_admitted",
      "lease_current",
      "fencing_current",
      "object_lock_held",
      "not_cancelled_before_start",
    ];
    return {
      ok: false,
      status: 403,
      code: "PRE_EXECUTION_REVIEW_NOT_ALLOWED",
      message: "执行前复核未通过，因此**没有执行**。",
      reason_ref: review.body.reason_ref ?? null,
      failed_checks: CHECK_KEYS.filter((key) => review.body[key] === false),
      approval_id: id,
      command_id: commandId,
    };
  }

  // ④ 执行：进入 EXECUTING，并落执行凭证
  const executed = await callBackend("transaction", `/internal/v1/commands/${encodeURIComponent(commandId)}/execute`, context, {
    method: "POST",
    body: {
      command_id: commandId,
      expected_record_version: lease.body.record_version,
      lease_ref: lease.body.lease_ref,
      fencing_token: lease.body.fencing_token,
      review_ref: review.body.review_ref,
    },
  });
  if (executed.status !== 202) {
    return {
      ok: false,
      status: executed.status,
      code: executed.body?.code ?? "EXECUTE_FAILED",
      message: executed.body?.message ?? "执行失败",
      approval_id: id,
      command_id: commandId,
    };
  }

  // ⑤ 受控派发。**必须由 transaction-service 身份发起**（工具侧核这个头），
  //    所以这里临时换身份——那是 BFF 作为可信编排者的职责，不是"提权"：
  //    派发端点自己的核对（凭证/租约/fencing）一项不少。
  const after = await readExecutionRecord(commandId, context);
  const executionRef = after?.execution_credential?.execution_ref;
  if (!executionRef) {
    return {
      ok: false,
      status: 500,
      code: "EXECUTION_CREDENTIAL_MISSING",
      message: "执行已受理但没有拿到执行凭证，无法派发——已如实停在 EXECUTING。",
      status_after: after?.status ?? null,
      approval_id: id,
      command_id: commandId,
    };
  }

  const dispatched = await callBackend("tool", "/internal/v1/tool-executions/controlled", context, {
    method: "POST",
    body: {
      command_id: commandId,
      execution_credential_ref: executionRef,
      lease_ref: lease.body.lease_ref,
      fencing_token: lease.body.fencing_token,
    },
    identityOverride: CONTROLLED_EXECUTION_CALLER,
  });

  return {
    ok: true,
    approval_id: id,
    command_id: commandId,
    dispatched: dispatched.status === 202,
    dispatch_code: dispatched.body?.code ?? null,
    status_after: (await readExecutionRecord(commandId, context))?.status ?? "EXECUTING",
    // 刻意说清"到这里为止"：派发 ≠ 过账。权威回执由源系统给。
    note:
      "已派发执行（状态 EXECUTING）。这不等于已过账——" +
      "只有源系统的权威回执到达，业务成功与过账状态才成立。",
  };
}

/**
 * 拿到「这条批准对应的受控命令」——没有就提交一条。
 *
 * ## 为什么需要"提交"这一步在这里
 *
 * 内核对顺序有一处硬约束：**命令声明 `approval_ref` 时，那条批准必须已存在**
 * （提交时 `find_approval` 查不到就报 `APPROVAL_NOT_FOUND`）。
 * 所以命令只能在**批准之后**提交。
 *
 * 但用户关心的是"我批完了，然后呢"——他不该知道"提交命令"这个内部步骤。
 * 所以把它收在这里，并保证它**幂等**：同一条批准只提交一次。
 *
 * ## 幂等靠什么
 *
 * 不用内存缓存（重启就失效），而是**先查命令账本**：
 * `command_refs` 里已经有指向这条批准的命令就直接复用。
 * 查不到才提交。
 */
async function ensureCommandForApproval(approvalId, admission, identityContext = null) {
  const existing = await findCommandByApproval(approvalId, identityContext);
  if (existing) {
    return {
      command_id: existing.command_id,
      // 复用时也要把 task_id 带回去：读命令需要它（工厂只读链路要求非空）
      task_id: existing.task_id ?? existing.context?.task_id ?? null,
      reused: true,
      admission,
    };
  }

  const pending = await readPendingApproval(approvalId, identityContext);
  if (!pending) {
    return {
      ok: false,
      status: 404,
      code: "APPROVAL_NOT_FOUND",
      message: "找不到这条批准的绑定信息，无法提交执行单。",
    };
  }

  const traceId = `trace-user-cmd-${Date.now().toString(36)}`;
  let context = contextFor(identityContext, traceId);
  if (pending.task_id) context = { ...context, task_id: pending.task_id };

  const submitted = await callBackend("transaction", "/internal/v1/commands/controlled", context, {
    method: "POST",
    body: {
      normalization_ref: pending.normalization_ref,
      policy_evaluation_ref: pending.policy_evaluation_ref,
      approval_ref: approvalId,
      idempotency_key: `user-cmd-${approvalId}`,
    },
  });
  if (submitted.status !== 202 || !submitted.body?.command_id) {
    return {
      ok: false,
      status: submitted.status,
      code: submitted.body?.code ?? "COMMAND_SUBMIT_FAILED",
      message: submitted.body?.message ?? "提交执行单失败",
    };
  }
  return { command_id: submitted.body.command_id, task_id: context.task_id ?? null, reused: false, admission };
}

/** 按批准找它名下的命令（读命令账本，不另记索引）。 */
async function findCommandByApproval(approvalId, identityContext) {
  const tenantId = identityContext?.tenant_id;
  const journalPath = path.join(REPO_ROOT, ".v3-dev", "state", "commands.jsonl");
  let raw = "";
  try {
    raw = fs.readFileSync(journalPath, "utf-8");
  } catch {
    return null;
  }
  let found = null;
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let record;
    try {
      record = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (tenantId && record.tenant_id !== tenantId) continue;
    if (record.approval_ref !== approvalId) continue;
    found = record; // 追加式账本：后写覆盖先写
  }
  return found;
}

/** 读一条批准的绑定（连同它的任务号）。 */
async function readPendingApproval(approvalId, identityContext) {
  const tenantId = identityContext?.tenant_id;
  const journalPath = path.join(REPO_ROOT, ".v3-dev", "state", "approvals.jsonl");
  let raw = "";
  try {
    raw = fs.readFileSync(journalPath, "utf-8");
  } catch {
    return null;
  }
  let found = null;
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let record;
    try {
      record = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (tenantId && record.tenant_id !== tenantId) continue;
    if (record.approval_id !== approvalId) continue;
    found = record;
  }
  if (!found) return null;
  const binding = found.binding ?? {};
  return {
    approval_id: found.approval_id,
    approved: found.approved,
    normalization_ref: binding.normalization_ref ?? null,
    policy_evaluation_ref: found.policy_evaluation_ref ?? null,
    // 任务号写在绑定里（发起时由 BFF 自动建任务并挂上）
    task_id: (binding.context ?? {}).task_id ?? null,
  };
}

async function readExecutionRecord(commandId, context) {
  const result = await callBackend(
    "transaction",
    `/internal/v1/commands/${encodeURIComponent(commandId)}/execution-record`,
    context,
  );
  return result.status === 200 ? result.body : null;
}

/**
 * 用户侧**批准 / 否决**（上游 §6.2「批准必须绑定具体对象、规范化 payload 哈希、
 * 策略版本、动作范围及有效期」）。
 *
 * ## 三个不可妥协之处

 * 1. **批准人身份来自可信 Context**，不由请求体指定。
 *    请求体里的 `actor_id` 会被内核用来写 `approver_ref`——如果让浏览器填，
 *    就等于"谁都能声称自己是别人"，职责分离形同虚设。
 *    所以这里**强制用当前登录身份解析出的 actor**，并把客户端可能传来的
 *    `actor_id` 覆盖掉。
 * 2. **绑定必须逐项一致**：`object_id`/`payload_hash`/`tool_id`/`tool_version`/
 *    `policy_version`/`action_scope` 都要与建立绑定时相同，否则内核报
 *    `APPROVAL_BINDING_CHANGED`（"批准绑 A、执行时 B"就是这样被发现的）。
 * 3. **已决定不可改判**：内核按"事实追加不可变"处理，重复同一决定幂等、
 *    相反决定 409。这里只是如实转达。
 */
async function decideApproval(approvalId, decision, form = {}, identityContext = null) {
  const traceId = `trace-user-decide-${Date.now().toString(36)}`;
  const context = contextFor(identityContext, traceId);
  const id = String(approvalId ?? "").trim();
  if (!id) {
    return { ok: false, status: 400, code: "MISSING_APPROVAL_ID", message: "缺少 approval_id。" };
  }
  if (typeof decision !== "boolean") {
    return { ok: false, status: 400, code: "BAD_DECISION", message: "决定必须是布尔值。" };
  }

  // 需要绑定的六项：**必须由发起方如实回填**（界面从待批准记录里带过来）
  const required = ["object_id", "tool_id", "tool_version", "payload_hash", "policy_version", "action_scope"];
  const missing = required.filter((key) => form[key] === undefined || form[key] === null || form[key] === "");
  if (missing.length) {
    return {
      ok: false,
      status: 400,
      code: "MISSING_BINDING_FIELDS",
      message:
        `批准必须逐项绑定到具体对象与 payload；缺少：${missing.join("、")}。` +
        "这些字段应当从待批准记录里带过来，不能由批准人凭印象填写。",
    };
  }

  const decisionBody = {
    approval_id: id,
    approved: decision,
    object_id: form.object_id,
    expected_version: form.expected_version ?? 0,
    tool_id: form.tool_id,
    tool_version: form.tool_version,
    command_type: form.command_type ?? form.tool_id,
    action_scope: Array.isArray(form.action_scope) ? form.action_scope : [form.action_scope],
    payload_hash: form.payload_hash,
    payload_ref: form.payload_ref ?? form.object_id,
    normalization_ref: form.normalization_ref ?? null,
    policy_version: form.policy_version,
    // ⚠️ **覆盖**客户端可能传来的 actor_id：批准人必须是当前登录身份
    actor_id: context.actor_id,
    valid_until: form.valid_until ?? "2026-12-31T00:00:00Z",
  };

  const result = await callBackend("control", `/api/v1/approvals/${encodeURIComponent(id)}/decisions`, context, {
    method: "POST",
    body: decisionBody,
  });

  if (result.status !== 200) {
    return {
      ok: false,
      status: result.status,
      code: result.body?.code ?? "APPROVAL_DECISION_FAILED",
      message: result.body?.message ?? "批准决定失败",
    };
  }
  return {
    ok: true,
    approval_id: result.body.approval_id ?? id,
    approved: result.body.approved,
    record_version: result.body.record_version ?? null,
    decision_ref: result.body.decision_ref ?? null,
    decided_by: context.actor_id,
    note: decision
      ? "批准已成立。**这不等于已执行**——执行由 Transaction 按命令走完并取权威回执。"
      : "已否决。按事实追加不可变，此决定不能改判为通过。",
  };
}

/**
 * 本租户的**待批准清单**（`approved === null`）。
 *
 * 与任务列表同理：内核没有"列批准"的端点，所以 BFF 直接读批准账本
 * （`approvals.jsonl`）——**不另记一份**，避免第二份真相。
 *
 * **只列本租户**，且**只列尚未决定的**：已决定的不该出现在"待我批准"里，
 * 那会让人以为还有事没做。
 */
function listPendingApprovals(tenantId) {
  const journalPath = path.join(REPO_ROOT, ".v3-dev", "state", "approvals.jsonl");
  let raw = "";
  try {
    raw = fs.readFileSync(journalPath, "utf-8");
  } catch (error) {
    return {
      approvals: [],
      note: `批准账本暂不可读（${error.code ?? error.message}）——这不代表没有待批准。`,
    };
  }

  const latest = new Map();
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let record;
    try {
      record = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (record.tenant_id !== tenantId) continue; // ← 租户隔离
    if (!record.approval_id) continue;
    latest.set(record.approval_id, record);
  }

  const pending = [...latest.values()]
    .filter((record) => record.approved === null || record.approved === undefined)
    .map((record) => {
      const binding = record.binding ?? {};
      return {
        approval_id: record.approval_id,
        trace_id: record.trace_id ?? null,
        object_id: binding.object_id ?? null,
        expected_version: binding.expected_version ?? null,
        tool_id: binding.tool_id ?? null,
        tool_version: binding.tool_version ?? null,
        command_type: binding.command_type ?? null,
        action_scope: Array.isArray(binding.action_scope) ? binding.action_scope : [],
        payload_hash: binding.payload_hash ?? null,
        payload_ref: binding.payload_ref ?? null,
        normalization_ref: binding.normalization_ref ?? null,
        policy_version: binding.policy_version ?? null,
        policy_evaluation_ref: record.policy_evaluation_ref ?? null,
        valid_until: record.valid_until ?? null,
        updated_at: record.updated_at ?? null,
        // **发起人是谁**（受委托执行人，来自 Tool Gateway 的规范化结果）。
        //
        // 页面需要它判断"**这条该不该由我来批**"：系统要求审批人不是发起人
        // （职责分离），所以给发起人摆一个"批准"按钮是**死路**——点下去必然被拒。
        // 与其让他撞一次墙，不如一开始就说清"这条要别人批"。
        requester_actor: (binding.context ?? {}).actor_id ?? null,
        // 这次批准**关涉什么业务对象**（当前无业务名称，见 describeBusinessObject 的说明）
        business: describeBusinessObject(record),
      };
    })
    .sort((a, b) => String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")));

  return {
    approvals: pending,
    total: pending.length,
    note:
      pending.length === 0
        ? "本租户暂无待批准。"
        : "只列**尚未决定**的批准；已决定的按「事实追加不可变」保留在账本里，不再出现在这里。",
  };
}

async function createTask(form = {}, identityContext = null) {
  const traceId = `trace-user-task-${Date.now().toString(36)}`;
  const context = contextFor(identityContext, traceId);
  const inputRef = String(form.input_ref ?? "").trim() || "pmc.weekly-schedule";

  const created = await callBackend("control", "/api/v1/tasks", context, {
    method: "POST",
    body: {
      ...context,
      context,
      idempotency_key: `user-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      input_ref: inputRef,
      input_schema: { type: "object" },
    },
  });

  if (created.status !== 202 || !created.body?.task_id) {
    return {
      ok: false,
      status: created.status,
      code: created.body?.code ?? "TASK_CREATE_FAILED",
      message: created.body?.message ?? "任务受理失败",
    };
  }
  return {
    ok: true,
    // **受理即返回 202，不是完成**——如实转达，别让界面把它当"已成功"。
    accepted: true,
    task_id: created.body.task_id,
    status: created.body.status,
    status_source: created.body.status_source ?? null,
    updated_at: created.body.updated_at ?? null,
    note: "任务已受理（ACCEPTED）。这不表示已完成——要看结论请查这条任务。",
  };
}

/**
 * 用户侧**AI 员工目录**。
 *
 * ## 数据来源与形状（2026-10-05 更新）
 *
 * 登记表的 `employees` 原先是一串**裸 id**；现在按契约
 * `packages/contracts/schemas/p3-employee.json` 存**结构化记录**：
 * `{ employee_id, executing_actor_ref, positions[{ delegated_scopes, limits[] }], config_authority }`。
 *
 * 所以这一页从"只能显示一个裸 id"变成了真能回答
 * **"这个 AI 员工能做什么、以谁的身份、受什么约束"**。
 *
 * ## 一条仍然如实标注的事实
 *
 * `delegated_scopes` 在登记表里**同时存在**于租户级与岗位级。本页显示的是
 * **岗位级**（更精确），并把租户级放在旁边对照——**两者的差别本身是信息**：
 * 岗位级是"这个人被授予什么"，租户级是"这个租户整体被允许什么"。
 */
async function listEmployees(identityContext = null) {
  const tenantId = identityContext?.tenant_id ?? USER_FACING_IDENTITY.tenant_id;
  let registry = null;
  try {
    registry = JSON.parse(
      fs.readFileSync(
        path.join(REPO_ROOT, "services", "_kernel", "registry.mock.json"),
        "utf-8",
      ),
    );
  } catch (error) {
    return {
      error: { code: "REGISTRY_UNREADABLE", message: `登记表读不到：${error.message}` },
    };
  }

  const tenant = registry?.tenants?.[tenantId] ?? {};
  const tenantScopes = Array.isArray(tenant.delegated_scopes) ? tenant.delegated_scopes : [];
  const entries = Array.isArray(tenant.employees) ? tenant.employees : [];

  const employees = entries.map((entry) => {
    // 兼容裸 id 形态：历史上登记表就是这么写的。**读法只在这一处分支**，
    // 页面不自己判断形状——否则形状一变，多个页面各错各的。
    if (typeof entry === "string") {
      return {
        employee_id: entry,
        executing_actor_ref: null,
        positions: [],
        config_authority: null,
        tenant_delegated_scopes: tenantScopes,
        incomplete: true,
      };
    }
    const positions = Array.isArray(entry.positions) ? entry.positions : [];
    return {
      employee_id: entry.employee_id,
      executing_actor_ref: entry.executing_actor_ref ?? null,
      factory_ids: Array.isArray(entry.factory_ids) ? entry.factory_ids : [],
      config_authority: entry.config_authority ?? null,
      tenant_delegated_scopes: tenantScopes,
      positions: positions.map((position) => ({
        position_id: position.position_id,
        title: position.title ?? null,
        delegated_scopes: Array.isArray(position.delegated_scopes) ? position.delegated_scopes : [],
        data_class_ceiling: position.data_class_ceiling ?? null,
        limits: (Array.isArray(position.limits) ? position.limits : []).map((limit) => ({
          risk_class: limit.risk_class,
          decision: limit.decision,
          max_amount: limit.max_amount ?? null,
          requires_second_approval: limit.requires_second_approval === true,
        })),
      })),
      incomplete: false,
    };
  });

  // 只在**确实有人不完整**时才提示，避免页面永远挂着一条没用的警告
  const incomplete = employees.filter((e) => e.incomplete).length;

  return {
    tenant_id: tenantId,
    factory_id: identityContext?.factory_id ?? USER_FACING_IDENTITY.factory_id,
    employees,
    incomplete_count: incomplete,
    note:
      "权限挂在**岗位**上，不挂人：员工的权限 = 其所任岗位的并集。" +
      "改权限靠改任命，不靠逐人配额度。",
  };
}

/**
 * V2.1 公司 → V3.0 身份 的映射（**唯一接缝**）。
 *
 * ## 为什么需要它
 *
 * V2.1 只有「公司(company)」，没有工厂；V3.0 的语义是 tenant / factory / employee。
 * 两者**不会自动对应**——必须显式声明，否则只能猜，而猜错的后果是
 * **一家公司看到另一家的数据**。
 *
 * ## 两条 fail-closed 决定
 *
 * 1. **未登录 ⇒ 401**，不退回演示身份。V3.0 页面全都涉及"我的单子/我的员工"，
 *    匿名给一份演示数据等于**让人以为那是自己的**。
 * 2. **公司未映射 ⇒ 403 TENANT_NOT_MAPPED**，不静默回退到演示租户。
 *    回退的后果同上，而且更难发现（页面照常显示，只是数据是别人的）。
 */
const TENANT_MAP_PATH = path.join(REPO_ROOT, "services", "_kernel", "v3_tenant_map.json");

/** V2.1 后端地址：只用来校验 token（**不读它的密钥、不复制它的 JWT 逻辑**）。 */
const V21_BACKEND = process.env.V3_V21_BACKEND ?? "http://127.0.0.1:8000";

function readTenantMap() {
  try {
    return JSON.parse(fs.readFileSync(TENANT_MAP_PATH, "utf-8"));
  } catch (error) {
    return { _read_error: error.message, mappings: {} };
  }
}

/**
 * 校验 V2.1 的 `Bearer` token，取回用户与所属公司。
 *
 * **用 V2.1 自己的 `/auth/me` 校验**，而不是在本服务里解 JWT：
 * 解 JWT 需要 V2.1 的密钥（那等于把密钥复制一份），而且一旦 V2.1 改了
 * 签发改动（过期、吊销），这里会**悄悄验过**。让签发方自己回答"这票据是谁的"，
 * 是唯一不会分叉的做法。
 */
async function verifyV21Token(token) {
  try {
    const response = await fetch(`${V21_BACKEND}/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (response.status !== 200) {
      return { ok: false, status: response.status };
    }
    return { ok: true, user: await response.json() };
  } catch (error) {
    return { ok: false, status: 502, message: `V2.1 后端不可达：${error.message}` };
  }
}

/**
 * 解析本次请求的 **V3.0 身份**。
 *
 * 返回 `{ ok: true, context, identity }` 或 `{ ok: false, status, body }`。
 */
async function resolveIdentity(req, traceId) {
  const header = req.headers.authorization ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!token) {
    return {
      ok: false,
      status: 401,
      body: {
        code: "AUTH_REQUIRED",
        message: "V3.0 页面需要 V2.1 登录态；请先登录。",
      },
    };
  }

  const verified = await verifyV21Token(token);
  if (!verified.ok) {
    return {
      ok: false,
      status: verified.status === 401 ? 401 : 502,
      body: {
        code: verified.status === 401 ? "AUTH_INVALID" : "AUTH_BACKEND_UNAVAILABLE",
        message:
          verified.status === 401
            ? "登录态无效或已过期，请重新登录。"
            : (verified.message ?? "无法校验登录态。"),
      },
    };
  }

  const user = verified.user ?? {};
  const companyId = user.company_id;

  // ⚠️ **先判"有没有租户归属"，再判"租户有没有映射"**。
  // 反过来会让"不属于任何公司"报成 `TENANT_NOT_MAPPED`，而那句提示
  // 叫用户去登记映射——**方向是错的**，问题不在映射表，在这个账号本身。
  if (companyId === null || companyId === undefined) {
    return {
      ok: false,
      status: 403,
      body: {
        code: "NO_TENANT_SCOPE",
        message:
          "当前账号不属于任何公司，因此没有租户范围（平台超管请用管理台 / 8300 查看）。",
      },
    };
  }

  const tenantMap = readTenantMap();
  const mapping = tenantMap.mappings?.[String(companyId)];
  if (!mapping) {
    return {
      ok: false,
      status: 403,
      body: {
        code: "TENANT_NOT_MAPPED",
        message:
          `公司 ${companyId} 尚未映射到 V3.0 租户。请先在 ` +
          "`services/_kernel/v3_tenant_map.json` 登记。**不会**回退到演示租户——" +
          "那会让你看到别家公司的数据。",
      },
    };
  }

  // ⚠️ **actor 按用户取，不按公司取**（2026-10-06）。
  //
  // 原先映射表里每个公司只有一个 `actor_id`，于是公司里所有人共用同一个
  // 执行身份——后果不是"不方便"，而是**职责分离永远不成立**：
  // 同一个人既发起又批准，而 `revalidate_before_execution` 会因此报
  // `DUTIES_NOT_SEPARATED` 并拒绝执行（实测）。
  //
  // 所以 actor 逐用户登记；**查不到就拒绝**（不退回 tenant 级默认 actor）——
  // 退回会把"某个人"变成"某公司的执行身份"，绕过职责分离。
  const userEmail = String(user.email ?? "").toLowerCase();
  const actors = mapping.actors ?? {};
  const actorId =
    actors[user.email] ??
    actors[userEmail] ??
    Object.entries(actors).find(([email]) => email.toLowerCase() === userEmail)?.[1];

  if (!actorId) {
    return {
      ok: false,
      status: 403,
      body: {
        code: "ACTOR_NOT_MAPPED",
        message:
          `用户 ${user.email} 尚未映射到 V3.0 执行身份（公司 ${companyId} 下）。` +
          "请在 `services/_kernel/v3_tenant_map.json` 的 `mappings[<公司>].actors` 里登记。" +
          "**不会**回退到公司级默认身份——那会让一个人代表整个公司，绕过职责分离。",
      },
    };
  }

  // actor 必须是登记表 `tenants[*].actors` 里**已有**的名字：
  // 映射表只写"谁是谁"，不新增 V3 侧身份。写错时会在这里当场发现，
  // 而不是等某个写操作被 403 拒掉再回头查。
  if (!registryAllowsActor(mapping.tenant_id, actorId)) {
    return {
      ok: false,
      status: 500,
      body: {
        code: "ACTOR_NOT_IN_REGISTRY",
        message:
          `映射把 ${user.email} 指到了 actor ${actorId}，但它不在登记表 ` +
          `tenants["${mapping.tenant_id}"].actors 里。请先把它加进 ` +
          "`services/_kernel/registry.mock.json`（两处必须一致）。",
      },
    };
  }

  const context = {
    tenant_id: mapping.tenant_id,
    factory_id: mapping.factory_id,
    // ⚠️ **actor_id 必须来自映射表，不能用 user.email**。
    // V2.1 的用户名是邮箱（`admin@demo.com`），而 V3.0 的 `actor_id` 必须是
    // 合法标识符（契约 pattern 不含 `@`）——把邮箱塞进去会被控制面
    // 400 `REQUEST_INVALID` 拒掉（实测）。actor_id 与 tenant/factory 同属
    // **后端标识符**，所以同样显式登记在 `v3_tenant_map.json` 里。
    actor_id: actorId,
    employee_id: mapping.default_employee_id,
    task_id: null,
    trace_id: traceId,
    data_class: "C2",
    schema_version: "1.0.0",
    deadline_at: "2026-12-31T00:00:00Z",
  };
  return {
    ok: true,
    context,
    identity: {
      company_id: companyId,
      tenant_id: mapping.tenant_id,
      factory_id: mapping.factory_id,
      actor_id: actorId,
      employee_id: mapping.default_employee_id,
      label: mapping.label ?? null,
      // 邮箱只用于**显示与审计追溯**，不参与后端标识符
      user_email: user.email ?? null,
      role: user.role ?? null,
    },
  };
}

/**
 * 该 tenant 的登记表里有没有这个 actor。
 *
 * 读登记表**当场核对**，而不是信任映射表：两处写不一致时，
 * 症状是"某些写操作莫名 403"，很难定位；在这里挡住能直接指出是哪两处不一致。
 */
function registryAllowsActor(tenantId, actorId) {
  try {
    const registry = JSON.parse(
      fs.readFileSync(path.join(REPO_ROOT, "services", "_kernel", "registry.mock.json"), "utf-8"),
    );
    const actors = registry?.tenants?.[tenantId]?.actors;
    return Array.isArray(actors) && actors.includes(actorId);
  } catch {
    // 登记表读不到时**不放行**：这是一条身份判据，读不到就不能说"可以"。
    return false;
  }
}

/**
 * 用户侧**我的任务**列表。
 *
 * ## 为什么要读 journal 而不是另记一份
 *
 * 任务的真相源是 control-plane 写的 `tasks` journal（见
 * `services/_kernel/routes.py` 的 `persist_task`，注释写明"单一真相源：
 * 任务状态只写 journal 一次，其它运行单元通过同一个 journal 读"）。
 *
 * 如果这里自己维护一份"我提交过的任务"，就是**第四份任务真相**——
 * 它会与 journal 分叉，而且分叉时**没人知道哪个对**。
 * 所以直接读同一个 journal，按租户过滤。
 *
 * ## 为什么没有服务端 list 端点
 *
 * 内核目前只有 `GET /api/v1/tasks/{task_id}`（按 id 查），没有 list。
 * 加一个 list 端点属于**契约面改动**（要动 `packages/contracts` 与 openapi），
 * 本轮不擅自扩大范围；**读数**则由 BFF 直接读真相源，语义与内核一致。
 *
 * ## 租户过滤是硬要求
 *
 * journal 是**所有租户共用**的一个文件。少做这一步过滤，
 * 就会把别家公司的 task_id 列给用户看——那正是多租户最典型的漏洞。
 */
function listTasksForTenant(tenantId) {
  const journalPath = path.join(REPO_ROOT, ".v3-dev", "state", "tasks.jsonl");
  let raw = "";
  try {
    raw = fs.readFileSync(journalPath, "utf-8");
  } catch (error) {
    return {
      tasks: [],
      note: `任务账本暂不可读（${error.code ?? error.message}）——这不代表你没有任务。`,
    };
  }

  // journal 是**追加式**的：同一 task_id 可能出现多次，**后写覆盖先写**。
  const latest = new Map();
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let record;
    try {
      record = JSON.parse(trimmed);
    } catch {
      continue; // 半行/损坏行跳过：不能让一行坏数据毁掉整页
    }
    if (record.tenant_id !== tenantId) continue; // ← 租户隔离
    if (!record.task_id) continue;
    latest.set(record.task_id, record);
  }

  // 命令账本：只为了数"每个任务下有几条命令"。
  // 任务记录自己的 `command_refs` 恒为空（内核在组装视图时才派生，见
  // `services/_kernel/routes.py::_commands_of_task`），所以这里也得自己数。
  const commands = readCommandRecords(tenantId);
  const commandsReadable = commands !== null;

  const tasks = [...latest.values()]
    .map((record) => {
      // 一个任务**关涉什么业务对象**：从它名下的命令里读（任务记录自己不带这些字段，
      // 实测任务记录只有 task_id/status/fingerprint/idempotency_key 等）。
      const mine = commandsReadable
        ? commands.filter((candidate) => candidate.task_id === record.task_id)
        : [];
      return {
        task_id: record.task_id,
        status: record.status ?? null,
        updated_at: record.updated_at ?? null,
        // 任务记录里**没有** input_ref（实测确认），恒为 null。
        // 保留这个字段是为了让界面显式显示"—"，而不是悄悄少一列。
        input_ref: null,
        // 读不到命令账本时**返回 null 而不是 0**：
        // 0 的含义是"这条任务确实没有命令"，与"我不知道"完全不同。
        // 把未知显示成 0 会让用户以为某个已执行的订单没有副作用。
        command_count: commandsReadable ? mine.length : null,
        business: mine.length ? describeBusinessObject(mine[mine.length - 1]) : null,
      };
    })
    .sort((a, b) => String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")));

  return {
    tasks,
    total: tasks.length,
    note:
      tasks.length === 0
        ? "本租户暂无任务。"
        : "只列出本租户的任务；别的公司的任务不在这个账本视图里。" +
          (commandsReadable ? "" : "（命令账本暂不可读，故操作数显示为「—」而非 0。）"),
  };
}

/**
 * 把一条命令**关涉的业务对象**整理成界面能用的形状。
 *
 * ## 一条必须说清的诚实边界（2026-10-06 取证确认）
 *
 * 我查过实际数据，我们**根本没有业务名称**。命令与规范化记录里只有：
 *
 *     object_id  = "schedule-ok-d45259"   ← 技术编号
 *     payload_ref = "schedule-input-demo"  ← 内部引用
 *     tool_id    = "pmc.dispatch.publish"  ← 操作
 *
 * 没有「41 周排产计划」「3 号仓原料」这类客户认得的名字。
 * 那个名字应当由 **ERP/PMC 的业务对象**提供，而上游把「真实外部字段映射」
 * 明确列为**待冻结的外部依赖**。
 *
 * 所以这里**不去编一个名字**，而是：
 * - 把**操作**用人话说清（那部分我们有依据：工具登记表里就是干这个的）；
 * - 把**技术编号**原样带上，供追溯；
 * - **显式留出 `name` 位置并标为待接入**，等外部映射就位后由 BFF 填上。
 *
 * 编一个像样的名字会更好看，但那会让客户以为系统认识他的单子 —— 而它并不认识。
 */
function describeBusinessObject(command) {
  const binding = command?.binding ?? {};
  return {
    // 业务名称：**当前取不到**。留空比编造安全。
    name: null,
    name_source: null,
    // 操作的技术代号与人话标题（人话标题由前端术语层给，BFF 不重复维护一份）
    command_type: binding.command_type ?? null,
    tool_id: binding.tool_id ?? null,
    // 技术编号：客户不认，但审计与排障要靠它
    object_id: binding.object_id ?? null,
    payload_ref: binding.payload_ref ?? null,
    expected_version: binding.expected_version ?? null,
    // 明确告诉调用方"为什么没有名字"，而不是让它以为是 bug
    name_unavailable_reason:
      "业务对象名称需由 ERP/PMC 提供（外部字段映射尚未接入），当前只有系统编号。",
  };
}

/**
 * 读本租户的命令记录（只为计数）。
 *
 * 读不到时返回 `null`（**不是空数组**）：空数组的含义是"确实没有命令"，
 * `null` 的含义是"我不知道"——两者在界面上必须区分。
 *
 * 命令的 `task_id` 由内核在受控命令提交时写入记录**顶层**
 * （取自可信 Context，不是请求体字段）。
 */
function readCommandRecords(tenantId) {
  const journalPath = path.join(REPO_ROOT, ".v3-dev", "state", "commands.jsonl");
  let raw = "";
  try {
    raw = fs.readFileSync(journalPath, "utf-8");
  } catch {
    return null;
  }
  const latest = new Map();
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let record;
    try {
      record = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (record.tenant_id !== tenantId) continue;
    if (!record.command_id) continue;
    latest.set(record.command_id, record);
  }
  return [...latest.values()];
}

async function serveStatic(res, urlPath) {
  // 两个根：
  // - `/`（含页面）来自 `public/`
  // - `/src/*` 与判定源码同源暴露，便于在浏览器里**直接核对**判定逻辑，
  //   而不是只能相信服务端渲染出来的结论。
  let baseDir = PUBLIC_DIR;
  let relative = urlPath;
  if (urlPath.startsWith("/src/")) {
    baseDir = path.join(HERE, "src");
    relative = urlPath.slice("/src".length);
  } else if (urlPath === "/") {
    relative = "/index.html";
  }

  // **目录穿越防护**：resolve 之后再确认仍在允许的目录内。
  const target = path.resolve(baseDir, "." + relative);
  if (!target.startsWith(baseDir)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
    return;
  }
  res.writeHead(200, {
    "content-type": CONTENT_TYPES[path.extname(target)] ?? "application/octet-stream",
    // UI 是调试用的：不缓存，避免"改了没生效"的假象
    "cache-control": "no-store",
  });
  fs.createReadStream(target).pipe(res);
}

async function readJsonBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
  } catch {
    throw new Error("请求体不是合法 JSON");
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${ADMIN_PORT}`);
  try {
    if (url.pathname === "/api/v3/task") {
      const taskId = url.searchParams.get("task_id");
      if (!taskId) return json(res, 400, { code: "MISSING_PARAM", message: "缺少 task_id" });
      return json(res, 200, await resolveTask(taskId));
    }
    if (url.pathname === "/api/v3/approval") {
      const approvalId = url.searchParams.get("approval_id");
      if (!approvalId) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少 approval_id" });
      }
      return json(res, 200, await resolveApproval(approvalId));
    }
    if (url.pathname === "/api/v3/command") {
      const commandId = url.searchParams.get("command_id");
      if (!commandId) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少 command_id" });
      }
      return json(res, 200, await resolveCommand(commandId));
    }
    // **写路径**：提交批准决定。默认 403（未配置决定人），见 submitApprovalDecision 的说明。
    if (url.pathname === "/api/v3/approval-decision" && req.method === "POST") {
      const body = await readJsonBody(req);
      if (!body.approval_id) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少 approval_id" });
      }
      const outcome = await submitApprovalDecision(body.approval_id, {
        approved: body.approved,
        comment: body.comment ?? null,
        // 绑定由知情操作方提供（平台无「读取待决批准绑定」接口）；
        // 真正的一致性校验在后端，这里只做结构性预检。
        binding: body.binding ?? null,
      });
      return json(res, outcome.ok ? 200 : outcome.status || 400, outcome);
    }
    if (url.pathname === "/api/v3/reconciliation") {
      const commandId = url.searchParams.get("command_id");
      if (!commandId) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少 command_id" });
      }
      return json(res, 200, await resolveReconciliation(commandId));
    }
    if (url.pathname === "/api/v3/release") {
      const releaseId = url.searchParams.get("release_id");
      if (!releaseId) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少 release_id" });
      }
      return json(res, 200, await resolveRelease(releaseId));
    }
    // **写路径**：推进发布台账一格。安全边界在后端（见 submitReleaseTransition）。
    if (url.pathname === "/api/v3/release-transition" && req.method === "POST") {
      const body = await readJsonBody(req);
      if (!body.release_id) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少 release_id" });
      }
      if (!body.to) {
        return json(res, 400, { code: "MISSING_PARAM", message: "缺少目标状态 to" });
      }
      const outcome = await submitReleaseTransition(body.release_id, body.to, {
        gateEvidence: body.gate_evidence ?? [],
        evidenceRefs: body.evidence_refs ?? [],
        approvalRef: body.approval_ref ?? null,
      });
      return json(res, outcome.ok ? 200 : outcome.status || 400, outcome);
    }
    // ==================================================================
    // V3.0 **用户侧**接口（前缀 /v3-user，由 V2.1 前端经 vite 代理调用）
    // ==================================================================
    //
    // ## 为什么和 /api/v3/*（管理台）分开
    //
    // 同一个 BFF，**两种读者**：
    // - 管理台（`/api/v3/*`）：运维视角——命令状态、批准可决定性、七态台账细节；
    // - 用户侧（`/v3-user/*`）：业务视角——**我这单成功没有、过账没有**、
    //   这个 AI 员工能做什么。
    //
    // 分开的**真正理由**不是美观，而是**别把运维语汇灌给业务用户**：
    // 业务用户不需要看 `RECONCILIATION_NOT_FOUND`，他需要看「这单还没查过状态」。
    // 所以用户侧端点在服务端就把语义翻好，页面不自己判断。
    //
    // ## 身份
    //
    // **固定演示身份**（tenant-demo / factory-demo / employee-planning），
    // 由 `demoContext()` 在**服务端**注入；浏览器里既没有 token 也没有内部地址。
    // 这与 V2.1 的 JWT 是两套身份，暂未映射——**这是如实登记的限制，不是遗漏**。
    if (url.pathname === "/v3-user/tasks") {
      const id = await resolveIdentity(req, `trace-user-list-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      return json(res, 200, {
        ...listTasksForTenant(id.context.tenant_id),
        tenant_id: id.context.tenant_id,
        identity: id.identity,
      });
    }
    if (url.pathname === "/v3-user/approvals") {
      const id = await resolveIdentity(req, `trace-user-appr-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      return json(res, 200, {
        ...listPendingApprovals(id.context.tenant_id),
        tenant_id: id.context.tenant_id,
        identity: id.identity,
      });
    }
    if (url.pathname === "/v3-user/write-request" && req.method === "POST") {
      const id = await resolveIdentity(req, `trace-user-wr-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      const form = await readJsonBody(req);
      return json(res, 200, { ...(await requestApproval(form, id.context)), identity: id.identity });
    }
    if (url.pathname === "/v3-user/approval-decision" && req.method === "POST") {
      const id = await resolveIdentity(req, `trace-user-ad-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      const form = await readJsonBody(req);
      const result = await decideApproval(form.approval_id, form.approved, form, id.context);
      return json(res, result.ok ? 200 : result.status, { ...result, identity: id.identity });
    }
    if (url.pathname === "/v3-user/execute" && req.method === "POST") {
      const id = await resolveIdentity(req, `trace-user-exec-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      const form = await readJsonBody(req);
      const result = await continueExecution(form.approval_id, id.context);
      return json(res, result.ok ? 200 : result.status, { ...result, identity: id.identity });
    }
    if (url.pathname === "/v3-user/task") {
      const taskId = url.searchParams.get("task_id");
      if (!taskId) return json(res, 400, { code: "MISSING_PARAM", message: "缺少 task_id" });
      // **先解析身份，再读数据**：拿不到身份就不去读——
      // 用演示身份读一次再"看情况拒绝"，会有窗口把别人的数据带出来。
      const id = await resolveIdentity(req, `trace-user-task-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      const view = await resolveTask(taskId, id.context);
      // 用户侧只加一层"人话"，不改判定——判定权仍在 `deriveDisplayStatus`
      return json(res, 200, { ...view, identity: id.identity });
    }
    if (url.pathname === "/v3-user/task-create" && req.method === "POST") {
      const id = await resolveIdentity(req, `trace-user-new-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      const form = await readJsonBody(req);
      return json(res, 200, await createTask(form, id.context));
    }
    if (url.pathname === "/v3-user/employees") {
      const id = await resolveIdentity(req, `trace-user-emp-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      return json(res, 200, { ...(await listEmployees(id.context)), identity: id.identity });
    }
    if (url.pathname === "/v3-user/release") {
      const releaseId = url.searchParams.get("release_id");
      if (!releaseId) return json(res, 400, { code: "MISSING_PARAM", message: "缺少 release_id" });
      const id = await resolveIdentity(req, `trace-user-rel-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      return json(res, 200, { ...(await resolveRelease(releaseId, id.context)), identity: id.identity });
    }
    if (url.pathname === "/v3-user/reconciliation") {
      const commandId = url.searchParams.get("command_id");
      if (!commandId) return json(res, 400, { code: "MISSING_PARAM", message: "缺少 command_id" });
      const id = await resolveIdentity(req, `trace-user-rec-${Date.now().toString(36)}`);
      if (!id.ok) return json(res, id.status, id.body);
      return json(res, 200, {
        ...(await resolveReconciliation(commandId, id.context)),
        identity: id.identity,
      });
    }
    if (url.pathname === "/v3-user/health") {
      // 健康检查**不带身份也能看**：它只报"后端活没活"，不含任何租户数据。
      // 但会**如实说明登录态是否有效**，避免页面在未登录时看起来一切正常。
      const results = {};
      for (const [name, port] of Object.entries(BACKENDS)) {
        try {
          const response = await fetch(`http://127.0.0.1:${port}/health/live`);
          results[name] = { port, alive: response.status === 200 };
        } catch {
          results[name] = { port, alive: false };
        }
      }
      const id = await resolveIdentity(req, `trace-user-hl-${Date.now().toString(36)}`);
      return json(res, 200, {
        backends: results,
        authenticated: id.ok,
        identity: id.ok ? id.identity : null,
        auth_error: id.ok ? null : id.body,
      });
    }
    if (url.pathname === "/api/v3/health") {
      // **遍历 BACKENDS，不写死端口**——写死会让"改了端口"与"没改"无法区分。
      const results = {};
      for (const [name, port] of Object.entries(BACKENDS)) {
        try {
          const response = await fetch(`http://127.0.0.1:${port}/health/live`);
          results[name] = { port, alive: response.status === 200 };
        } catch {
          results[name] = { port, alive: false };
        }
      }
      // **写路径状态必须披露**：界面要能如实告诉使用者"现在能不能做决定"，
      // 而不是等他点了才发现 403。
      return json(res, 200, {
        admin: {
          port: ADMIN_PORT,
          writeEnabled: Boolean(APPROVER_ACTOR),
          approverActor: APPROVER_ACTOR,
          approverTenant: APPROVER_TENANT,
        },
        backends: results,
      });
    }
    return await serveStatic(res, url.pathname);
  } catch (error) {
    // 后端没起来是**最可能**的情况，要说人话并给出下一步
    return json(res, 502, {
      code: "BACKEND_UNREACHABLE",
      message: `无法连接后端（${error.message}）。请先启动 V3 服务集群。`,
    });
  }
});

server.listen(ADMIN_PORT, "127.0.0.1", () => {
  // **必须打印实际绑定的端口**：`V3_ADMIN_PORT=0` 时由系统分配，
  // 打印配置值会给出 0 这个不可连接的端口（测试脚本就靠这行读回端口）。
  const boundPort = server.address().port;
  console.log(`V3 管理后台：http://127.0.0.1:${boundPort}`);
  console.log(`后端：control=${BACKENDS.control} transaction=${BACKENDS.transaction}`);
  console.log(
    APPROVER_ACTOR
      ? `写路径：开（决定人 ${APPROVER_ACTOR}，租户 ${APPROVER_TENANT}）`
      : "写路径：关（未配置 V3_ADMIN_APPROVER_ACTOR）",
  );
});
