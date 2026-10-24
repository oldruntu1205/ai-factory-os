"""测试用 SQLite 库路径：**只写临时目录，绝不污染仓库根**。

## 为什么需要它

`backend/tests/` 下多个验收脚本原先这么写：

```python
os.environ["SQLITE_PATH"] = f"./test_mcp_{os.getpid()}.db"
```

相对路径 `./` 是**进程工作目录**——从仓库根跑 pytest 时，
每个用例进程都在仓库根拉出一个测试库。实测累积到 **45 个文件 / 20.95MB**。

更关键的是**清理位置不对**：清理写在 `main()` 的末尾，
而 pytest 导入这个模块时**不会执行 `main()`**（由 `__main__` 守卫挡住），
于是被 pytest 收集执行时**永远不清理**。

## 这个模块做什么

1. 把库路径指到**系统临时目录**（`tempfile.gettempdir()`），与仓库根解耦；
2. 用 `atexit` 注册清理——**无论正常结束、断言失败还是异常退出都会跑**；
3. 顺手清理**上次同一 PID 崩溃**留下的同名残留（PID 复用是良性的，清掉正是我们想要的）。

## 用法

```python
from _test_sqlite import isolate_sqlite
_TEST_DB = isolate_sqlite("test_mcp")
```
"""

from __future__ import annotations

import atexit
import os
import pathlib
import tempfile
import time

# 启动清扫的年龄阈值：只清"明显是上一次留下的"库，避免删掉**正在跑的**其它用例的库。
# 用例通常在秒级完成，10 分钟前的同前缀库不可能是活的。
#
# **为什么清扫是必需的**：`atexit` 只在正常退出时执行；进程被强杀（超时、
# 任务管理器、管道被提前关闭）时它不会跑。更常见的是——Windows 上 SQLite
# 句柄还没释放，`unlink` 会失败（被 `_remove_quietly` 静默吞掉），
# 于是本次的库也留到下一轮才被清掉。
STALE_AFTER_SECONDS = 600


def isolate_sqlite(prefix: str) -> str:
    """把 `SQLITE_PATH` 指到临时目录，并保证退出时清理。返回该路径。"""
    target = pathlib.Path(tempfile.gettempdir()) / f"{prefix}_{os.getpid()}.db"

    # 上次同 PID 崩溃留下的残留（PID 复用是良性的，清掉正是我们想要的）
    for suffix in ("", "-wal", "-shm"):
        _remove_quietly(pathlib.Path(f"{target}{suffix}"))
    _sweep_stale(prefix)

    os.environ["USE_SQLITE"] = "1"
    os.environ["SQLITE_PATH"] = str(target)

    def _cleanup() -> None:
        # SQLite 可能还留着 WAL/SHM 边车文件，一并清掉
        for suffix in ("", "-wal", "-shm"):
            _remove_quietly(pathlib.Path(f"{target}{suffix}"))

    atexit.register(_cleanup)
    return str(target)


def _sweep_stale(prefix: str) -> None:
    """清掉**上一次运行被硬终止**留下的同前缀库。

    `atexit` 只在正常退出/异常时执行；进程被强杀（超时、任务管理器）
    时它不会跑，于是临时目录仍会攒垃圾。启动时扫一遍既简单又安全：
    只看**同前缀**、且**明显过期**的文件。
    """
    cutoff = time.time() - STALE_AFTER_SECONDS
    try:
        candidates = pathlib.Path(tempfile.gettempdir()).glob(f"{prefix}_*")
    except OSError:
        return
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                _remove_quietly(candidate)
        except OSError:
            continue


def _remove_quietly(path: pathlib.Path) -> None:
    """尽力删除；失败不抛（Windows 上句柄未释放时删除会失败，属正常）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
