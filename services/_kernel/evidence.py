"""轨迹证据落盘（P0 Mock 实现）。

上游要求「轨迹可验证：记录输入来源、策略和模型版本、批准者、执行回执及评测」。
P0 只做到**可追踪**：把每次业务调用的最小事实追加到本地 JSONL，供 L3 验收按
`tenant_id + task_id + trace_id` 串联。

有意不做的（后续阶段）：
- 不写 EventEnvelope（§11 的 outbox/inbox 与 broker 属 P2）；
- 不做分级对象存储与保留策略；
- 不记录任何正文，只记录引用与版本，避免把 C3/C4 正文写进普通日志。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from typing import Any

from .registry import REPO_ROOT

DEFAULT_EVIDENCE_PATH = "services/.evidence/evidence.jsonl"
_LOCK = threading.Lock()


def evidence_path() -> pathlib.Path:
    configured = os.environ.get("V3_EVIDENCE_PATH") or DEFAULT_EVIDENCE_PATH
    path = pathlib.Path(configured)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def append(record: dict[str, Any]) -> None:
    """追加一条证据。写入失败不影响业务响应，但要留下 stderr 痕迹。"""
    path = evidence_path()
    payload = {"recorded_at": _utc_now(), **record}
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:  # pragma: no cover - 磁盘异常
        print(f"[evidence] 写入失败 {path}: {exc}")


def read_all() -> list[dict[str, Any]]:
    path = evidence_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def reset() -> None:
    """仅供测试夹具清理。"""
    path = evidence_path()
    if path.exists():
        path.unlink()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
