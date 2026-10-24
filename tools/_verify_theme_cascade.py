"""验证主题 CSS 的级联结果，并证明"修好了"。

问题原状：`[data-color="green"]`（特异性 0,1,0）写在 `[data-theme="dark"]`（也是 0,1,0）**之后**
⇒ 后写的赢 ⇒ 夜间 + 绿色时，`--primary-hover` 取的是**亮色**值 `#15803d`（按钮 hover 变浅、发暗）。

本脚本按 CSS 级联规则（特异性优先；同特异性看**声明顺序**）算出每个组合的最终值，
并与期望值比对。
"""

from __future__ import annotations

import pathlib
import re
import sys

CSS = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src" / "index.css"

#: 期望的最终值：(mode, color) -> {变量: 值}
EXPECTED = {
    ("light", "blue"): {"--primary": "#2563eb", "--primary-hover": "#1d4ed8"},
    ("light", "green"): {"--primary": "#16a34a", "--primary-hover": "#15803d"},
    ("light", "purple"): {"--primary": "#9333ea", "--primary-hover": "#7e22ce"},
    ("light", "orange"): {"--primary": "#ea580c", "--primary-hover": "#c2410c"},
    ("dark", "blue"): {"--primary": "#3b82f6", "--primary-hover": "#60a5fa"},
    ("dark", "green"): {"--primary": "#22c55e", "--primary-hover": "#4ade80"},
    ("dark", "purple"): {"--primary": "#a855f7", "--primary-hover": "#c084fc"},
    ("dark", "orange"): {"--primary": "#f97316", "--primary-hover": "#fb923c"},
}


def parse_rules(text: str) -> list[tuple[str, dict[str, str]]]:
    """按出现顺序取出 (选择器, 变量表)。**只用顶层规则**，够本判据用。"""
    rules: list[tuple[str, dict[str, str]]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        selector = match.group(1).strip()
        if selector.startswith("@"):
            continue
        body = match.group(2)
        variables = dict(re.findall(r"(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);", body))
        if variables:
            rules.append((selector, {k: v.strip() for k, v in variables.items()}))
    return rules


def specificity(selector: str) -> tuple[int, int, int]:
    """够用的特异性计算：只处理属性选择器与 `:root`。

    `:root` 视为 (0,1,0)（伪类，等同于一个类）。
    `[a="b"]` 每个算 (0,1,0)。
    """
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector)) + len(re.findall(r"\[[^\]]+\]", selector))
    if selector.strip() == ":root":
        classes += 1
    elements = len(re.findall(r"(?<![\w.\-#\[])[a-zA-Z][\w-]*", selector.split("[")[0]))
    return (ids, classes, elements)


def resolve(rules, mode: str, color: str, variable: str) -> tuple[str | None, str | None]:
    """按级联算出最终值，返回 (值, 胜出选择器)。"""
    best: tuple[tuple[int, int, int], int, str, str] | None = None
    for order, (selector, variables) in enumerate(rules):
        if variable not in variables:
            continue
        # 该选择器是否匹配当前 <html data-theme data-color>
        applies = True
        for attr, value in re.findall(r'\[data-([\w-]+)="([^"]+)"\]', selector):
            if (attr == "theme" and value != mode) or (attr == "color" and value != color):
                applies = False
                break
        if not applies:
            continue
        spec = specificity(selector)
        key = (spec, order)
        if best is None or key > (best[0], best[1]):
            best = (spec, order, variables[variable], selector)
    if best is None:
        return None, None
    return best[2], best[3]


def main() -> int:
    rules = parse_rules(CSS.read_text(encoding="utf-8"))
    print(f"解析到 {len(rules)} 条含变量的顶层规则\n")

    failures = []
    for (mode, color), want in sorted(EXPECTED.items()):
        print(f"[{mode} / {color}]")
        for variable, expected in want.items():
            got, winner = resolve(rules, mode, color, variable)
            mark = "OK " if got == expected else "!! "
            print(f"  {mark}{variable:18s} = {got}   期望 {expected}   (来自 {winner})")
            if got != expected:
                failures.append(f"{mode}/{color} {variable}: 实际 {got} 期望 {expected}")

    print()
    if failures:
        print("结论：**级联结果与期望不符**")
        for f in failures:
            print("  -", f)
        return 1
    print("结论：8 个「明暗 × 主色」组合的最终值全部正确（暗色不再被亮色规则覆盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
