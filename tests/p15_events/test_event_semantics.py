"""事件一致性验收（P1.5）：outbox 原子性 / inbox 去重与版本收敛 / DLQ 重放。

与 `tests/p1_runtime` 的分工：
- `tests/p1_runtime`：跨**真实进程**的 HTTP 验收（谁能读到什么、拒绝语义）。
- 本文件：事件**语义**验收。事件层的接口契约由 P1.5 契约声明，
  但 outbox/inbox/DLQ 的一致性规则与接口形态无关，因此这里直接对内核做断言
  （每个用例用独立的临时 `V3_STATE_DIR`，互不串场）。

上游 §11 逐条对应见 `services/_kernel/events.py` 的模块 docstring。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from services._kernel import events

AGG = "command-demo"


def _envelope(*, version: int, event_id: str, aggregate: str = AGG, event_type: str = "command.accepted", tenant: str = "tenant-demo"):
    envelope = events.build_envelope(
        event_type=event_type,
        tenant_id=tenant,
        factory_id="factory-demo",
        source_system="transaction-service",
        aggregate_type="command",
        aggregate_id=aggregate,
        aggregate_version=version,
        trace_id="trace-demo",
        data_class="C2",
        payload_ref=f"payload-{aggregate}-{version}",
        payload_hash="sha256:demo",
    )
    envelope["event_id"] = event_id
    return envelope


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """每个用例独立的投递/投影状态目录。"""
    monkeypatch.setenv("V3_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("V3_OUTBOX_FAIL_BEFORE_COMMIT", raising=False)
    return tmp_path


# ==========================================================================
# Outbox：状态与事件同事务（原子提交）
# ==========================================================================

def test_state_and_event_commit_together(isolated_state):  # A07
    outbox = events.Outbox(state_name="atomic")
    outbox.stage_state({"command_id": AGG, "status": "ACCEPTED"})
    outbox.stage_event(_envelope(version=0, event_id="evt-0"))

    result = outbox.commit()
    assert result["committed"] is True
    record = outbox.committed()
    assert record is not None
    assert record["state"]["command_id"] == AGG
    assert [item["event_id"] for item in record["events"]] == ["evt-0"]


def test_state_and_event_commit_together(isolated_state):  # A07
    """outbox 原子性：一次提交要么都生效，要么都没有副作用。

    这条必须**直接观察落盘结果**：只断言"未提交时没有事件"是不够的——
    "先落状态再落事件"的实现也能通过那种断言（变异测试实测的盲区）。
    """
    outbox = events.Outbox(state_name="atomic-probe")
    outbox.stage_state({"command_id": AGG, "status": "ACCEPTED"})
    outbox.stage_event(_envelope(version=0, event_id="evt-atomic"))

    result = outbox.commit()
    assert result["committed"] is True
    record = outbox.committed()
    assert record is not None
    assert record["state"]["command_id"] == AGG
    assert [item["event_id"] for item in record["events"]] == ["evt-atomic"]

    # 提交文件是**唯一**落盘点：
    #  - 目录里不得出现任何"先落状态再落事件"式的中间落盘（*.jsonl journal / *.tmp-*）；
    #  - 只允许那一个原子提交文件。
    # 早期只断言 `committed()` 与 `*.tmp-*`，结果"先 append 状态再 replace"的实现
    # 也能通过（变异测试实测的盲区），故这里直接把落盘形态钉死。
    delivery_dir = isolated_state / "delivery"
    json_files = [path.name for path in delivery_dir.glob("*.json")]
    assert json_files == ["delivery_atomic-probe.json"], json_files
    assert not list(delivery_dir.glob("*.jsonl")), "不得存在非原子的中间落盘"
    assert not list(delivery_dir.glob("*.tmp-*"))


def test_crash_before_commit_leaves_no_partial_fact(isolated_state, monkeypatch):  # A07
    """模拟"提交前崩溃"：状态与事件都不能出现，且不得只出现其中一个。"""
    outbox = events.Outbox(state_name="crash")
    outbox.stage_state({"command_id": AGG, "status": "SUCCEEDED"})
    outbox.stage_event(_envelope(version=0, event_id="evt-crash"))

    monkeypatch.setenv("V3_OUTBOX_FAIL_BEFORE_COMMIT", "1")
    with pytest.raises(RuntimeError):
        outbox.commit()

    assert outbox.committed() is None, "崩溃后不得留下半提交事实"
    assert outbox.committed_events() == []
    # 临时文件可以残留，但绝不能被当作已提交读取
    leftover = list((isolated_state / "delivery").glob("*.tmp-*"))
    assert all(path.name for path in leftover)  # 存在与否都行，关键是没被读成事实


def test_nothing_staged_produces_no_side_effect(isolated_state):  # A07
    outbox = events.Outbox(state_name="empty")
    result = outbox.commit()
    assert result["committed"] is False
    assert outbox.committed() is None


def test_delivery_is_at_least_once_and_dedup_is_consumer_duty(isolated_state):  # A07
    """至少一次投递：同一事件可以被投递多次，去重必须由消费侧完成。"""
    outbox = events.Outbox(state_name="deliver")
    outbox.stage_state({"command_id": AGG})
    outbox.stage_event(_envelope(version=0, event_id="evt-d"))
    outbox.commit()

    first = outbox.deliver(event_id="evt-d", delivery_ref="ref-1")
    second = outbox.deliver(event_id="evt-d", delivery_ref="ref-2")
    assert first["delivered"] is True and second["delivered"] is True
    assert first["delivery_ref"] != second["delivery_ref"]

    # 未提交的事件不得被投递
    assert outbox.deliver(event_id="evt-not-committed", delivery_ref="ref-x")["delivered"] is False


# ==========================================================================
# Inbox：去重、版本收敛、缺版本、迟到
# ==========================================================================

def test_duplicate_event_is_applied_once(isolated_state):  # A07
    inbox = events.Inbox(projection_name="dup")
    envelope = _envelope(version=0, event_id="evt-dup")

    assert inbox.consume(envelope).outcome is events.ConsumeOutcome.APPLIED
    assert inbox.consume(envelope).outcome is events.ConsumeOutcome.DUPLICATE
    # 重复不得把投影推进两次（版本仍是最初应用的那个）
    assert inbox.applied_versions(("tenant-demo", AGG))["aggregate_version"] == 0


def test_version_gap_queues_for_backfill_and_fills_in_order(isolated_state):  # A07
    """缺版本先进补拉队列；补齐后按序应用；乱序到达不改变最终版本。"""
    inbox = events.Inbox(projection_name="gap")

    assert inbox.consume(_envelope(version=0, event_id="evt-0")).outcome is events.ConsumeOutcome.APPLIED
    # 跳过 v1 直接来 v2
    gap = inbox.consume(_envelope(version=2, event_id="evt-2"))
    assert gap.outcome is events.ConsumeOutcome.GAP
    assert gap.queued_for_backfill is True
    assert gap.applied_version == 0
    assert len(inbox.backfill_queue()) == 1

    # 补上 v1
    assert inbox.consume(_envelope(version=1, event_id="evt-1")).outcome is events.ConsumeOutcome.APPLIED
    # 重投 v2：必须成功应用，**不能**被误判为"已见过"
    assert inbox.drain_backfill(_envelope(version=2, event_id="evt-2")).outcome is events.ConsumeOutcome.APPLIED
    assert inbox.applied_versions(("tenant-demo", AGG))["aggregate_version"] == 2


def test_late_event_never_overwrites_newer_fact(isolated_state):  # A07
    inbox = events.Inbox(projection_name="late")
    inbox.consume(_envelope(version=0, event_id="evt-0"))
    inbox.consume(_envelope(version=1, event_id="evt-1", event_type="command.succeeded"))

    late = inbox.consume(_envelope(version=0, event_id="evt-late"))
    assert late.outcome is events.ConsumeOutcome.LATE
    projection = inbox.applied_versions(("tenant-demo", AGG))
    assert projection["aggregate_version"] == 1, "迟到事件不得把投影拉回旧版本"
    assert projection["last_event_type"] == "command.succeeded", "迟到事件不得改写最后事实"


def test_aggregate_key_includes_tenant(isolated_state):  # A07
    """不同租户的同名聚合互不影响（P0 x-delivery.aggregate_key 含 tenant）。"""
    inbox = events.Inbox(projection_name="tenant")
    inbox.consume(_envelope(version=0, event_id="evt-a", tenant="tenant-demo"))
    # 另一个租户从 version 0 开始也必须是 APPLIED，而不是"重复/迟到"
    result = inbox.consume(_envelope(version=0, event_id="evt-b", tenant="tenant-c3"))
    assert result.outcome is events.ConsumeOutcome.APPLIED


# ==========================================================================
# DLQ
# ==========================================================================

def test_dlq_keeps_reason_attempts_and_human_status(isolated_state):  # A07
    dlq = events.DeadLetterQueue()
    entry = dlq.record(_envelope(version=5, event_id="evt-bad"), reason="handler_failed", attempts=3)

    entries = dlq.entries()
    assert len(entries) == 1
    stored = entries[0]
    assert stored["reason"] == "handler_failed"
    assert stored["attempts"] == 3
    # 与 p15-dlq.json 的 handling_status 枚举一致
    assert stored["handling_status"] == "MANUAL_REVIEW"
    assert stored["event_id"] == "evt-bad"
    assert stored["replay_count"] == 0

    assert dlq.resolve(entry["dlq_id"], status="RESOLVED") is True
    assert dlq.find(entry["dlq_id"])["status"] == "RESOLVED"


def test_dlq_sanitizes_malformed_event_without_inventing_business_data(isolated_state):  # A07
    """死信条目里的事件必须是合法 Envelope，但**不得编造业务内容**。"""
    broken = {"event_id": "evt-broken"}  # 缺绝大多数必填字段
    dlq = events.DeadLetterQueue()
    entry = dlq.record(broken, reason="missing_fields")

    events.validate_envelope(entry["event"])  # 契约 p15-dlq.json 要求 event 合法
    assert entry["event"]["event_id"] == "evt-broken", "能取到的原值必须保留"
    # 取不到的字段用明确的占位标识，而不是填成 tenant-demo 这类看似真实的业务值
    assert entry["event"]["tenant_id"] == "tenant-unavailable"
    assert entry["event"]["aggregate_id"] == "aggregate-unavailable"
    assert entry["raw_event"] == broken, "原始事件必须保留供人工排查"


def test_dlq_replay_still_obeys_dedup_rules(isolated_state):  # A07
    """重放**仍走原去重规则**：已应用过的事件重放不得再产生副作用。"""
    inbox = events.Inbox(projection_name="replay")
    envelope = _envelope(version=0, event_id="evt-r")
    inbox.consume(envelope)

    dlq = events.DeadLetterQueue()
    assert dlq.replay(envelope, inbox).outcome is events.ConsumeOutcome.DUPLICATE


# ==========================================================================
# Envelope 结构与 fail-closed
# ==========================================================================

def test_envelope_rejects_missing_fields_and_unknown_type(isolated_state):  # A07
    good = _envelope(version=0, event_id="evt-ok")
    events.validate_envelope(good)  # 不抛异常

    for field_name in events.ENVELOPE_REQUIRED:
        broken = dict(good)
        broken.pop(field_name)
        with pytest.raises(events.EventError):
            events.validate_envelope(broken)

    unknown = dict(good)
    unknown["event_type"] = "not.a.registered.event"
    with pytest.raises(events.EventError):
        events.validate_envelope(unknown)


def test_payload_must_be_controlled_reference_not_body(isolated_state):  # A07
    """正文不得进入事件：payload 只接受受控引用与哈希。"""
    good = _envelope(version=0, event_id="evt-p")
    events.validate_envelope(good)

    for broken_payload in ({"payload_ref": "p"}, {"payload_hash": "h"}, {"payload_ref": "p", "payload_hash": ""}, "raw text"):
        broken = dict(good)
        broken["payload"] = broken_payload
        with pytest.raises(events.EventError):
            events.validate_envelope(broken)


def test_consuming_invalid_envelope_is_rejected_not_silently_applied(isolated_state):  # A07
    inbox = events.Inbox(projection_name="invalid")
    broken = _envelope(version=0, event_id="evt-x")
    broken.pop("aggregate_version")
    with pytest.raises(events.EventError):
        inbox.consume(broken)
    assert inbox.applied_versions(("tenant-demo", AGG)) is None


def test_delivery_constants_match_p0_contract(isolated_state):  # A07
    """语义常量必须与 P0 `event-envelope.json` 的 x-delivery 一致。"""
    assert events.DELIVERY_SEMANTICS == "at_least_once"
    assert events.LATE_EVENT_OVERWRITE is False
    assert events.EXECUTED_EDGE_ACTION_REPLAY is False
    assert events.aggregate_key({"tenant_id": "t", "aggregate_id": "a"}) == ("t", "a")
