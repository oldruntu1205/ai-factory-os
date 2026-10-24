"""进程间 Mock 存储 + 回执对账（P0 骨架）。

为什么需要：P0 要求「Mock 闭环可追踪」，而回执必须由**权威源**产生、再**回流**到
命令状态——不能由命令服务自己宣布成功。所以这里不共享内存，而是共享**追加式日志**：

    transaction-service  ──写 command──▶  commands.jsonl
    tool-gateway(adapter) ──写回执────▶  receipts.jsonl
    transaction-service  ──对账线程──▶  读 receipts.jsonl，推进命令状态

这模拟了真实形态（outbox + 对账 worker），也严格遵守"不得跨 owner 直接写表"：
两个服务各写自己的日志文件，互不写对方文件。

P0 明确留待后续阶段的能力（不在这里假装有）：
- outbox/inbox、broker、至少一次投递（§11）；
- Saga 与补偿（§12）；
- 持久化 DB、多实例 fencing token（P3）。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from typing import Any, Callable

from .registry import REPO_ROOT

_LOCK = threading.Lock()
DEFAULT_DIR = "services/.evidence/state"


def state_dir() -> pathlib.Path:
    configured = os.environ.get("V3_STATE_DIR")
    path = pathlib.Path(configured) if configured else REPO_ROOT / DEFAULT_DIR
    return path if path.is_absolute() else REPO_ROOT / path


def journal_path(name: str) -> pathlib.Path:
    return state_dir() / f"{name}.jsonl"


def append(name: str, record: dict[str, Any]) -> None:
    """追加一条记录。追加式写入天然幂等友好（读取侧按 id 去重）。"""
    path = journal_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read_all(name: str) -> list[dict[str, Any]]:
    path = journal_path(name)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with _LOCK:
        text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def read_latest(name: str, key: Callable[[dict[str, Any]], Any]) -> dict[Any, dict[str, Any]]:
    """按 key 折叠，后写的覆盖先写的（命令状态向后推进）。"""
    latest: dict[Any, dict[str, Any]] = {}
    for record in read_all(name):
        latest[key(record)] = record
    return latest


def reset() -> None:
    for name in ("commands", "receipts", "tasks", "approvals", "runs"):
        path = journal_path(name)
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------
# 对账：把权威回执回流到命令状态
# --------------------------------------------------------------------------

class Reconciler:
    """轮询 receipts 日志，把命令推进到 SUCCEEDED 并记录回执。

    有意做成"对账线程"而不是同步写回：上游要求命令的最终副作用状态由
    Transaction 依据**权威回执**决定，而不是由调用链上的某个服务宣布。
    """

    def __init__(self, *, interval: float = 0.2) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.applied: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="v3-reconciler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover - 对账故障不得影响服务存活
                print(f"[reconciler] 失败: {type(exc).__name__}")
            self._stop.wait(self.interval)

    def run_once(self) -> int:
        """返回本轮新应用的回执数。"""
        applied = 0
        receipts = read_all("receipts")
        for receipt in receipts:
            command_id = receipt.get("command_id")
            tenant_id = receipt.get("tenant_id")
            if not command_id or not tenant_id:
                continue
            token = f"{tenant_id}:{command_id}"
            if token in self.applied:
                continue
            if self._apply(tenant_id, command_id, receipt):
                self.applied.add(token)
                applied += 1
        return applied

    def _apply(self, tenant_id: str, command_id: str, receipt: dict[str, Any]) -> bool:
        from . import routes  # 局部导入避免循环依赖

        record = routes.find_command(tenant_id, command_id)
        if record is None:
            return False
        record["status"] = "SUCCEEDED"
        record["receipt_refs"] = [receipt["receipt_ref"]]
        record["source_system"] = receipt.get("source_system") or "mock-authoritative"
        record["updated_at"] = _utc_now()
        routes.persist_command(record)
        return True


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
