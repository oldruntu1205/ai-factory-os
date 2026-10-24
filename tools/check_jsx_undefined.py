"""静态检查：JSX/JS 里调用了但**没定义也没导入**的标识符。

## 为什么需要这个脚本（这是真事故留下的）

2026-10-06 我在 `V3AIEmployees.jsx` 里用了 `plain()` 却忘了 import，
页面直接崩成「页面出现了一点小问题 / plain is not defined」。

**而 `vite build` 是通过的**——Vite 只做转译，**不解析标识符**。
所以我当时"构建通过 + dev server 转译 200"的验证结论，
**从原理上就查不出这类错**。这不是疏忽，是验证方法有洞。

## 它怎么做到准确

先**剥掉字符串与注释**（否则 `'var(--primary)'` 里的 `var(` 会被当成调用），
再收集：本文件定义的 / import 进来的 / 全局的，最后报剩下的。

只报**本项目自己的工具函数**（`v3Terms` 的导出）是因为那是新增的一层、
最容易漏 import；同时把其他疑似项一并列出供人判断。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(r"H:\制造业 AI 员工平台全量开发")
SRC = ROOT / "frontend" / "src"

# ⚠️ 必须允许跨行：多行 import（`import {\n  a,\n  b,\n} from '...'`）很常见。
# 第一版漏了 `[^;]` 与换行处理，导致**多行 import 全被跳过**——
# 于是 `applyTheme` 之类明明导入了却被报"未导入"（实测 ThemeSwitcher 就这样误报）。
# 更糟的是：那意味着本脚本**抓不到多行 import 场景下的真漏导入**。
IMPORT_RE = re.compile(r"import\s+([^;]+?)\s+from\s+['\"]([^'\"]+)['\"]", re.S)
EXPORT_RE = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)")
EXPORT_BRACE_RE = re.compile(r"export\s*\{([^}]*)\}")
CALL_RE = re.compile(r"(?<![\w.$])([a-z][\w$]*)\s*\(")
DEF_RE = re.compile(r"(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)")
# 解构赋值：`const { a, b } = ...` 与 `const [open, setOpen] = useState()`
# 漏了这个就会把 `setOpen(` 全报成未定义——实测误报 700+ 的主因。
DESTRUCTURE_RE = re.compile(r"(?:const|let|var)\s*[\{\[]([^\}\]]*)[\}\]]\s*=", re.S)
# 函数参数。
#
# ⚠️ 第一版写的是「`(` 参数 `)` 后面必须跟 `=>` 或 `{`」——那是错的：
# `const fn = (a, b) =>\n  body`（箭头后面换行）就匹配不到，
# 于是它匹配到别的位置、"参数"是个空串，靠**碰巧**把附近调用里的名字吸进结果。
# 证据：`list` 曾被判为已定义（其实也是参数），而 `setList` 没有——
# 两者本该同进同出，这种不一致正说明匹配错了地方。
#
# 正确做法：先定位函数头（`function 名` / `变量 =` / `名 (`），
# 再**剥壳配对**取出参数列表，不依赖参数后面是什么。
FUNC_HEAD_RE = re.compile(
    r"(?:function\s+[A-Za-z_$][\w$]*\s*|(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*|[A-Za-z_$][\w$]*\s*)"
)

GLOBALS = {
    "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "decodeURIComponent",
    "console", "fetch", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "require", "alert", "confirm", "prompt", "structuredClone", "queueMicrotask",
    "requestAnimationFrame", "cancelAnimationFrame", "btoa", "atob", "Number", "String",
    "Boolean", "Array", "Object", "Promise", "Symbol", "Date", "Math", "JSON", "Set", "Map",
    "Error", "RegExp", "BigInt", "WeakMap", "WeakSet", "Intl", "URL", "URLSearchParams",
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "await", "new",
    "delete", "void", "in", "of", "do", "else", "try", "finally", "throw", "case", "yield",
}


def strip_strings_and_comments(source: str) -> str:
    """把字符串、模板串、注释替换成等长空白，保留换行以便行号不变。"""
    out = list(source)
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        # 行注释
        if char == "/" and nxt == "/":
            while index < length and source[index] != "\n":
                out[index] = " "
                index += 1
            continue
        # 块注释
        if char == "/" and nxt == "*":
            while index < length and not (source[index] == "*" and index + 1 < length and source[index + 1] == "/"):
                if source[index] != "\n":
                    out[index] = " "
                index += 1
            if index < length:
                out[index] = out[index + 1] if index + 1 < length else " "
                index += 2
            continue
        # 单/双引号、模板串
        if char in "'\"`":
            quote = char
            out[index] = " "
            index += 1
            while index < length and source[index] != quote:
                if source[index] == "\\":
                    out[index] = " "
                    index += 1
                    if index < length:
                        out[index] = " "
                        index += 1
                    continue
                if source[index] != "\n":
                    out[index] = " "
                index += 1
            if index < length:
                out[index] = " "
                index += 1
            continue
        index += 1
    return "".join(out)


def imported_names(source: str) -> set[str]:
    names: set[str] = set()
    for clause, _module in IMPORT_RE.findall(source):
        clause = clause.strip()
        default = re.match(r"^([A-Za-z_$][\w$]*)", clause)
        if default:
            names.add(default.group(1))
        braces = re.search(r"\{(.*)\}", clause, re.S)
        if braces:
            for part in braces.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                alias = re.search(r"\bas\s+([A-Za-z_$][\w$]*)", part)
                names.add(alias.group(1) if alias else part.split(":")[0].strip())
    return names


def our_exports() -> set[str]:
    """本项目的工具模块导出了哪些名字（用于判断"用了但没导入"）。"""
    names: set[str] = set()
    for path in SRC.rglob("*.js"):
        if path.name.endswith((".test.js", ".spec.js")):
            continue
        source = path.read_text(encoding="utf-8")
        names |= set(EXPORT_RE.findall(source))
        for group in EXPORT_BRACE_RE.findall(source):
            for part in group.split(","):
                part = part.strip()
                if not part:
                    continue
                alias = re.search(r"\bas\s+([A-Za-z_$][\w$]*)", part)
                names.add(alias.group(1) if alias else part.split(":")[0].strip())
    return names


def _looks_like_params(text: str) -> bool:
    """参数列表必须**长得像参数列表**。

    ## 为什么必须有这个把关（这是第二次修同一个洞留下的教训）

    第一版剥壳配对在括号不匹配时会从某个 `(` 一路配到**文件末尾**，
    于是整个函数体被当成"参数列表" ⇒ `plain` 这种被调用的名字
    就"被定义"了 ⇒ 检查器报 0 个问题。

    **那次"干净"是假的**：负向验证（故意删掉 import 再跑）没能报出来，
    才发现检查器把自己骗了。所以这里加一道形状判据：
    参数列表只可能由 标识符 / 解构 / 默认值 / 逗号 / 空白 / 注释 组成，
    出现 `{`、`;`、`=>`、`<`、字符串残留 等就说明**不是参数列表**。
    """
    if len(text) > 600:
        return False
    allowed = re.compile(r"^[\s\w$,.=\[\]\{\}\(\):'\"\-]*$")
    if not allowed.match(text):
        return False
    # 真正的参数列表不会出现这些
    for bad in ("=>", ";", "{", "}", "<", ">", "?"):
        if bad in text:
            return False
    return True


def _param_lists(source: str) -> list[str]:
    """取出所有函数头后面的参数列表（剥壳配对 + 形状把关）。

    扫描每个 `(`，若它前面紧跟的是"函数头形状"（`function 名`、
    `变量 =`、或 `名 (` 这样的方法简写），就把配对的 `)` 之间当参数列表——
    但**只有形状像参数列表时才接受**（见 `_looks_like_params`）。
    """
    lists: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        if source[index] != "(":
            index += 1
            continue
        # 回头看一小段，判断是不是函数头。
        # 必须容纳 `async`（`const fn = async (a, b) => …`）——漏了它
        # `onUrl`（async 函数的参数）就会被误报为未定义，实测如此。
        head = source[max(0, index - 60) : index]
        is_func_head = (
            re.search(r"function\s*\*?\s*[A-Za-z_$][\w$]*\s*$", head) is not None
            or re.search(r"=\s*(?:async\s*)?$", head) is not None
            or re.search(r"^(?:\s*)(?:async\s+)?[A-Za-z_$][\w$]*\s*$", head.split("\n")[-1]) is not None
        )
        if not is_func_head:
            index += 1
            continue
        # 剥壳配对
        depth = 0
        cursor = index
        while cursor < length:
            if source[cursor] == "(":
                depth += 1
            elif source[cursor] == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        candidate = source[index + 1 : cursor]
        if _looks_like_params(candidate):
            lists.append(candidate)
        index = cursor + 1
    return lists


def bound_names(source: str) -> set[str]:
    """本文件里被绑定的名字：定义、解构、**函数参数**、import。"""
    names: set[str] = set(DEF_RE.findall(source))
    for group in DESTRUCTURE_RE.findall(source):
        for part in group.split(","):
            part = part.strip()
            if not part:
                continue
            # `a: alias` / `a = default` / 普通名字
            part = part.split("=")[0].strip()
            alias = re.search(r":\s*([A-Za-z_$][\w$]*)", part)
            names.add(alias.group(1) if alias else part)
    for group in _param_lists(source):
        for part in group.split(","):
            part = part.strip()
            if not part:
                continue
            # 解构参数 `{ a, b }` / `[x, y]`：里面的标识符都算绑定
            for identifier in re.findall(r"[A-Za-z_$][\w$]*", part.split("=")[0]):
                names.add(identifier)
    return names


def main() -> int:
    ours = our_exports()
    files = sorted(list(SRC.rglob("*.jsx")) + list(SRC.rglob("*.js")))
    problems: list[str] = []
    checked = 0

    # JS 语法关键字/类成员方法：不是"调用未定义标识符"
    noise = {
        "async", "await", "constructor", "super", "render", "static", "get", "set",
        "componentDidMount", "componentDidUpdate", "componentWillUnmount", "shouldComponentUpdate",
        "getDerivedStateFromError", "componentDidCatch", "getSnapshotBeforeUpdate",
        "if", "for", "while", "switch", "catch", "return", "typeof", "function", "new",
        "delete", "void", "in", "of", "do", "else", "try", "finally", "throw", "case",
    }

    for path in files:
        raw = path.read_text(encoding="utf-8")
        source = strip_strings_and_comments(raw)
        known = imported_names(raw) | bound_names(source) | GLOBALS | noise
        checked += 1

        for match in CALL_RE.finditer(source):
            name = match.group(1)
            if name in known:
                continue
            line_no = source[: match.start()].count("\n") + 1
            line = raw.splitlines()[line_no - 1].strip()
            marker = "**未导入**" if name in ours else "未定义"
            problems.append(f"{path.relative_to(ROOT)}:{line_no}  {marker} `{name}(`  →  {line[:88]}")

    print(f"扫描 {checked} 个 JS/JSX 文件")
    if problems:
        print(f"\n发现 {len(problems)} 处：")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("未发现未定义调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
