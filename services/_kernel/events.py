"""事件一致性核心（P1.5，上游 §11）。

上游 §11 的硬规则，逐条对应到本模块的实现：

| 规则 | 实现位置 |
| --- | --- |
| 事件是"已发生事实"，命令才是"请求执行动作" | `EventEnvelope` 只由权威 adapter/owner 在取得凭证后发布（见 `publish` 调用点） |
| **至少一次投递，不承诺全局 exactly-once** | `Outbox.deliver()` 允许重复；正确性由消费侧 inbox 去重收敛 |
| Producer 把自身状态与 outbox 放**同一事务** | `commit_with_state()`：状态与事件一次 `os.replace` 落盘 |
| Consumer 在自身事务内更新投影与 inbox 去重 | `Inbox.consume()`：去重记录 + 版本收敛在同一次调用内完成 |
| **跨库不共享事务** | 只做本 owner 的原子提交；跨 owner 用事件引用，不开跨库事务 |
| 同一 tenant+aggregate 按版本排序 | `Inbox` 按 `aggregate_version` 单调收敛 |
| 重复忽略、缺版本进补拉队列 | `ConsumeOutcome.DUPLICATE` / `GAP` |
| **迟到事件不覆盖新事实** | `ConsumeOutcome.LATE`，不写投影 |
| DLQ 保留错误原因、次数、人工处理状态；重放仍走原去重规则 | `DeadLetterQueue`：`attempts` / `reason` / `status`，重放复用 `Inbox` |

P0 已给出权威契约（`packages/contracts/schemas/event-envelope.json`）：
16 个必填字段 + `x-delivery`（`at_least_once` / `outbox` / `inbox` /
`aggregate_key=[tenant_id, aggregate_id]` / `late_event_overwrite=false` /
`executed_edge_action_replay=false`）。本模块**不重新定义** Envelope，只实现语义。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import journal
from .registry import REPO_ROOT

# ---- 与 P0 event-envelope.json 的 event_type 枚举逐字一致 -----------------
EVENT_TYPES = (
    "task.accepted",
    "run.waiting_approval",
    "approval.decided",
    "command.accepted",
    "command.succeeded",
    "command.unknown",
    "saga.compensation_required",
    "erp.demand.released",
    "pmc.schedule.published",
    "mes.operation.completed",
    "wms.stock.changed",
    "edge.decision.observed",
    "edge.action.executed",
    "release.evaluated",
)

ENVELOPE_REQUIRED = (
    "event_id",
    "event_type",
    "schema_version",
    "tenant_id",
    "factory_id",
    "source_system",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "occurred_at",
    "received_at",
    "trace_id",
    "causation_id",
    "correlation_id",
    "data_class",
    "payload",
)

# 与 P0 x-delivery 逐项一致；作为断言常量，避免实现悄悄偏离契约。
DELIVERY_SEMANTICS = "at_least_once"
LATE_EVENT_OVERWRITE = False
EXECUTED_EDGE_ACTION_REPLAY = False


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def aggregate_key(envelope: dict[str, Any]) -> tuple[str, str]:
    """P0 `x-delivery.aggregate_key`：tenant_id + aggregate_id。"""
    return (envelope["tenant_id"], envelope["aggregate_id"])


class EventError(ValueError):
    """事件结构或语义错误（fail-closed：不发布、不应用）。"""


def validate_envelope(envelope: dict[str, Any]) -> None:
    """结构校验：任何缺失或不合法都拒绝，不"尽力而为"。"""
    for key in ENVELOPE_REQUIRED:
        if key not in envelope:
            raise EventError(f"事件缺少必填字段: {key}")
    if envelope["event_type"] not in EVENT_TYPES:
        raise EventError(f"未登记的事件类型: {envelope['event_type']}")
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise EventError("payload 必须是对象（受控引用，不得是原文）")
    for key in ("payload_ref", "payload_hash"):
        if not payload.get(key):
            raise EventError(f"payload 缺少 {key}（正文不得进入事件）")
    if not isinstance(envelope["aggregate_version"], int) or envelope["aggregate_version"] < 0:
        raise EventError("aggregate_version 必须是非负整数")


# ==========================================================================
# Outbox：生产者把状态与事件在同一事务里落盘
# ==========================================================================

class Outbox:
    """事务性发件箱。

    **事务边界（本阶段用文件系统模拟，但原子性是真的）**：
    状态与事件序列化进**同一个**提交文件，先写临时文件，再用 `os.replace`
    一次性换到正式路径。`os.replace` 在同一卷上是原子的 ——
    因此读者要么看到"状态 + 全部事件"，要么什么都看不到；
    不存在"状态改了但事件丢了"或"事件发了但状态没改"。

    真实实现应是"状态表与 outbox 表在同一 DB 事务里 INSERT"（P2/P3）。
    本阶段把语义立起来，并让它在真实进程里可被故障注入验证。

    故障注入：`V3_OUTBOX_FAIL_BEFORE_COMMIT=1` 模拟"提交前崩溃"，
    此时临时文件残留、正式文件不存在 —— 这正是要断言的可观察状态。
    """

    def __init__(self, *, state_name: str, event_name: str = "outbox") -> None:
        self.channel = f"delivery/{state_name}"
        self.event_name = f"delivery/{event_name}"
        self._pending_state: dict[str, Any] | None = None
        self._pending_events: list[dict[str, Any]] = []
        self._delivered: list[str] = []

    # ---- 写路径 ---------------------------------------------------------

    def stage_state(self, record: dict[str, Any]) -> None:
        self._pending_state = record

    def stage_event(self, envelope: dict[str, Any]) -> None:
        validate_envelope(envelope)
        self._pending_events.append(envelope)

    def commit(self) -> dict[str, Any]:
        """一次提交：状态与事件同时生效。**没有 stage 任何东西就不产生副作用。**"""
        if self._pending_state is None and not self._pending_events:
            return {"committed": False, "reason": "nothing_staged"}

        commit_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        payload = {
            "commit_id": commit_id,
            "committed_at": utc_now(),
            "state": self._pending_state,
            "events": list(self._pending_events),
        }
        target = _commit_dir() / f"{self.channel.replace('/', '_')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # 同一目录下的临时文件 → 原子 rename（保证同卷）
        tmp = target.with_suffix(f".tmp-{commit_id}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")

        if os.environ.get("V3_OUTBOX_FAIL_BEFORE_COMMIT") == "1":
            # 模拟"提交前崩溃"：临时文件保留、正式文件不动。调用方必须认为**什么都没发生**。
            raise RuntimeError("injected crash before atomic commit")

        os.replace(tmp, target)  # 原子切换点

        self._pending_state = None
        self._pending_events = []
        return {
            "committed": True,
            "commit_id": commit_id,
            "state_records": 1 if payload["state"] else 0,
            "event_records": len(payload["events"]),
        }

    # ---- 读路径 ---------------------------------------------------------

    def committed(self) -> dict[str, Any] | None:
        """读取当前**已提交**的投递记录。临时文件不算（未提交就不存在）。"""
        target = _commit_dir() / f"{self.channel.replace('/', '_')}.json"
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    def committed_events(self) -> list[dict[str, Any]]:
        record = self.committed()
        return list(record["events"]) if record else []

    # ---- 投递（至少一次）-------------------------------------------------

    def deliver(self, *, event_id: str, delivery_ref: str) -> dict[str, Any]:
        """标记投递。至少一次：**同一事件可以被投递多次**，去重责任在消费侧。"""
        events = self.committed_events()
        if not any(item["event_id"] == event_id for item in events):
            return {"delivered": False, "event_id": event_id, "reason": "event_not_in_committed_outbox"}
        journal.append(
            f"{self.event_name}-delivery",
            {
                "event_id": event_id,
                "delivery_ref": delivery_ref,
                "delivered_at": utc_now(),
                "semantics": DELIVERY_SEMANTICS,
            },
        )
        self._delivered.append(event_id)
        return {"delivered": True, "event_id": event_id, "delivery_ref": delivery_ref}


# ==========================================================================
# Inbox：消费者去重 + 版本收敛
# ==========================================================================

class ConsumeOutcome(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"      # 同 event_id 重复到达 → 忽略
    LATE = "late"               # 版本落后于已应用 → 不覆盖新事实
    GAP = "gap"                 # 版本跳跃 → 进补拉队列，先不应用


@dataclass
class ConsumeResult:
    outcome: ConsumeOutcome
    applied_version: int | None = None
    detail: str = ""
    queued_for_backfill: bool = False


class Inbox:
    """收件箱：投影与去重记录在同一"事务"内更新（本阶段同一次调用内）。"""

    def __init__(self, *, projection_name: str) -> None:
        self.projection_name = f"projection/{projection_name}"
        self.inbox_name = f"inbox/{projection_name}"
        self.backfill_name = f"backfill/{projection_name}"

    def applied_versions(self, key: tuple[str, str]) -> dict[str, Any] | None:
        latest = journal.read_latest(
            self.projection_name, lambda item: (item["tenant_id"], item["aggregate_id"])
        )
        return latest.get(key)

    def has_seen(self, event_id: str) -> bool:
        """是否**已应用**过该事件（缺版本进补拉队列不算已应用，否则补拉回来的事件会被误判为重复）。"""
        return any(item.get("event_id") == event_id for item in journal.read_all(self.inbox_name))

    def is_queued(self, event_id: str) -> bool:
        return any(item.get("event_id") == event_id for item in journal.read_all(self.backfill_name))

    def consume(self, envelope: dict[str, Any]) -> ConsumeResult:
        validate_envelope(envelope)
        key = aggregate_key(envelope)
        event_id = envelope["event_id"]
        version = envelope["aggregate_version"]

        # 1) 重复：同 event_id 已**应用**过 → 忽略（不重复产生副作用）
        if self.has_seen(event_id):
            return ConsumeResult(ConsumeOutcome.DUPLICATE, detail="event_id already applied")

        current = self.applied_versions(key)
        # 基线是 -1（从未应用过任何版本），所以第一个版本必须是 0 或 1 才连续；
        # 用 0 当基线会把"版本从 1 开始"的合法流永远判成缺版本（实测踩过）。
        current_version = current["aggregate_version"] if current else -1

        # 2) 迟到：版本落后 → **不覆盖新事实**（P0 x-delivery.late_event_overwrite=false）
        if version <= current_version:
            journal.append(
                self.inbox_name,
                {
                    "event_id": event_id,
                    "tenant_id": envelope["tenant_id"],
                    "aggregate_id": envelope["aggregate_id"],
                    "aggregate_version": version,
                    "outcome": ConsumeOutcome.LATE.value,
                    "received_at": utc_now(),
                },
            )
            return ConsumeResult(ConsumeOutcome.LATE, applied_version=current_version, detail="late event discarded")

        # 3) 缺版本：跳跃 → 进补拉队列，先不应用。
        #    注意：**不写 inbox 的 applied 记录**，否则补拉回来时会被误判为重复。
        if version > current_version + 1:
            if self.is_queued(event_id):
                return ConsumeResult(
                    ConsumeOutcome.GAP,
                    applied_version=current_version,
                    detail="already queued for backfill",
                    queued_for_backfill=True,
                )
            journal.append(
                self.backfill_name,
                {
                    "event_id": event_id,
                    "tenant_id": envelope["tenant_id"],
                    "aggregate_id": envelope["aggregate_id"],
                    "aggregate_version": version,
                    "expected_version": current_version + 1,
                    "queued_at": utc_now(),
                },
            )
            return ConsumeResult(
                ConsumeOutcome.GAP,
                applied_version=current_version,
                detail=f"expected version {current_version + 1}",
                queued_for_backfill=True,
            )

        # 4) 正常应用：投影与去重记录一起写
        journal.append(
            self.inbox_name,
            {
                "event_id": event_id,
                "tenant_id": envelope["tenant_id"],
                "aggregate_id": envelope["aggregate_id"],
                "aggregate_version": version,
                "outcome": ConsumeOutcome.APPLIED.value,
                "received_at": utc_now(),
            },
        )
        journal.append(
            self.projection_name,
            {
                "tenant_id": envelope["tenant_id"],
                "aggregate_id": envelope["aggregate_id"],
                "aggregate_type": envelope["aggregate_type"],
                "aggregate_version": version,
                "last_event_type": envelope["event_type"],
                "last_event_id": event_id,
                "data_class": envelope["data_class"],
                "updated_at": utc_now(),
            },
        )
        return ConsumeResult(ConsumeOutcome.APPLIED, applied_version=version)

    def backfill_queue(self) -> list[dict[str, Any]]:
        latest = journal.read_latest(
            self.backfill_name,
            lambda item: (item["tenant_id"], item["aggregate_id"], item["aggregate_version"]),
        )
        return list(latest.values())

    def drain_backfill(self, envelope: dict[str, Any]) -> ConsumeResult:
        """补拉到位后重投：走同一套去重与版本规则，不绕过。"""
        result = self.consume(envelope)
        if result.outcome is ConsumeOutcome.APPLIED:
            journal.append(
                self.backfill_name,
                {
                    "event_id": envelope["event_id"],
                    "tenant_id": envelope["tenant_id"],
                    "aggregate_id": envelope["aggregate_id"],
                    "aggregate_version": envelope["aggregate_version"],
                    "expected_version": None,
                    "queued_at": utc_now(),
                    "status": "drained",
                },
            )
        return result


# ==========================================================================
# DLQ：死信与重放
# ==========================================================================

def _placeholder(name: str, value: Any, pattern_ok: bool = True) -> str:
    """占位引用：能保留原值时保留（便于排查），否则给一个**明确标注**的占位。

    绝不为了"看起来合法"而编造业务内容（例如不能把缺失的 tenant 填成 tenant-demo）。
    """
    if isinstance(value, str) and value and pattern_ok:
        return value
    return f"{name}-unavailable"


def sanitize_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """把可能非法的原始事件整理成契约可接受的 Envelope（仅供 DLQ 展示与人工处置）。

    只做"缺失字段占位 + 类型纠正"，不做任何业务推断。
    """
    identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

    def ident(name: str, value: Any) -> str:
        return _placeholder(name, value, isinstance(value, str) and bool(identifier.match(value)))

    event_type = raw.get("event_type")
    if event_type not in EVENT_TYPES:
        event_type = "task.accepted"  # 占位类型：原值保留在 raw_event 里
    aggregate_version = raw.get("aggregate_version")
    if not isinstance(aggregate_version, int) or isinstance(aggregate_version, bool) or aggregate_version < 0:
        aggregate_version = 0
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not payload.get("payload_ref") or not payload.get("payload_hash"):
        payload = {"payload_ref": "payload-unavailable", "payload_hash": "hash-unavailable"}

    return {
        "event_id": ident("event", raw.get("event_id")),
        "event_type": event_type,
        "schema_version": ident("schema", raw.get("schema_version")) if raw.get("schema_version") else "1.0.0",
        "tenant_id": ident("tenant", raw.get("tenant_id")),
        "factory_id": raw.get("factory_id") if isinstance(raw.get("factory_id"), str) and raw.get("factory_id") else None,
        "source_system": ident("source", raw.get("source_system")),
        "aggregate_type": ident("aggregate-type", raw.get("aggregate_type")),
        "aggregate_id": ident("aggregate", raw.get("aggregate_id")),
        "aggregate_version": aggregate_version,
        "occurred_at": raw.get("occurred_at") if isinstance(raw.get("occurred_at"), str) and raw.get("occurred_at") else utc_now(),
        "received_at": raw.get("received_at") if isinstance(raw.get("received_at"), str) and raw.get("received_at") else utc_now(),
        "trace_id": ident("trace", raw.get("trace_id")),
        "causation_id": ident("cause", raw.get("causation_id")),
        "correlation_id": ident("corr", raw.get("correlation_id")),
        "data_class": raw.get("data_class") if raw.get("data_class") in ("C0", "C1", "C2", "C3", "C4") else "C2",
        "payload": payload,
    }


class DeadLetterQueue:
    """死信队列。保留错误原因与次数；重放**仍走原去重规则**。"""

    def __init__(self, *, name: str = "dlq") -> None:
        self.name = f"dlq/{name}"

    def record(self, envelope: dict[str, Any], *, reason: str, attempts: int = 1,
               dlq_id: str | None = None, origin: str = "INBOX") -> dict[str, Any]:
        """登记一条死信。

        `event` 只保存**事件信封**（它只含受控引用与哈希，不含正文）。
        死信里的事件本身可能就是**结构非法**的（这正是它进 DLQ 的原因），
        但契约 `p15-dlq.json` 要求 `event` 是一个合法 `EventEnvelope`。
        因此这里做一次"可读化"：能从原事件取到的字段原样保留，
        缺失/非法的字段替换为占位引用，**绝不编造业务内容**；
        原始事件另存 `raw_event` 供人工排查。
        """
        raw = envelope if isinstance(envelope, dict) else {"_non_object": str(envelope)[:200]}
        entry = {
            "dlq_id": dlq_id or f"dlq-{uuid.uuid4().hex[:16]}",
            "event_id": raw.get("event_id"),
            "tenant_id": raw.get("tenant_id"),
            "aggregate_id": raw.get("aggregate_id"),
            "aggregate_version": raw.get("aggregate_version") if isinstance(raw.get("aggregate_version"), int) else 0,
            "event_type": raw.get("event_type"),
            "origin": origin,
            "event": sanitize_envelope(raw),
            "raw_event": raw,
            "reason": reason,
            "attempts": attempts,
            "replay_count": 0,
            "status": "MANUAL_REVIEW",  # 与 p15-dlq.json 的 handling_status 枚举一致
            "handling_status": "MANUAL_REVIEW",
            "recorded_at": utc_now(),
            "updated_at": utc_now(),
            "record_version": 0,
        }
        journal.append(self.name, entry)
        return entry

    def entries(self) -> list[dict[str, Any]]:
        return journal.read_all(self.name)

    def find(self, dlq_id: str) -> dict[str, Any] | None:
        latest = journal.read_latest(self.name, lambda item: item["dlq_id"])
        return latest.get(dlq_id)

    def resolve(self, dlq_id: str, *, status: str = "RESOLVED", operator_ref: str | None = None,
                reason_code: str | None = None) -> bool:
        record = self.find(dlq_id)
        if record is None:
            return False
        journal.append(
            self.name,
            {
                **record,
                "status": status,
                "operator_ref": operator_ref,
                "handling_reason_code": reason_code,
                "record_version": int(record.get("record_version", 0)) + 1,
                "updated_at": utc_now(),
            },
        )
        return True

    def bump_replay(self, dlq_id: str, *, status: str) -> dict[str, Any] | None:
        record = self.find(dlq_id)
        if record is None:
            return None
        updated = {
            **record,
            "replay_count": int(record.get("replay_count", 0)) + 1,
            "status": status,
            "record_version": int(record.get("record_version", 0)) + 1,
            "updated_at": utc_now(),
        }
        journal.append(self.name, updated)
        return updated

    def replay(self, envelope: dict[str, Any], inbox: Inbox) -> ConsumeResult:
        """重放：**复用同一个 Inbox**，因此已消费过的事件仍会被去重规则挡住。"""
        return inbox.consume(envelope)


# ==========================================================================
# 事件发布：命令/任务状态变化的统一出口
# ==========================================================================

def build_envelope(
    *,
    event_type: str,
    tenant_id: str,
    factory_id: str | None,
    source_system: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    trace_id: str,
    data_class: str,
    payload_ref: str,
    payload_hash: str,
    causation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """构造合规 Envelope。payload 只接受受控引用，**正文不得进入事件**。"""
    envelope = {
        "event_id": f"evt-{uuid.uuid4().hex[:20]}",
        "event_type": event_type,
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "factory_id": factory_id,
        "source_system": source_system,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "occurred_at": utc_now(),
        "received_at": utc_now(),
        "trace_id": trace_id,
        "causation_id": causation_id or f"cause-{uuid.uuid4().hex[:12]}",
        "correlation_id": correlation_id or f"corr-{uuid.uuid4().hex[:12]}",
        "data_class": data_class,
        "payload": {"payload_ref": payload_ref, "payload_hash": payload_hash},
    }
    validate_envelope(envelope)
    return envelope


def _state_root() -> pathlib.Path:
    configured = os.environ.get("V3_STATE_DIR")
    base = pathlib.Path(configured) if configured else REPO_ROOT / "services" / ".evidence" / "state"
    return base if base.is_absolute() else REPO_ROOT / base


def _commit_dir() -> pathlib.Path:
    return _state_root() / "delivery"
