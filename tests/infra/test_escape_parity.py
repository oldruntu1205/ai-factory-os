"""跨语言一致性门禁（二）：**转义实现**在 `view.js` 与页面内联脚本两份必须等价。

上游 §6 / §17 A14（后台显示契约）。

## 为什么这条重要

`apps/web-admin` 里有**两份 HTML 转义**：

| 位置 | 函数 |
| --- | --- |
| `src/view.js` | `escapeHtml(value)` |
| `public/index.html` 内联脚本 | `esc(value)` |

**转义是安全相邻的实现**：分叉意味着"某一侧少转一个字符"，
而那一侧恰好可能渲染不可信内容（如 `release_id`、错误消息、审批说明）。

## 实测发现的一处真实差异（2026-10-05）

| 输入 | `view.js::escapeHtml` | 页面 `esc` |
| --- | --- | --- |
| `null` | `"null"` | `""` |
| `undefined` | `"undefined"` | `""` |

原因：页面的 `esc` 有 `String(value ?? "")`，`view.js` 那份没有。
**后果**：页面把缺失值渲染成空白，`view.js` 渲染成字面量 `"null"` ——
用户会看到 `null` 这五个字符。

（严重性诚实评估：都是**转义过的**，所以**不是 XSS**；是显示缺陷。）

## 与上一轮同源的方法论

上一轮我用 `node` **真跑** JS 函数来核对发布状态机——因为"在 Python 里复算 JS 规则"
不是核对，而是"照自己的理解又写了一份"。这里沿用同一方法：
**两侧都执行真实的 JS**，比对五字符替换表与边界行为。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VIEW_JS = REPO_ROOT / "apps/web-admin/src/view.js"
PAGE_HTML = REPO_ROOT / "apps/web-admin/public/index.html"
NODE = shutil.which("node")


def _view_escape_source() -> str:
    text = VIEW_JS.read_text(encoding="utf-8")
    start = text.index("export function escapeHtml")
    end = text.index("\n}\n", start) + len("\n}\n")
    # 去掉 `export ` 以便在 node 的 -e 里直接执行
    return text[start:end].replace("export function", "function", 1)


def _page_escape_source() -> str:
    text = PAGE_HTML.read_text(encoding="utf-8")
    match = re.search(r"const esc = \(value\) =>\n(?:.*\n)*?.*?replaceAll\(\"'\", \"&#39;\"\);", text)
    assert match, "页面里找不到 `esc` 转义实现"
    return match.group(0)


def _run_js(source: str, probe_body: str) -> dict:
    """在 node 里执行给定源码 + 探针，返回解析后的 JSON。"""
    script = source + "\n" + probe_body + "\nprocess.stdout.write(JSON.stringify(OUT));\n"
    result = subprocess.run(
        [NODE, "--input-type=commonjs", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"node 执行失败：{result.stderr[:400]}"
    return json.loads(result.stdout)


#: 覆盖危险字符、边界值、Unicode、以及"看起来像实体/标签"的输入。
PROBES = [
    "<script>",
    "<",
    ">",
    "&",
    '"',
    "'",
    "&amp;",              # 已经是实体的：必须再转义 `&`
    "a<b>&c\"d'e",
    "</textarea>",
    "`",                   # 反引号：JS 模板字符串里的风险字符
    "javascript:alert(1)",
    "",                    # 空串
    "plain",
    "中文与 emoji 😀",
    "line\nbreak\ttab",
    "\\backslash",
]


def _tables() -> tuple[dict, dict]:
    body = (
        "const OUT = {};\n"
        "for (const probe of " + json.dumps(PROBES, ensure_ascii=False) + ") OUT[probe] = esc_or_escape(probe);\n"
    )
    view_source = _view_escape_source().replace("function escapeHtml", "function esc_or_escape", 1)
    page_source = _page_escape_source().replace("const esc =", "const esc_or_escape =", 1)
    return _run_js(view_source, body), _run_js(page_source, body)


pytestmark = pytest.mark.skipif(NODE is None, reason="无 node（明确 skip，不算通过）")


def test_both_escapers_replace_the_same_five_dangerous_characters():  # A14
    """两份实现必须都替换 `& < > " '`，且**结果相同**。"""
    view_table, page_table = _tables()
    mismatches = {
        probe: (view_table[probe], page_table[probe])
        for probe in PROBES
        if view_table[probe] != page_table[probe]
    }
    assert not mismatches, (
        "两份转义实现结果不一致（分叉意味着某一侧的行为不是设计出来的）：\n  "
        + "\n  ".join(f"{probe!r}: view={value[0]!r} page={value[1]!r}" for probe, value in mismatches.items())
    )


def test_escaping_actually_neutralizes_tags_and_entities():  # A14
    """**能力自证**：`<script>` 必须被转义成 `&lt;script&gt;`，`&` 必须变 `&amp;`。

    没有这条，"两份一致地不转义"也会通过上面的等价断言。
    """
    view_table, page_table = _tables()
    for label, table in (("view.js", view_table), ("page", page_table)):
        assert table["<script>"] == "&lt;script&gt;", f"{label}: <script> 未被转义"
        assert table["&"] == "&amp;", f"{label}: & 未被转义"
        assert table['"'] == "&quot;", f"{label}: \" 未被转义"
        assert table["'"] == "&#39;", f"{label}: ' 未被转义"
        # 已是实体的 `&amp;` 必须再转义 `&`，否则会二次解析
        assert table["&amp;"] == "&amp;amp;", f"{label}: 既有实体未被再转义"
        # 任何输出都不得再含裸 `<` 或裸 `>`
        for probe, escaped in table.items():
            assert "<" not in escaped or probe == "<", f"{label}: {probe!r} 转义后仍含裸 <"
            assert ">" not in escaped or probe == ">", f"{label}: {probe!r} 转义后仍含裸 >"


def test_the_two_escapers_agree_on_missing_values():  # A14
    """**缺失值（null / undefined）的行为必须一致**。

    这是本轮实测发现的真实差异：页面的 `esc` 有 `String(value ?? "")`，
    `view.js` 那份没有 ⇒ 前者渲染空白、后者渲染字面量 `"null"`。

    **严重性**：两侧都转义过，所以不是 XSS；是显示缺陷（用户看到 `null` 五个字符）。

    断言"一致"而不是"必须都变成空串"——因为 `escapeHtml` 的语义可能是
    "忠实呈现传入值"（含 `null`）。两个选择都行，**但两份必须一致**，
    否则行为就不是设计出来的，而是哪份先写对算哪份。
    """
    body_missing = (
        "const OUT = { nullValue: esc_or_escape(null), undefinedValue: esc_or_escape(undefined) };\n"
    )
    view_table = _run_js(
        _view_escape_source().replace("function escapeHtml", "function esc_or_escape", 1), body_missing
    )
    page_table = _run_js(
        _page_escape_source().replace("const esc =", "const esc_or_escape =", 1), body_missing
    )
    assert view_table == page_table, (
        "两份实现对缺失值处理不一致：\n"
        f"  view.js = {view_table}\n"
        f"  page    = {page_table}\n"
        "（都是转义过的，所以不是 XSS；但要一致，否则行为不是设计出来的）"
    )


def test_page_inline_script_escapes_every_interpolation_site():  # A14
    """页面在用 `innerHTML` 拼 HTML 时，插值必须**要么过 `esc(...)`、要么在豁免表里**。

    ## 这条守的是一个具体风险

    `innerHTML` + 未转义的插值 = 注入。页面里到处是模板字符串拼 HTML，
    只要有一处忘了 `esc`，就有一个真实缺口。

    ## 豁免要"有名字"，不能靠"看着安全"

    实测初版检查报出 3 处未转义插值。逐条核对后**全部安全**，但安全性的
    **证据强度不同**，所以处理方式也不同：

    | 插值 | 为什么安全 | 处理 |
    | --- | --- | --- |
    | `result.approved === true ? "通过" : "否决"` | 条件**只选字面量**（类型保证来自服务端 `normalizeDecision` 的强制布尔） | **改写为 `=== true`**，让"只选字面量"成为**局部可验证**的事实 |
    | `writeState` | 值在**构造处**已对唯一数据插值 `esc(...)` 过 | 写进豁免表并注明 |
    | `parts.join(" ")` | 只含**服务名常量**与字面量 | 写进豁免表并注明 |

    > 豁免必须逐条说明理由——否则"当时看着安全"会随代码演进而失效。
    """
    html = PAGE_HTML.read_text(encoding="utf-8")
    inline = html.split('<script type="module">', 1)[1].split("</script>", 1)[0]

    #: 允许“已转义/纯字面量”的插值 → 理由。**每条都要说清凭什么安全。**
    ALLOWED_UNESCAPED = {
        "badge(": "`badge()` 内部对 outcome/label 都调用了 `esc(...)`",
        "renderRows(": "`view.js` 的渲染函数内部已转义（不在本页内联脚本里）",
        'result.approved === true ? "通过" : "否决"': (
            "条件**两侧都是字面量**（「通过」/「否决」），没有任何数据插值；"
            "且已用 `=== true` 而非真值判断 —— 于是「只选字面量」是**局部可验证**的，"
            "不依赖别处（`normalizeDecision` 强制布尔）的类型保证"
        ),
        "writeState": (
            "**本函数上方构造的 HTML 片段**；其中唯一的数据插值 "
            "`data.admin.approverActor` 已在构造处 `esc(...)`。"
            "改用 `.textContent` 反而会显示标签"
        ),
        'parts.join(" ")': (
            "只含 `Object.entries(data.backends)` 的**键名**（服务名常量）"
            "与拼接字面量，不含后端可控文本"
        ),
    }

    assignments = re.findall(r"innerHTML\s*=\s*`((?:[^`\\]|\\.)*)`", inline, re.S)
    interpolations: list[str] = []
    for chunk in assignments:
        for expression in re.findall(r"\$\{([^{}]*)\}", chunk):
            expression = expression.strip()
            if expression:
                interpolations.append(expression)

    unescaped = [
        expression
        for expression in interpolations
        if "esc(" not in expression
        and not any(key in expression for key in ALLOWED_UNESCAPED)
    ]
    assert not unescaped, (
        f"页面有 {len(unescaped)} 处 innerHTML 插值没走转义、也不在豁免表里：\n  "
        + "\n  ".join(repr(e) for e in unescaped[:10])
        + "\n\n若确认安全，请加进 ALLOWED_UNESCAPED 并**写清凭什么安全**。"
    )


def test_exempted_interpolations_still_exist():  # A14
    """豁免项不得**过期**（那些插值还在不在）。

    否则豁免表会积累早已不存在的东西，读者再也分不清哪些还是真的在用。
    """
    html = PAGE_HTML.read_text(encoding="utf-8")
    inline = html.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    stale = [key for key in ("writeState", 'parts.join(" ")') if key not in inline]
    assert not stale, f"豁免表里的插值已不存在：{stale}——请清理，否则豁免表会退化成历史账"


def test_the_page_does_not_use_the_old_truthy_prefix_check():  # A14
    """`result.approved ? ...` 这种真值判断不得回退。

    已改为 `=== true`：让"只选字面量"成为**局部可验证**的事实，
    而不是依赖"别处的类型保证"（`normalizeDecision` 强制布尔）。
    """
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert "result.approved ?" not in html, (
        "页面回退成了真值判断 `result.approved ?`——"
        "那会让分支正确性依赖别处的类型保证；请用 `=== true`"
    )
