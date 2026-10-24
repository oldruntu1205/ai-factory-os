/**
 * 回归守卫：**BFF 必须从执行记录里读 `authority_receipt`**。
 *
 * ## 为什么要有这条
 *
 * 2026-10-06 实测到一个"不报错但结论恒定错误"的真 bug：
 *
 * - 内核的 P3 执行记录把权威回执放在 **`authority_receipt`**；
 * - 而 BFF 的 `resolveTask` 读的是 **`.receipt`**（不存在的键）；
 * - 于是**回执明明在，BFF 永远取到 `null`** ⇒ 任务结论页永远显示「未过账」。
 *
 * 实测证据（修之前）：命令已 `SUCCEEDED`、`posting_status=POSTED`、
 * 回执 `authority_verified=true`，而任务结论页仍报「未过账」。
 *
 * 同一文件里 `resolveCommand` **本来就写对了** `authority_receipt`——
 * 一个概念两种写法，正是这类缺陷的典型成因。
 *
 * ## 它守什么
 *
 * 1. `resolveTask` / `resolveCommand` 都从 `authority_receipt` 取回执；
 * 2. **不允许**再把裸 `.receipt` 当作回执来源（读不到就是读不到，
 *    悄悄回退会让"没回执"与"回执在别的键上"混淆）；
 * 3. 内核侧确实把回执**写进**命令记录（否则 BFF 再对也没用）。
 *
 * 这是**源码级守卫**（读文件、断言形状），不依赖起服务——
 * 因为它防的是"用错了字段名"，那是个静态事实。
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ADMIN = path.resolve(HERE, "..");
const REPO_ROOT = path.resolve(WEB_ADMIN, "..", "..");

/** 去掉注释，避免"注释里提到 .receipt"被当成真的读它。 */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .map((line) => line.replace(/(^|[^:])\/\/.*$/, "$1"))
    .join("\n");
}

test("BFF 从 authority_receipt 取回执（不是裸 receipt）", () => {
  const source = stripComments(readFileSync(path.join(WEB_ADMIN, "server.mjs"), "utf-8"));

  const readsAuthority = source.match(/authority_receipt/g) ?? [];
  assert.ok(
    readsAuthority.length >= 2,
    `server.mjs 应当至少在两处读 authority_receipt（resolveTask / resolveCommand），实际 ${readsAuthority.length} 处`,
  );

  // 裸 `.receipt`（后面不接 `_`）不允许再被当作回执来源。
  // 允许的形态是 `authority_receipt` / `receipt_refs` / `receipt_id` 等带后缀的名字，
  // 所以只挑出 `.receipt` 后面紧跟非标识符字符、且前一个字符不是 `_` 的用法。
  const bareReceipt = [...source.matchAll(/(?<![\w.])\.receipt(?![\w])/g)];
  assert.equal(
    bareReceipt.length,
    0,
    "不得再用裸 `.receipt` 当回执来源：内核字段是 `authority_receipt`，" +
      "读错键不会报错，只会让「已过账」永远显示不出来。\n" +
      bareReceipt.map((hit) => {
        const before = source.slice(0, hit.index).split("\n");
        return `  server.mjs:${before.length}  ${before[before.length - 1].trim()}`;
      }).join("\n"),
  );
});

test("内核把权威回执写进命令记录（否则 BFF 再对也没用）", () => {
  const kernel = readFileSync(path.join(REPO_ROOT, "services", "_kernel", "routes.py"), "utf-8");

  assert.ok(
    /record\["authority_receipt"\]\s*=/.test(kernel),
    "收执路径必须把回执落进命令记录：`record[\"authority_receipt\"] = {...}`。\n" +
      "只写 status/receipt_refs 时，视图里的 business_success 与 posting_status " +
      "会从 authority_receipt 取（取不到 ⇒ 恒为 False / NOT_CONFIRMED），" +
      "于是「已过账」永远不会显示。",
  );

  // 视图必须从 authority_receipt 派生结论——这是"只有权威回执能说已过账"的落点
  assert.ok(
    /_p3_command_view[\s\S]{0,900}authority_receipt/.test(kernel),
    "`_p3_command_view` 必须从 `authority_receipt` 派生 business_success / posting_status。",
  );
});
