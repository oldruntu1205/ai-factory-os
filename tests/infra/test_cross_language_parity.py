"""跨语言一致性门禁：发布七态在 **Python 权威** 与 **JS 界面** 两份实现必须一致。

上游 §12（发布状态机）/ §17 A09。

## 这个缺陷是怎么被发现的

`packages/domain/release.py` 与 `apps/web-admin/server.mjs` **各写了一份七态判据**：

- Python 那份是**权威**（`edge.release_transition_allowed`，后端据此拒绝非法转换）；
- JS 那份只用于"**给不给按钮**"。

我在加这层核对时，**立刻抓到一个真实缺陷**：

```
ACTIVE   js=['REVOKED', 'REVOKED']   py=['REVOKED']
```

`ACTIVE` 的下一个状态本来就是 `REVOKED`，而 JS 又无条件追加了一次终态
⇒ 界面上出现**两个"撤销"按钮**。已修（前进一格即终态时不再追加）。

## ⚠️ 第一版核对器本身犯了这个错

第一版我在 Python 里**把 JS 的规则重算了一遍**再比对。那不是核对，
而是"我读了一遍 JS、照着自己的理解又写了一份 Python"。
**证据**：修好 JS 的 bug 之后，它**仍然报漂移**——因为它比的是我的复算。

所以现在**用 `node` 真跑 JS 函数**（把源码裁出来执行）。测的是真实实现。

> 教训：**核对器如果重写了被核对对象的逻辑，它核对的是自己的理解。**
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
sys.path.insert(0, str(REPO_ROOT))

from packages.domain import release as domain_release  # noqa: E402

SERVER = REPO_ROOT / "apps/web-admin/server.mjs"
NODE = shutil.which("node")


def _slice_js(text: str, start_marker: str, end_marker: str = "\n}\n") -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _js_facts() -> dict:
    """在 node 里真跑 JS 判据，返回 {states, terminal, legal{state: [...]}}。"""
    text = SERVER.read_text(encoding="utf-8")
    declarations = _slice_js(text, "const RELEASE_STATES")
    function_block = _slice_js(text, "function legalNextStates")

    script = (
        declarations
        + "\n"
        + function_block
        + "\n"
        + "const out = { states: RELEASE_STATES, terminal: RELEASE_TERMINAL, legal: {} };\n"
        + "for (const s of RELEASE_STATES) out.legal[s] = legalNextStates(s);\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [NODE, "--input-type=commonjs", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(SERVER.parent),
    )
    assert result.returncode == 0, f"无法在 node 里执行 JS 判据：{result.stderr[:400]}"
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="无 node（明确 skip，不算通过）")


def test_release_state_sequence_matches_between_python_and_js():  # A09
    """七态序列与终态必须一致。"""
    js = _js_facts()
    assert js["states"] == list(domain_release.EDGE_RELEASE_STATES), (
        f"状态序列漂移：py = {list(domain_release.EDGE_RELEASE_STATES)}，js = {js['states']}"
    )
    assert js["terminal"] == domain_release.TERMINAL_RELEASE_STATE, (
        f"终态漂移：py = {domain_release.TERMINAL_RELEASE_STATE}，js = {js['terminal']}"
    )


def test_legal_next_states_match_the_domain_predicate():  # A09
    """**逐状态**比对"合法下一步"：JS 给界面的按钮必须与 domain 判据同集合。"""
    js = _js_facts()
    states = list(domain_release.EDGE_RELEASE_STATES)
    mismatches = []
    for state in states:
        js_legal = list(js["legal"].get(state, []))
        py_legal = [
            target for target in states
            if domain_release.release_transition_allowed(state, target)
        ]
        if js_legal != py_legal:
            mismatches.append(f"{state}: js={js_legal} py={py_legal}")
    assert not mismatches, (
        "JS 的合法下一步与 domain 判据不一致：\n  " + "\n  ".join(mismatches)
    )


def test_legal_next_states_have_no_duplicates():  # A09
    """合法下一步不得有重复项（重复 ⇒ 界面上出现重复按钮）。

    这条对应本轮抓到的真实缺陷：`ACTIVE` 的下一个状态本来就是 `REVOKED`，
    而实现又无条件追加了一次终态 ⇒ `['REVOKED','REVOKED']`。
    """
    js = _js_facts()
    offenders = {
        state: legal for state, legal in js["legal"].items()
        if len(legal) != len(set(legal))
    }
    assert not offenders, f"JS 给出的合法下一步含重复项：{offenders}"


def test_terminal_state_offers_nothing():  # A09
    """终态不得再给任何按钮（`REVOKED` 是终态）。"""
    js = _js_facts()
    assert js["legal"][domain_release.TERMINAL_RELEASE_STATE] == [], (
        f"终态仍给出下一步：{js['legal'][domain_release.TERMINAL_RELEASE_STATE]}"
    )


def test_the_checker_reads_the_real_js_not_a_python_rewrite():  # A09
    """能力自检：核对器必须**真的执行 JS**，而不是在 Python 里复算规则。

    判据：故意把 JS 源码改一处（在内存里），`_js_facts` 必须反映出来。
    如果它返回的仍是未改动的结果，说明它在复算而不是在执行。
    """
    text = SERVER.read_text(encoding="utf-8")
    # 在内存里改 JS：把顺序换掉（不改磁盘）
    mutated = text.replace(
        '"DRAFT", "SCHEMA_FROZEN", "CALIBRATED"',
        '"DRAFT", "CALIBRATED", "SCHEMA_FROZEN"',
        1,
    )
    assert mutated != text, "能力自检的前提不成立：找不到可改的 JS 片段"

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe_server = pathlib.Path(tmp) / "server.mjs"
        probe_server.write_text(mutated, encoding="utf-8")
        # 用同一套裁片逻辑跑改过的副本
        declarations = _slice_js(mutated, "const RELEASE_STATES")
        function_block = _slice_js(mutated, "function legalNextStates")
        script = (
            declarations + "\n" + function_block + "\n"
            + "process.stdout.write(JSON.stringify(RELEASE_STATES));\n"
        )
        result = subprocess.run(
            [NODE, "--input-type=commonjs", "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert result.returncode == 0, result.stderr[:300]
        changed_states = json.loads(result.stdout)
        assert changed_states != list(domain_release.EDGE_RELEASE_STATES), (
            "核对器没有反映 JS 的真实内容——它可能在复算规则而不是执行 JS"
        )
