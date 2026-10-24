"""契约哈希锁**不得分叉**：两份副本对同一文件必须给出同一把锁。

## 为什么需要（2026-10-05 亲手踩到）

本仓有两份哈希锁，各自登记了**同一批**契约文件：

| 文件 | 字典 | 它登记的集合 |
| --- | --- | --- |
| `tests/contracts/test_p3_contracts.py` | `BASELINE_SHA256` / `P3_SHA256` | 历史基线 + p3 |
| `tests/contracts/test_p4_contracts.py` | `BASELINE_SHA256` | **四阶段并集**（p0/p1/p15/p3，名字有误导性） |

本轮给 p3 加一个契约（`p3-employee.json`），结果**要改三处**：p3 的 `P3_SHA256`、
p4 的 `BASELINE_SHA256`、以及两边的计数。我为此连改了四轮才对齐。

**这正是本项目反复吃亏的"两套真相"。** 分叉的后果很隐蔽：
一边放行、另一边拒绝，或者两边都放行而**锁的值不同**（谁都没报错）。

所以这里加一条守卫：**同名文件的哈希必须一致**。
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
P3_TEST = REPO_ROOT / "tests/contracts/test_p3_contracts.py"
P4_TEST = REPO_ROOT / "tests/contracts/test_p4_contracts.py"


def _sha_dicts(path: pathlib.Path) -> dict[str, dict[str, str]]:
    """用 AST 取出文件里的 `*_SHA256 = {...}` 字面量字典（**不执行**测试模块）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id.endswith("SHA256")
                and isinstance(node.value, ast.Dict)
            ):
                pairs = {}
                for key, value in zip(node.value.keys, node.value.values):
                    if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
                        pairs[key.value] = value.value
                found[target.id] = pairs
    return found


def test_both_hash_lock_copies_agree_on_shared_entries():
    """两份副本对**同一文件**必须给出同一把锁。

    分叉会表现为"一边放行、另一边拒绝"，或者更糟——**两边都放行而值不同**。
    """
    p3_dicts = _sha_dicts(P3_TEST)
    p4_dicts = _sha_dicts(P4_TEST)

    def merged(dicts: dict[str, dict[str, str]]) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        for name, pairs in dicts.items():
            for relative, digest in pairs.items():
                result.setdefault(relative, (digest, name))
        return result

    left = merged(p3_dicts)
    right = merged(p4_dicts)

    disagreements = []
    for relative in sorted(set(left) & set(right)):
        left_digest, left_name = left[relative]
        right_digest, right_name = right[relative]
        if left_digest != right_digest:
            disagreements.append(
                f"{relative}: {P3_TEST.name}/{left_name}={left_digest[:12]}… "
                f"!= {P4_TEST.name}/{right_name}={right_digest[:12]}…"
            )
    assert not disagreements, (
        "两份哈希锁对同一文件给出了不同的值：\n  " + "\n  ".join(disagreements)
    )


def test_every_locked_file_actually_exists_and_matches():
    """每一把锁都要**对得上磁盘**——锁本身不得过期。

    现有测试已分别校验各自字典；这条补的是"两份合起来"的视角：
    只要有一边登记了某个文件，那个文件就必须真的在，且哈希等于登记值。
    """
    import hashlib

    contract_root = REPO_ROOT / "packages/contracts"
    stale, missing = [], []
    for source in (P3_TEST, P4_TEST):
        for name, pairs in _sha_dicts(source).items():
            for relative, digest in pairs.items():
                path = contract_root / relative
                if not path.is_file():
                    missing.append(f"{relative}（登记于 {source.name}/{name}）")
                    continue
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != digest:
                    stale.append(
                        f"{relative}：登记 {digest[:12]}… 实际 {actual[:12]}…"
                        f"（{source.name}/{name}）"
                    )
    assert not missing, "锁里登记了但磁盘上不存在的文件：\n  " + "\n  ".join(missing)
    assert not stale, "哈希锁已过期（改了契约没更新锁）：\n  " + "\n  ".join(stale)


def test_the_two_copies_are_actually_different_views():
    """能力自证：确认这两份**确实是两份**（而不是我误读成一个文件两处引用）。

    如果哪天两份被合并成一处，本守卫就失去意义——那时应当删掉它，
    而不是让它继续"永远通过"。
    """
    p3_dicts = _sha_dicts(P3_TEST)
    p4_dicts = _sha_dicts(P4_TEST)
    assert p3_dicts, f"{P3_TEST.name} 里应至少有一个 *_SHA256 字典"
    assert p4_dicts, f"{P4_TEST.name} 里应至少有一个 *_SHA256 字典"
    assert P3_TEST != P4_TEST and P3_TEST.is_file() and P4_TEST.is_file()
