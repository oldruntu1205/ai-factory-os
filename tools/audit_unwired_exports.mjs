/**
 * 审计：`apps/` 里的导出**哪些在生产代码里没有被引用**。
 *
 * 与 Python 侧 `tests/infra/test_unwired_guards.py` 同一动机——
 * 2026-10-05 的事故（`approval.js` 留下 `if (false)` 把判据禁用）
 * 正发生在 `apps/`，而那一侧当时**没有**这类检测。
 *
 * 这里的"生产代码"包含：
 * - `apps/**` 里除 `test/` 之外的所有 JS（模块互调）；
 * - `apps/web-admin/public/index.html` 里内联的浏览器脚本
 *   （它用相对路径 import 这些模块——**这是最容易漏掉的消费者**）。
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const APPS = path.join(REPO_ROOT, "apps");

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

const all = walk(APPS);
const prodJs = all.filter(
  (f) => /\.(mjs|js)$/.test(f) && !f.includes(`${path.sep}test${path.sep}`),
);
const testJs = all.filter((f) => /\.(mjs|js)$/.test(f) && f.includes(`${path.sep}test${path.sep}`));
const html = all.filter((f) => f.endsWith(".html"));

const prodText =
  prodJs.map((f) => fs.readFileSync(f, "utf-8")).join("\n") +
  html.map((f) => fs.readFileSync(f, "utf-8")).join("\n");
const testText = testJs.map((f) => fs.readFileSync(f, "utf-8")).join("\n");

// 抓取 `export function name` / `export const name =` / `export { a, b }`
function exportedNames(text) {
  const names = new Set();
  for (const m of text.matchAll(/export\s+(?:async\s+)?function\s+(\w+)/g)) names.add(m[1]);
  for (const m of text.matchAll(/export\s+(?:const|let|var)\s+(\w+)/g)) names.add(m[1]);
  for (const m of text.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(",")) {
      const name = part.split(/\s+as\s+/).pop().trim();
      if (name) names.add(name);
    }
  }
  return [...names];
}

let total = 0;
const suspects = [];
for (const file of prodJs) {
  if (file.endsWith("server.mjs")) continue; // 入口：本身不被 import 是正常的
  const text = fs.readFileSync(file, "utf-8");
  for (const name of exportedNames(text)) {
    total += 1;
    const pattern = new RegExp(`\\b${name}\\b`, "g");
    const prodHits = (prodText.match(pattern) || []).length;
    const testHits = (testText.match(pattern) || []).length;
    // 生产里只出现一次 = 只有导出本身，没有消费者
    if (prodHits <= 1 && testHits > 0) {
      suspects.push({ file: path.relative(REPO_ROOT, file), name, prodHits, testHits });
    }
  }
}

console.log(`apps/ 生产模块导出共 ${total} 个`);
console.log(`其中**只被测试引用、生产侧无消费者**的：${suspects.length} 个`);
console.log();
for (const s of suspects) {
  console.log(`  ${s.name}  (${s.file})  生产引用=${s.prodHits} 测试=${s.testHits}`);
}
