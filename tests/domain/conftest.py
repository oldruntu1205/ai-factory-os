"""领域包测试夹具：把仓库根加入 sys.path。

`packages/` 位于仓库根，而 pytest 会把测试文件所在目录插到 `sys.path` 最前；
`tests/domain` 与 `packages/domain` 不同名（不构成遮蔽），但仍需显式加入仓库根。
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
