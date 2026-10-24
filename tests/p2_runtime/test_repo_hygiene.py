"""仓库卫生守卫：测试不得把产物写进仓库。

## 为什么需要

`backend/tests/` 下 5 个验收脚本原先写：

```python
os.environ["SQLITE_PATH"] = f"./test_mcp_{os.getpid()}.db"
```

相对路径是**进程工作目录**。从仓库根跑 pytest 时，每个用例进程都在仓库根
拉出一个测试库——实测累积到 **45 个文件 / 20.95MB**。

而且清理写在 `main()` 末尾，被 pytest 导入时**根本不执行**，
所以这不是"偶发漏清"，而是**结构性的**。

## 这条守卫守两件事

1. **回归**：任何新增/修改的测试都不得再把 `SQLITE_PATH` 之类的持久化路径
   设成相对路径或指向仓库内；
2. **基线**：仓库根已有的遗留库**只减不增**——禁不了历史，但不能继续长。

基线是显式登记的：新增文件会让门禁报红，逼人去看是不是又漏了清理。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 允许写文件的目录（测试夹具、临时产物都在这些地方）
WRITABLE_UNDER = ("backend/tests", "tests", "scripts", "docs", "output")

# 会打开可写文件句柄的环境变量：值不得是相对路径，也不得指向仓库内。
PERSISTENCE_ENV_VARS = (
    "SQLITE_PATH",
    "SQLITE_DB",
    "DATABASE_URL",
    "V3_STATE_DIR",
    "V3_EVIDENCE_DIR",
)

ASSIGNMENT = re.compile(
    r"""os\.environ\[\s*["'](?P<var>%s)["']\s*\]\s*=\s*(?P<value>.+)$"""
    % "|".join(PERSISTENCE_ENV_VARS),
    re.MULTILINE,
)


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts and "node_modules" not in path.parts
    )


def _string_spans(text: str) -> list[tuple[int, int, int, int]]:
    """所有字符串字面量的位置跨度 `(start_row, start_col, end_row, end_col)`。

    为什么需要它：守卫有两个**互相拉扯**的需求——

    - "检测字符串内容里是否写了相对路径" ⇒ 字符串必须**可见**；
    - "别匹配到文档字符串里引用的反面示例" ⇒ 文档字符串必须**被排除**。

    抹掉字符串就丢掉第一条，保留字符串就违反第二条。
    所以我先试了两种"预处理文本"的做法，都是死路（实测各踩一次）。

    正确的做法是**保留原文**，用 token 跨度判断某个匹配**是否落在字符串里**。
    """
    import io
    import tokenize

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError):
        return []
    return [
        (token.start[0], token.start[1], token.end[0], token.end[1])
        for token in tokens
        if token.type == tokenize.STRING
    ]


def _position_of(text: str, offset: int) -> tuple[int, int]:
    """字符偏移 → 1-based `(行, 列)`。"""
    row = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    return row, offset - line_start


def _inside_string(row: int, col: int, spans: list[tuple[int, int, int, int]]) -> bool:
    """位置 `(row, col)` 是否落在某个字符串字面量内部（1-based 行列）。

    判据按**字符串的起止列**比较，而不是"行号落在起止行之间"：

    - 单行字符串：`start_col <= col < end_col`；
    - 跨行字符串的首行：`col >= start_col`；
    - 跨行字符串的末行：`col < end_col`。

    第一版写成"`start_row == row` 或 `end_row == row` 就算"，
    于是**同一行上更靠前的代码**（赋值的左半部分）也被判成"在字符串里"——
    诊断脚本实测：`match=(2,0)` 被 `span=(2,11,2,24)` 判成 inside=True，
    探针文件里的赋值整行被排除，守卫从此探测不到。
    """
    for start_row, start_col, end_row, end_col in spans:
        if start_row == row == end_row:
            if start_col <= col < end_col:
                return True
            continue
        if start_row == row:
            if col >= start_col:
                return True
            continue
        if end_row == row:
            if col < end_col:
                return True
            continue
        if start_row < row < end_row:
            return True
    return False


def test_repo_root_has_no_test_database_debris():
    """仓库根不得有遗留的测试数据库（`test_*.db` 之类）。

    基线：本次修复时清点到的遗留文件按前缀登记在这里。
    它们**只减不增**——新增一个就说明又有测试在往仓库根写。
    """
    baseline: dict[str, int] = {}  # 修复后应为空；当前遗留见下面的 allowed 集合
    allowed = {
        "test_mcp": 45,
    }
    found: dict[str, int] = {}
    for path in REPO_ROOT.glob("test_*.db"):
        prefix = re.sub(r"_\d+\.db$", "", path.name)
        found[prefix] = found.get(prefix, 0) + 1

    for prefix, count in found.items():
        limit = allowed.get(prefix, baseline.get(prefix, 0))
        assert count <= limit, (
            f"仓库根新增了 {count - limit} 个 {prefix}_*.db："
            "测试又把持久化路径写到仓库里了（用 backend/tests/_test_sqlite.py 的 isolate_sqlite）"
        )


def _scan_relative_persistence(text: str) -> list[str]:
    """扫描一段代码里**指向相对目录的持久化赋值**，返回违规描述。

    主守卫与自检**共用这一个函数**——否则两处各写一份正则，
    变异测试会指出"哪一份才算数"的歧义（实测：两行完全相同的正则，
    变异只改到一份，于是自检形同虚设）。
    判据只有一份，改坏了任何一处都会被自检当场发现。
    """
    spans = _string_spans(text)
    hits: list[str] = []
    for match in ASSIGNMENT.finditer(text):
        if _inside_string(*_position_of(text, match.start()), spans):
            continue  # 落在文档字符串里（引言中引用的反面示例）
        value = match.group("value").strip()
        # 只看字面量相对路径。注意**必须容忍字符串前缀**：
        # 实际写法是 `f"./x.db"`，引号前面还有个 `f`——
        # 第一版正则要求引号紧邻等号右侧，于是被 `f` 前缀挡住、
        # 探针文件实测验证过「守卫探测不到」。
        if re.search(r"""[fFrRbBuU]{0,3}["']\.\.?[/\\]""", value):
            hits.append(f"{match.group('var')} = {value[:60]}")
    return hits


def test_test_scripts_never_point_persistence_at_relative_paths():
    """测试脚本不得把持久化路径设成相对路径（`./x.db`）。

    相对路径 = **进程工作目录**，从仓库根跑就落进仓库。
    """
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders += [f"{path.relative_to(REPO_ROOT)}: {hit}" for hit in _scan_relative_persistence(text)]
    assert not offenders, (
        "持久化路径指向相对目录（会落到进程工作目录，通常就是仓库根）：\n  "
        + "\n  ".join(offenders)
        + "\n用 tempfile 得到绝对路径（参见 backend/tests/_test_sqlite.py）。"
    )


def test_test_scripts_never_point_persistence_inside_the_repository():
    """持久化路径也不得**指向仓库内**的绝对路径（同样会留下产物）。"""
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        spans = _string_spans(text)
        for match in ASSIGNMENT.finditer(text):
            if _inside_string(*_position_of(text, match.start()), spans):
                continue
            value = match.group("value").strip()
            # 断言脚本里若出现仓库根常量的拼接，就是指向仓库内
            if re.search(r"REPO_ROOT|PROJECT_ROOT|BASE_DIR", value):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: {match.group('var')} = {value[:60]}"
                )
    assert not offenders, (
        "持久化路径指向仓库内（会在仓库留下产物）：\n  " + "\n  ".join(offenders)
    )


def test_isolate_sqlite_helper_is_used_by_the_scripts_that_need_it():
    """曾经写相对路径的那批脚本必须改用共享 helper —— 不许各写一份。"""
    helper = REPO_ROOT / "backend" / "tests" / "_test_sqlite.py"
    assert helper.exists(), "共享 helper 缺失：backend/tests/_test_sqlite.py"

    scripts = (
        "test_api.py",
        "test_channels.py",
        "test_hermes.py",
        "test_marketplace.py",
        "test_hermes_mcp_scheduler_audit.py",
    )
    for name in scripts:
        path = REPO_ROOT / "backend" / "tests" / name
        assert path.exists(), f"脚本缺失：{name}"
        text = path.read_text(encoding="utf-8")
        assert "isolate_sqlite" in text, f"{name} 未使用 isolate_sqlite（会重新污染仓库）"


# ==========================================================================
# 守卫的**自检**：保证它真的能探测到，而不是"因为没人写相对路径所以全绿"
# ==========================================================================
#
# 变异测试暴露过这个盲区：把上面那条相对路径正则改成 `if False`，
# 门禁**照样全绿**——因为仓库里当时没有任何相对路径写法，
# 守卫"探测能力为零"与"恰好没东西可探测"在观测上无法区分。
#
# 所以这里喂一份**构造的**样本给守卫用到的**同一套判据**，
# 断言它必须判定为违规。这样守卫的能力本身就被守住了。


def _detect_relative_persistence(text: str) -> list[str]:
    """自检入口：直接复用主守卫的判据函数（**不另写一份**）。"""
    return _scan_relative_persistence(text)


def test_guard_actually_detects_a_relative_persistence_path():
    """**能力自检**：给守卫喂一段已知违规的代码，它必须报出来。

    没有这条，守卫可能在"探测能力为零"的情况下长期绿灯——
    那比没有守卫更危险（给人一种"有人在看着"的错觉）。

    **样本必须混入一条合法赋值**：第一版样本里三条全是违规项，
    于是"完全不判断"（把正则检查改成 `if False`）照样返回 3 条、照样通过——
    自检形同虚设。混一条合法项后，不做判断必然多报，自检才有判别力。
    """
    sample = (
        "import os, tempfile, pathlib\n"
        'os.environ["DATABASE_URL"] = "postgresql://localhost/db"\n'   # 合法（非相对路径）
        'os.environ["SQLITE_PATH"] = f"./test_probe_{os.getpid()}.db"\n'  # 违规
        'os.environ["V3_STATE_DIR"] = r".\\state"\n'                      # 违规
    )
    hits = _detect_relative_persistence(sample)
    assert len(hits) == 2, f"守卫应当**正好**报出 2 处相对路径违规（不得多报合法项），实际 {hits}"
    assert all("DATABASE_URL" not in hit for hit in hits), f"合法赋值被误报：{hits}"


def test_guard_does_not_flag_absolute_temp_paths():
    """反向自检：**绝对临时路径不得被误报**（否则会逼人加豁免、最终失去守卫）。"""
    sample = (
        "import os, tempfile, pathlib\n"
        'os.environ["SQLITE_PATH"] = str(pathlib.Path(tempfile.gettempdir()) / "x.db")\n'
        'os.environ["V3_EVIDENCE_DIR"] = "/var/evidence"\n'
    )
    assert _detect_relative_persistence(sample) == []


def test_guard_ignores_examples_inside_docstrings():
    """**误报自检**：文档字符串里引用的反面示例不得被当成真实违规。

    本文件开头引言与 `_test_sqlite.py` 都引用了旧的错误写法，
    若不排除字符串内部，守卫会自伤（实测踩过）。
    """
    sample = (
        '"""引言：原来的写法是 os.environ["SQLITE_PATH"] = "./old.db" —— 反例。"""\n'
        "import os\n"
    )
    assert _detect_relative_persistence(sample) == []
