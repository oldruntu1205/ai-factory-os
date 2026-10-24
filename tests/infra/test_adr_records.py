"""ADR 记录的结构门禁（上游 §15 `docs/adr/`）。

## 这条门禁守什么、不守什么

**守**：ADR 的**结构**——编号格式、必备章节、状态取值封闭、索引完整。
没有这些，ADR 会退化成随手写的散文：状态写成"大概吧"、
选了什么都找不到、写了不登记索引等于没人会读。

**不守**：内容对不对。门禁不能替代评审，它只能说清"该写的东西写没写"。
这一点必须写明，否则会给人"ADR 已被机器验证"的错觉。

## 为什么状态必须是封闭枚举

状态是 ADR 的**唯一机器可读结论**。如果允许随手写，
"已接受"就不再是一个可依赖的事实——而本目录存在的意义正是
"当初这个决定是什么，现在还算不算数"。
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"

# 封闭枚举：与 README 的说明逐字对齐
ALLOWED_STATUSES = (
    "已接受",
    "已接受（延后）",
    "已接受（临时）",
    "已被取代",
)

REQUIRED_SECTIONS = (
    "## 背景",
    "## 决策",
    "## 后果",
    "## 备选方案与不选的理由",
)

FILENAME = re.compile(r"^(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")


def _adr_files() -> list[pathlib.Path]:
    return sorted(p for p in ADR_DIR.glob("*.md") if p.name != "README.md")


def test_adr_directory_exists_with_records():
    assert ADR_DIR.exists(), "docs/adr 缺失（上游 §15 布局要求）"
    files = _adr_files()
    assert files, "docs/adr 里没有任何决策记录"
    # 本项目的实际规模：22 轮里做过多处有长期影响的取舍，至少要有一批
    assert len(files) >= 5, f"决策记录过少（{len(files)} 篇）——多半是没把已做的取舍写下来"


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_adr_filename_follows_the_numbering_convention(path):
    assert FILENAME.match(path.name), (
        f"{path.name} 不符合 `NNNN-<kebab-case>.md`——"
        "编号是 ADR 之间互相引用的凭据，格式不稳就无法引用"
    )


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_adr_has_every_required_section(path):
    text = path.read_text(encoding="utf-8")
    missing = [section for section in REQUIRED_SECTIONS if section not in text]
    assert not missing, f"{path.name} 缺少章节：{missing}"


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_adr_has_a_header_with_status_and_date(path):
    head = path.read_text(encoding="utf-8").splitlines()[:8]
    joined = "\n".join(head)
    assert joined.lstrip().startswith("# "), f"{path.name} 缺少一级标题"
    assert "状态" in joined, f"{path.name} 头部缺少「状态」"
    assert "日期" in joined, f"{path.name} 头部缺少「日期」"
    assert re.search(r"\d{4}-\d{2}-\d{2}", joined), f"{path.name} 头部缺少 YYYY-MM-DD 形式的日期"


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_adr_status_is_from_the_closed_enum(path):
    """状态必须是**封闭枚举**里的一个，不能随手写。

    这是本门禁最实质的一条：状态是 ADR 唯一可机读的结论，
    允许自由文本等于让"已接受"失去约束力。
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^-\s*状态：\s*(.+?)\s*$", text, re.MULTILINE)
    assert match, f"{path.name} 找不到 `- 状态：…` 行"
    status = match.group(1).strip().strip("*")
    assert status in ALLOWED_STATUSES, (
        f"{path.name} 的状态 {status!r} 不在允许取值内：{ALLOWED_STATUSES}"
    )


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_adr_records_the_upstream_basis(path):
    """每篇 ADR 都要写清**上游依据或外部约束**。

    没有依据的"决策"只是偏好，没法在评审时被检验，也没法判断何时该推翻它。
    """
    text = path.read_text(encoding="utf-8")
    assert "上游依据" in text or "外部约束" in text, (
        f"{path.name} 未写明上游依据/外部约束——没有依据的决策无法被检验"
    )


def test_adr_numbers_are_unique():
    numbers = [FILENAME.match(p.name).group(1) for p in _adr_files()]
    assert len(numbers) == len(set(numbers)), f"ADR 编号重复：{numbers}"


def test_index_lists_every_adr():
    """索引必须列出每一篇——否则写了也没人找得到。

    这是"文档可发现性"的机械判据：不登记索引的 ADR 等于不存在。
    """
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in _adr_files() if p.name not in index]
    assert not missing, f"以下 ADR 未登记在 README 索引里：{missing}"


def test_index_and_records_agree_on_status():
    """索引里的状态必须与各篇正文一致。

    两处状态必然会分叉，而分叉的表现是"索引说已接受、正文说已延后"——
    看的人无从判断该信哪个。
    """
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    mismatches: list[str] = []
    for path in _adr_files():
        body_status = re.search(r"^-\s*状态：\s*(.+?)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
        assert body_status
        status = body_status.group(1).strip().strip("*")
        # 索引行的形式：| [0001](0001-xxx.md) | 标题 | 状态 | 一句话 |
        row = re.search(rf"\|\s*\[[^\]]+\]\({re.escape(path.name)}\)\s*\|\s*[^|]*\|\s*([^|]+?)\s*\|", index)
        if row and row.group(1).strip() != status:
            mismatches.append(f"{path.name}: 索引={row.group(1).strip()!r} 正文={status!r}")
    assert not mismatches, "索引与正文的状态不一致：\n  " + "\n  ".join(mismatches)


# ==========================================================================
# 运行手册（上游 §15 `docs/runbooks/`）
# ==========================================================================
#
# 手册与 ADR 的守点不同：ADR 守"决策有没有成文且状态可机读"，
# 手册守"**是否可执行且可被发现**"——写成散文的"手册"没人能在故障时用上。

RUNBOOK_DIR = REPO_ROOT / "docs" / "runbooks"
RUNBOOK_NAME = re.compile(r"^(\d{2})-(.+)\.md$")


def _runbook_files() -> list[pathlib.Path]:
    if not RUNBOOK_DIR.exists():
        return []
    return sorted(p for p in RUNBOOK_DIR.glob("*.md") if p.name != "README.md")


def test_runbook_directory_exists_with_manuals():
    assert RUNBOOK_DIR.exists(), "docs/runbooks 缺失（上游 §15 布局要求）"
    assert _runbook_files(), "docs/runbooks 里没有任何手册"


@pytest.mark.parametrize("path", _runbook_files(), ids=lambda p: p.name)
def test_runbook_filename_is_numbered(path):
    assert RUNBOOK_NAME.match(path.name), (
        f"{path.name} 不符合 `NN-<名称>.md`——编号让故障时能按顺序找"
    )


@pytest.mark.parametrize("path", _runbook_files(), ids=lambda p: p.name)
def test_runbook_has_actionable_sections(path):
    """手册必须能被**照着做**。

    - **命令块**：没有命令的手册只是文章，故障时照着做不了（所有手册都要求）；
    - **「现象 → 原因/处理」**：只有**排障类**手册必须有——
      流程类手册（"怎么加一条接口"）本来就按步骤走，硬套一节"常见问题"
      只会写出凑数的内容，反而降低手册的可信度。
    """
    text = path.read_text(encoding="utf-8")
    assert "```" in text, f"{path.name} 没有可执行的命令块——故障时照着做不了"

    # 排障类：标题含"定位/问题/故障"，或正文出现"现象""常见问题"
    is_troubleshooting = any(
        word in path.name for word in ("定位", "问题", "故障")
    ) or "常见问题" in text or "现象" in text
    if is_troubleshooting:
        assert "常见问题" in text or "现象" in text, (
            f"{path.name} 是排障类手册，必须有「现象 → 原因/处理」一节"
        )


@pytest.mark.parametrize("path", _runbook_files(), ids=lambda p: p.name)
def test_runbook_commands_state_the_working_directory(path):
    """命令里出现的仓库路径必须**先交代工作目录**。

    手册最常见的失效方式就是"在错误的工作目录下执行"——
    同样一条 `backend\\.venv\\Scripts\\python.exe …` 在别处跑就会报路径不存在。
    """
    text = path.read_text(encoding="utf-8")
    uses_venv = "backend\\.venv" in text or "backend/.venv" in text
    if not uses_venv:
        return
    has_cd = "cd " in text
    has_absolute = "H:\\" in text or "H:/" in text
    assert has_cd or has_absolute, (
        f"{path.name} 用了仓库 venv 却没交代工作目录（`cd …` 或绝对路径）——"
        "在错误目录下执行是手册最常见的失效方式"
    )


def test_runbook_index_lists_every_manual():
    index = (RUNBOOK_DIR / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in _runbook_files() if p.name not in index]
    assert not missing, f"以下手册未登记在 README 索引里：{missing}"


def test_runbook_index_states_the_serial_gate_rule():
    """索引必须写明"门禁必须串行跑"这条铁律。

    它是本项目**实际误判过两次**的坑（并发抢端口 ⇒ 假失败，
    其中一次还被归因成代码缺陷），因此必须出现在最容易被看到的地方。
    """
    index = (RUNBOOK_DIR / "README.md").read_text(encoding="utf-8")
    assert "串行" in index, "runbooks 索引未写明「门禁必须串行跑」这条铁律"
    assert "墙钟" in index, "runbooks 索引未写明「判定函数不得读墙钟」这条铁律"
