"""审计：生产模块里哪些**公共函数只被测试引用**。

上一轮事故的通用形态是「判据存在 ≠ 判据被使用」：
被改的是**调用点**，而测试测的是**函数本身**，于是判据静默失效。
本脚本找出同类风险的存量：有测试、却没有生产调用点的公共函数。
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

PROD_DIRS = ("services", "packages")
TEST_DIRS = ("tests", "tools", "scripts")
APP_DIRS = ("apps",)


def _py_files(dirs):
    out = []
    for name in dirs:
        base = ROOT / name
        if base.exists():
            out.extend(base.rglob("*.py"))
    return out


def _text(paths):
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in paths if p.exists())


def main() -> int:
    prod = _py_files(PROD_DIRS)
    others = _py_files(TEST_DIRS)
    apps = []
    for name in APP_DIRS:
        base = ROOT / name
        if base.exists():
            apps.extend(base.rglob("*.mjs"))
            apps.extend(base.rglob("*.js"))

    prod_text = _text(prod)
    other_text = _text(others)
    app_text = _text(apps)

    defined: dict[str, str] = {}
    for path in prod:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                defined[node.name] = path.relative_to(ROOT).as_posix()

    print(f"生产模块公共函数共 {len(defined)} 个")
    suspects = []
    for name, where in sorted(defined.items()):
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        prod_hits = len(pattern.findall(prod_text))
        other_hits = len(pattern.findall(other_text))
        app_hits = len(pattern.findall(app_text))
        # 生产里只出现一次 = 只有定义、没有调用
        if prod_hits <= 1 and (other_hits + app_hits) > 0:
            suspects.append((name, where, prod_hits, other_hits, app_hits))

    print(f"其中**只被测试/工具引用、生产代码内无调用**的：{len(suspects)} 个")
    print()
    for name, where, prod_hits, other_hits, app_hits in suspects:
        print(f"  {name}  ({where})")
        print(f"      生产引用={prod_hits}  测试/工具={other_hits}  前端={app_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
