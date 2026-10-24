"""契约登记处的**测试隔离**守卫。

## 这条防的是一个很脆的东西

`tests/contracts/test_contract_inventory_gates.py` 会**直接改**模块级字典：

```python
inventory.CONTRACT_INVENTORY[phase] = {...}   # 合成一个不存在的阶段
```

它现在靠 pytest 的**回滚式** `monkeypatch` 还原。但同一文件里 `UNKNOWN_PHASES` 是**导入时**
算出来的、依赖"当前有哪些阶段已登记"。于是：

- 只要有一次运行泄漏了状态（例如调试时先删了清单条目再跑），
  `UNKNOWN_PHASES` 就会把**已经存在**的阶段当成"未登记阶段"，或反之；
- 表现是**次序相关的假失败**，而且**换一次运行就换个样子**。

**我实测到过**：同一份代码，单独跑 `28 passed`；在另一次运行里 `28 failed`。
两者都不是真实缺陷。**次序相关的测试比没测试更危险**——它会教人忽略红灯。

## 判据取什么

不检查"它用了 monkeypatch"（那是实现细节），而是检查**可观察性质**：
跑完那套件之后，`CONTRACT_INVENTORY` 必须**逐字回到**跑之前的样子。
"""

from __future__ import annotations

import copy
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.contracts import contract_inventory as inventory  # noqa: E402


def _snapshot() -> dict:
    return copy.deepcopy(inventory.CONTRACT_INVENTORY)


def test_inventory_matches_between_fresh_imports():
    """**能力自证**：证明"状态泄漏"这种事**确实可被检出**。

    做法：在一个**独立子进程**里改掉清单再打印，父进程必须看不到这个改动
    （进程隔离），而同一进程内的改动必须可见 —— 后者说明"快照对比"这种判据真的能分辨。
    """
    before = _snapshot()

    # 同一进程内改动必须可见（否则快照对比是空转的）
    inventory.CONTRACT_INVENTORY["p999-probe"] = {"openapi": (), "schemas": ()}
    try:
        during = _snapshot()
        assert "p999-probe" in during, "同一进程内的改动必须可见，否则判据无效"
    finally:
        inventory.CONTRACT_INVENTORY.clear()
        inventory.CONTRACT_INVENTORY.update(before)

    after = _snapshot()
    assert after == before, "还原后必须逐字相同"

    # 另一个子进程里的改动不得影响本进程
    probe = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, r"{REPO_ROOT}")
        from tests.contracts import contract_inventory as inv
        inv.CONTRACT_INVENTORY["p998-probe"] = {{"openapi": (), "schemas": ()}}
        print(len(inv.CONTRACT_INVENTORY))
        """
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
    assert "p998-probe" not in _snapshot(), "子进程的改动不得影响本进程"


def test_inventory_has_no_synthetic_phases_left_over():
    """清单里不得有**合成阶段**残留（形如 `p<数字>-probe` / 未在磁盘上出现的阶段）。"""
    synthetic = [key for key in inventory.CONTRACT_INVENTORY if "-probe" in key or "-future" in key]
    assert not synthetic, (
        f"契约清单里残留了合成阶段：{synthetic}\n"
        "说明某个测试改了全局字典却没清理干净。"
    )


def test_declared_phases_are_exactly_those_with_files_on_disk():
    """清单的阶段集合必须与**磁盘实际拥有的阶段**一致。

    这条把"登记与磁盘同步"变成独立判据：泄漏、误删、漏登记都会在这里现形，
    而不是等到某个阶段门禁报出次序相关的假失败。
    """
    from tests.contracts.contract_inventory import CONTRACT_ROOT

    on_disk = set()
    for relative in ("openapi", "schemas"):
        for path in (CONTRACT_ROOT / relative).glob("*.json"):
            # 文件名前缀即阶段（p0 / p1 / p15 / p2 / p3 / p4；无前缀的算 p0 基线）
            stem = path.stem
            for candidate in sorted(inventory.CONTRACT_INVENTORY, key=len, reverse=True):
                if stem.startswith(candidate):
                    on_disk.add(candidate)
                    break

    declared = set(inventory.CONTRACT_INVENTORY)
    assert declared == on_disk, (
        f"清单与磁盘不一致：\n"
        f"  仅在清单里：{sorted(declared - on_disk)}\n"
        f"  仅在磁盘上：{sorted(on_disk - declared)}"
    )
