"""Jev 适配层门禁的夹具（上游 §15 `packages/decision/`）。

本组是**纯函数级**用例，不需要真实进程集群——但保留 `conftest.py`
是为了与 `packages/domain/` 的组织形态一致（那里也有一个）。
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

__all__ = ["REPO_ROOT"]
