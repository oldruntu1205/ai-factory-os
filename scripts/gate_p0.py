#!/usr/bin/env python
"""兼容入口：等价于 `scripts/gate.py --stages p0`。

保留此文件是为了不破坏既有引用（文档、习惯命令、CI）：
- 上游 P0 SPEC `docs/v3-upgrade/01-阶段-P0-契约与骨架.md` §9 里给出的命令，
  以及 P0 handoff 清单里的验证方式，都指向本文件名。
- 阶段门禁已并入 `scripts/gate.py`（P0/P1 阶段分离），本文件只做转发。

用法：
    backend\\.venv\\Scripts\\python.exe scripts\\gate_p0.py --evidence <文件>
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from gate import main as gate_main  # noqa: E402

if __name__ == "__main__":
    # 强制阶段为 p0；其余参数（如 --evidence）原样透传。
    argv = sys.argv[1:]
    if "--stages" not in argv:
        argv = ["--stages", "p0", *argv]
    sys.argv = [sys.argv[0], *argv]
    raise SystemExit(gate_main())
