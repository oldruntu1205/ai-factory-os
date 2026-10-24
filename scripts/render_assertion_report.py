"""从**权威源**重新生成 A01–A14 覆盖报告（上游 §17）。

## 为什么是生成器，而不是手写文档

原报告是手写的，于是**必然过期**：实测它写着"75 个引用节点"，
而实际库存已涨到 95；A14 的缺口还写着「UI 面未接入（无前端）」——
那早就不成立了，`apps/web-admin` 的显示契约有 44 条用例。

**手写证据会漂移，而漂移的证据比没有证据更危险**：它看起来权威。

所以这份报告改为**生成物**：
- 唯一输入是 `tests/assertions/assertion_inventory.py`（门禁核对的机器可读清单）；
- 生成过程无网络、无随机、无墙钟（时间戳由调用方经 `--timestamp` 显式传入）；
- 因此**同一份库存必然生成同一份报告**，可复现、可比对。

## 用法

```
python scripts/render_assertion_report.py > docs/v3-upgrade/evidence/A01-A14-断言覆盖报告.md
python scripts/render_assertion_report.py --check   # 只校验：报告是否与库存一致
```

`--check` 是给"报告过期了"这件事一个**可执行的判据**：
它对比生成结果与磁盘上的报告，不一致即 exit 1。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.assertions import assertion_inventory as inv  # noqa: E402

REPORT = REPO_ROOT / "docs" / "v3-upgrade" / "evidence" / "A01-A14-断言覆盖报告.md"

HEADER = """# A01–A14 验收断言覆盖报告

- 上游依据：`AI_Factory_V3.0_系统架构.md` §17「必须验证的验收断言」
- **权威源**：`tests/assertions/assertion_inventory.py`（门禁逐条核对）
- **本文件是生成物**：`python scripts/render_assertion_report.py > <本文件>`
  —— 请勿手改，改库存后重新生成（`--check` 可校验是否已过期）
- 执行门禁：`scripts/gate.py --stages assertions`（exit 0）
"""


def _escape(text: str | None) -> str:
    """把内容压成单行表格单元（管道符与换行都会破坏 Markdown 表）。"""
    if not text:
        return "—"
    return " ".join(str(text).split()).replace("|", "/")


def _summary_rows() -> list[tuple[str, str, int, str]]:
    rows = []
    for key in sorted(inv.ASSERTIONS):
        entry = inv.ASSERTIONS[key]
        refs = entry.get("refs") or []
        rows.append((key, str(entry.get("status")), len(refs), entry.get("gap")))
    return rows


def render() -> str:
    rows = _summary_rows()
    ref_total = sum(row[2] for row in rows)

    counted: dict[str, list[str]] = {}
    for key, status, _count, _gap in rows:
        counted.setdefault(status, []).append(key)

    lines: list[str] = [HEADER]
    lines.append("## 1. 总览")
    lines.append("")
    lines.append("| 状态 | 数量 | 断言 |")
    lines.append("| --- | --- | --- |")
    for status in sorted(counted):
        keys = counted[status]
        lines.append(f"| **{status}** | {len(keys)} | {' '.join(keys)} |")
    lines.append(f"| 引用的测试节点 | {ref_total} | — |")
    lines.append("")
    lines.append(f"- 机器可读清单条目：{len(inv.ASSERTIONS)}")
    lines.append(f"- 带断言标签的套件（TAGGED）：{len(inv.TAGGED_SUITES)}")
    lines.append(f"- 文件级豁免（UNTAGGED，均已写明理由）：{len(inv.UNTAGGED_SUITES)}")
    lines.append("")
    lines.append(
        "**环境均为 `mock`**（本地真实多进程 + 真实 HTTP + Mock 外部依赖）。"
        "没有任何一条达到 sandbox 或生产验收——门禁会拒绝把 mock 写成 production。"
    )

    lines.append("")
    lines.append("## 2. 逐条结论")
    lines.append("")
    lines.append("| 编号 | 断言（上游原文） | 状态 | 引用数 | 缺口 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for key, status, count, gap in rows:
        claim = _escape(inv.ASSERTIONS[key].get("claim"))
        lines.append(f"| {key} | {claim} | {status} | {count} | {_escape(gap)} |")

    lines.append("")
    lines.append("## 3. 逐条证据（由清单自动展开）")
    lines.append("")
    for key, status, _count, _gap in rows:
        entry = inv.ASSERTIONS[key]
        refs = entry.get("refs") or []
        lines.append(f"### {key} · {status}")
        lines.append("")
        lines.append(f"- 断言：{_escape(entry.get('claim'))}")
        lines.append(f"- 环境：{_escape(entry.get('environment'))}")
        if entry.get("gap"):
            lines.append(f"- 缺口：{_escape(entry.get('gap'))}")
        if refs:
            lines.append("- 证据节点：")
            for ref in refs:
                lines.append(f"  - `{ref}`")
        else:
            lines.append("- 证据节点：**无**（状态须为 not_covered，且缺口必须写明）")
        lines.append("")

    lines.append("## 4. 文件级豁免（不在 A01–A14 内，均已写明理由）")
    lines.append("")
    lines.append("| 文件 | 理由 |")
    lines.append("| --- | --- |")
    for path, reason in inv.UNTAGGED_SUITES:
        lines.append(f"| `{path}` | {_escape(reason)} |")
    lines.append("")

    lines.append("## 5. 这份报告的判据（不是自述）")
    lines.append("")
    lines.append("`tests/assertions/test_assertion_coverage.py` 逐条核对：")
    lines.append("")
    lines.append("| 门禁判据 | 挡住什么 |")
    lines.append("| --- | --- |")
    lines.append("| 清单引用的测试**必须真实存在** | 「引用幽灵测试」——文档上有证据、实际什么都没验 |")
    lines.append("| `covered` 必须有引用；空引用只能是 `not_covered` | 「声称覆盖但无证据」 |")
    lines.append("| `partial` / `not_covered` **必须写明具体缺口** | 用沉默代替说明 |")
    lines.append("| 关键套件**每个用例**必须声明支撑的断言（`# Axx`） | 新用例含糊其辞、无人知道它验的是什么 |")
    lines.append("| 清单与用例标签**互相印证** | 只改一边造成的漂移 |")
    lines.append("| **本报告与清单一致**（`--check`） | 报告过期后仍被当作权威 |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验 A01–A14 覆盖报告")
    parser.add_argument("--check", action="store_true", help="只校验报告是否与库存一致")
    parser.add_argument("--output", default=None, help="输出路径（默认写回仓库内报告）")
    args = parser.parse_args()

    rendered = render()
    target = pathlib.Path(args.output) if args.output else REPORT

    if args.check:
        if not target.exists():
            print(f"[过期] 报告不存在：{target}")
            return 1
        current = target.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "[过期] 覆盖报告与清单不一致 —— 请重新生成：\n"
                "  python scripts/render_assertion_report.py > "
                "docs/v3-upgrade/evidence/A01-A14-断言覆盖报告.md"
            )
            return 1
        print("[一致] 覆盖报告与断言清单一致")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"[已生成] {target.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
