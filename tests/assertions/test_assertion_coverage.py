"""A01–A14 验收断言的**可执行覆盖门禁**（上游 §17）。

它回答一个具体问题：**"我们声称覆盖了 A01–A14"这句话能不能被机器验证？**

三道判据（任一不成立即失败）：
1. 清单引用的测试**必须真实存在**（改名/删测试即报红，杜绝"引用幽灵测试"）；
2. 每条断言的 `status` / `environment` / `gap` 必须显式给出
   （`partial` / `not_covered` 尤其要写清缺口，不许用沉默代替说明）；
3. `TAGGED_SUITES` 里**每个用例**都必须声明它支撑哪条断言（`# A01,A02` 形式）——
   新用例不许含糊其"到底在验证什么"。

第 3 条是这套门禁真正的价值：它把"测试"与"上游断言"绑在一起，
使"我们测了 600 多个用例"变成"每条硬断言都有可定位证据、每条缺口都有名字"。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import re
import subprocess

import pytest

from .assertion_inventory import ASSERTIONS, ASSERTION_STATUSES, TAGGED_SUITES, UNTAGGED_SUITES

_EXEMPT_PATHS = {entry[0].replace("\\", "/") for entry in UNTAGGED_SUITES}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REPORT_PATH = REPO_ROOT / "docs/v3-upgrade/evidence/A01-A14-断言覆盖报告.md"


def _load_report_renderer():
    """加载报告生成器（文件名 `render_assertion_report.py`，可直接按路径加载）。"""
    script = REPO_ROOT / "scripts" / "render_assertion_report.py"
    spec = importlib.util.spec_from_file_location("render_assertion_report", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_coverage_report_is_generated_and_not_stale():
    """覆盖报告**必须存在、且与断言清单一致**（不是手写的）。

    ## 为什么这条重要（一次真实漂移）

    原报告是手写的，于是**必然过期**：实测它写着"75 个引用节点"，
    而库存已涨到 95；A14 的缺口还写着「UI 面未接入（无前端）」——
    那早就不成立了（`apps/web-admin` 有 44 条显示契约用例）。

    **手写证据会漂移，而漂移的证据比没有证据更危险**：它看起来权威。
    所以报告改为生成物，并且**门禁核对它是否过期** —— 否则改了清单忘了重新生成，
    报告会继续以权威面目误导读者。
    """
    if not REPORT_PATH.exists():
        pytest.fail(
            f"覆盖报告不存在：{REPORT_PATH.relative_to(REPO_ROOT).as_posix()}；"
            "请运行 python scripts/render_assertion_report.py"
        )

    renderer = _load_report_renderer()
    expected = renderer.render()
    actual = REPORT_PATH.read_text(encoding="utf-8")

    if actual != expected:
        # 给出**可定位的差异**，而不是只说"不一致"
        expected_lines = expected.splitlines()
        actual_lines = actual.splitlines()
        diff_at = next(
            (
                index
                for index, (left, right) in enumerate(zip(expected_lines, actual_lines))
                if left != right
            ),
            min(len(expected_lines), len(actual_lines)),
        )
        detail = (
            f"首个差异在第 {diff_at + 1} 行：\n"
            f"  报告：{actual_lines[diff_at][:120] if diff_at < len(actual_lines) else '<缺行>'}\n"
            f"  应为：{expected_lines[diff_at][:120] if diff_at < len(expected_lines) else '<多行>'}"
        )
        pytest.fail(
            "覆盖报告与断言清单不一致（报告已过期）——"
            "请重新生成：\n  python scripts/render_assertion_report.py\n" + detail
        )



def _is_exempt(relative_path: str) -> bool:
    """该测试文件是否被显式豁免打标签？（支持整目录与单文件两种登记）"""
    relative_path = relative_path.replace("\\", "/")
    for entry in _EXEMPT_PATHS:
        if entry.endswith(".py"):
            if relative_path == entry:
                return True
        elif relative_path.startswith(entry + "/"):
            return True
    return False

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"

# 断言标签：`# A01` 或 `# A01,A02`（写在函数定义行尾，或紧邻函数定义上方）
TAG_RE = re.compile(r"#\s*(A\d{2}(?:\s*,\s*A\d{2})*)\s*$")


def _collected_node_ids() -> set[str]:
    """用 pytest 自己收集全部节点 ID——这是"测试是否存在"最可靠的判据。"""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(
        [str(VENV_PYTHON), "-m", "pytest", "tests", "--collect-only", "-q", "--no-header"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    found: set[str] = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if "::" in line and line.startswith("tests"):
            found.add(line)
    return found


@pytest.fixture(scope="module")
def collected() -> set[str]:
    nodes = _collected_node_ids()
    assert nodes, "未能收集到任何测试节点，覆盖门禁本身失效"
    return nodes


# ==========================================================================
# 判据 1：引用的测试必须真实存在
# ==========================================================================

def test_every_referenced_test_exists(collected: set[str]):
    """清单里引用的每个测试节点都必须能在收集结果里找到。

    这条挡住的是最常见也最隐蔽的造假：**引用一个不存在（或已改名）的测试**，
    文档上看着"有证据"，实际什么都没验证。

    参数化用例的收集结果带 `[参数]` 后缀，故按"前缀匹配"判定存在性。
    """
    exact = set(collected)
    bases = {node.split("[", 1)[0] for node in collected}

    missing: list[str] = []
    for assertion_id, entry in ASSERTIONS.items():
        for ref in entry["refs"]:
            if ref in exact or ref in bases:
                continue
            missing.append(f"{assertion_id} -> {ref}")
    assert not missing, "覆盖清单引用了不存在的测试：\n  " + "\n  ".join(missing)


def test_refs_are_not_duplicated_across_assertions_within_entry():
    """同一条断言里不得重复引用同一个测试（重复引用多半是复制粘贴错误）。"""
    for assertion_id, entry in ASSERTIONS.items():
        refs = list(entry["refs"])
        assert len(refs) == len(set(refs)), f"{assertion_id} 存在重复引用"


# ==========================================================================
# 判据 2：状态/环境/缺口必须显式给出
# ==========================================================================

def test_inventory_covers_all_fourteen_assertions():
    expected = {f"A{index:02d}" for index in range(1, 15)}
    assert set(ASSERTIONS) == expected, f"清单与上游断言集合不一致：{sorted(set(ASSERTIONS) ^ expected)}"


def test_status_and_environment_are_from_allowed_values():
    for assertion_id, entry in ASSERTIONS.items():
        assert entry["status"] in ASSERTION_STATUSES, f"{assertion_id} status 非法: {entry['status']}"
        assert isinstance(entry["environment"], str) and entry["environment"], (
            f"{assertion_id} 必须标明环境（mock / sandbox / production / not_covered）"
        )
        assert isinstance(entry["claim"], str) and entry["claim"].strip(), f"{assertion_id} 缺少断言描述"


def test_partial_and_uncovered_assertions_must_name_their_gap():
    """`partial` / `not_covered` 必须写出**具体缺口**，不许留空或用套话。"""
    for assertion_id, entry in ASSERTIONS.items():
        if entry["status"] in ("partial", "not_covered"):
            gap = str(entry.get("gap") or "").strip()
            assert gap, f"{assertion_id} 状态为 {entry['status']}，必须写明 gap"
            assert len(gap) >= 10, f"{assertion_id} 的 gap 描述过于笼统（至少 10 字）"


def test_covered_assertions_must_have_evidence():
    """`covered` 必须有至少一个真实引用；空引用只能是 `not_covered`。"""
    for assertion_id, entry in ASSERTIONS.items():
        refs = list(entry["refs"])
        if entry["status"] == "covered":
            assert refs, f"{assertion_id} 声称 covered 却没有任何引用"
        if not refs:
            assert entry["status"] == "not_covered", f"{assertion_id} 无引用时状态必须是 not_covered"


def test_no_assertion_claims_production_environment():
    """当前**没有任何**断言达到生产验收——不得把 mock 写成生产。

    上游：只在文档或单元 Mock 中通过不等于完成生产验收。
    """
    production = [key for key, entry in ASSERTIONS.items() if entry["environment"] == "production"]
    assert not production, f"以下断言声称已达到生产环境（当前不可能）：{production}"


# ==========================================================================
# 判据 3：关键套件里每个用例都必须声明支撑的断言
# ==========================================================================

def _assertion_tags(path: pathlib.Path) -> dict[str, set[str]]:
    """解析测试文件，返回 {函数名: {断言ID}}。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = path.read_text(encoding="utf-8").splitlines()
    tags: dict[str, set[str]] = {}

    def collect(function: ast.FunctionDef) -> None:
        if not function.name.startswith("test_"):
            return
        found: set[str] = set()
        # 函数定义行尾
        header = lines[function.lineno - 1]
        for match in TAG_RE.finditer(header):
            found.update(part.strip() for part in match.group(1).split(","))
        # 紧邻上方（含 docstring 之前）两行内
        for offset in (2, 3):
            index = function.lineno - 1 - (offset - 1)
            if index >= 0:
                for match in TAG_RE.finditer(lines[index]):
                    found.update(part.strip() for part in match.group(1).split(","))
        tags[function.name] = found

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            collect(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    collect(child)
    return tags


def test_every_test_in_tagged_suites_declares_its_assertion():
    """关键套件里的每个用例都必须带 `# Axx` 标签。

    这是本门禁最有价值的一条：它让新增用例无法"含糊通过"——
    要么你明确说它在验证哪条上游断言，要么门禁报红。
    """
    untagged: list[str] = []
    for suite in TAGGED_SUITES:
        directory = REPO_ROOT / suite
        if not directory.exists():
            continue
        for path in sorted(directory.glob("test_*.py")):
            if _is_exempt(f"{suite}/{path.name}"):
                continue
            for name, tags in _assertion_tags(path).items():
                if not tags:
                    untagged.append(f"{suite}/{path.name}::{name}")
    assert not untagged, (
        f"{len(untagged)} 个用例未声明它支撑的上游断言"
        "（请在函数定义行尾或上方加 `# Axx` 注释）：\n  " + "\n  ".join(untagged[:40])
    )


def test_tags_only_reference_known_assertions():
    """标签只能引用已登记的 A01–A14（防止写出 A15 这种不存在的断言）。"""
    known = set(ASSERTIONS)
    unknown: list[str] = []
    for suite in TAGGED_SUITES:
        directory = REPO_ROOT / suite
        if not directory.exists():
            continue
        for path in sorted(directory.glob("test_*.py")):
            if _is_exempt(f"{suite}/{path.name}"):
                continue
            for name, tags in _assertion_tags(path).items():
                for tag in tags:
                    if tag not in known:
                        unknown.append(f"{suite}/{path.name}::{name} -> {tag}")
    assert not unknown, "标签引用了未登记的断言：\n  " + "\n  ".join(unknown)


def test_declared_tags_match_inventory_refs(collected: set[str]):
    """标签与清单必须**互相印证**：清单说某测试支撑 A06，那个测试就必须标 `# A06`。

    这一条把两边的漂移堵死——只改一边就会被发现。
    """
    # 收集磁盘上所有标签
    tagged: dict[str, set[str]] = {}
    for suite in TAGGED_SUITES:
        directory = REPO_ROOT / suite
        if not directory.exists():
            continue
        for path in sorted(directory.glob("test_*.py")):
            if _is_exempt(f"{suite}/{path.name}"):
                continue
            relative = f"{suite}/{path.name}"
            for name, tags in _assertion_tags(path).items():
                tagged[f"{relative}::{name}"] = tags

    mismatches: list[str] = []
    for assertion_id, entry in ASSERTIONS.items():
        for ref in entry["refs"]:
            file_part, _, _function = ref.partition("::")
            if not file_part.endswith(".py"):
                continue
            if ref not in tagged:
                # 未在该套件的标签集合里（可能套件不在 TAGGED_SUITES）——由判据 1 负责
                continue
            if assertion_id not in tagged[ref]:
                # 一个用例可以支撑多条断言（多标签），因此只要求"包含"，
                # 不要求逐字相等——但要挡住"清单说 A06、用例却没标 A06"。
                mismatches.append(f"{assertion_id}: 清单引用了 {ref}，但该用例未标 `# {assertion_id}`")
    assert not mismatches, "清单与用例标签不一致：\n  " + "\n  ".join(mismatches)


def test_untagged_suites_are_explicitly_excluded():
    """未被要求打标签的套件必须**显式列出并写明理由**——
    避免"忘了加"被当成"不需要"。

    豁免不是"我不方便打标签"，而是"它守的东西不属于 A01–A14"。
    所以每条豁免都要给理由，且理由必须非空、可读。
    """
    all_files = {
        str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        for path in (REPO_ROOT / "tests").glob("*/test_*.py")
    }
    exempt = {entry[0] for entry in UNTAGGED_SUITES}

    # 理由必须非空——空理由等于没解释
    blank = [path for path, reason in UNTAGGED_SUITES if not (reason or "").strip()]
    assert not blank, f"以下豁免没写理由：{blank}"

    # 标签要求（TAGGED_SUITES）必须真的覆盖它下面的文件
    covered: set[str] = set()
    for suite in TAGGED_SUITES:
        covered |= {f for f in all_files if f.startswith(suite + "/")}
    # 单文件豁免与目录豁免都支持
    covered |= {
        entry
        for entry in exempt
        if entry.endswith(".py")
    }
    covered |= {
        f
        for f in all_files
        for entry in exempt
        if not entry.endswith(".py") and f.startswith(entry + "/")
    }

    missing = sorted(all_files - covered)
    assert not missing, (
        f"以下测试文件既不在 TAGGED_SUITES，也没有登记豁免理由：{missing}\n"
        "新增业务套件请加进 assertion_inventory.TAGGED_SUITES；"
        "若它守的不是 V3 行为断言，请加进 UNTAGGED_SUITES 并写明理由。"
    )

    # 豁免不得与标签要求**直接冲突**。
    # 注意：**单文件豁免落在带标签目录内不算冲突**——那正是"这个套件里
    # 有一个文件属于别的范畴"的表达方式（例如 p2_runtime 里的工程卫生守卫）。
    # 真正的冲突是同一条路径被整体要求打标签、又被整体豁免。
    conflicts = sorted(
        entry
        for entry in exempt
        if not entry.endswith(".py")
        and any(entry == suite or entry.startswith(suite + "/") for suite in TAGGED_SUITES)
    )
    assert not conflicts, f"这些路径同时被整体要求打标签又整体豁免（清单自相矛盾）：{conflicts}"
