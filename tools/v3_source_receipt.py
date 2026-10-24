"""端到端验证：**批准 → 执行链**（走 BFF 真实接口，不绕内部端点）。

## 覆盖什么

① 发起 → ② 另一人批准 → ③ 推进执行（BFF 编排：确保命令 → 租约 → 复核 → 执行 → 派发）
→ ④ 源系统回执 → ⑤ 命令终态

## 为什么回执由本脚本发

权威回执是**源系统**给的事实（ERP 出了什么事由 ERP 说）。
BFF **刻意不替源系统造回执**——那正是本项目第一条显示契约要防的事。
演示环境里没有真 ERP，所以由本源系统替身补一条，模拟外部系统异步回执。

用法：
    python tools/v3_demo_receipt.py --command-id command-xxx
    python tools/v3_demo_receipt.py --command-id command-xxx --outcome FAILED
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import uuid

ROOT = pathlib.Path(r"H:\制造业 AI 员工平台全量开发")
sys.path.insert(0, str(ROOT))

from services._kernel import transactions  # noqa: E402

# 与内核 `CONTROLLED_EXECUTION_CALLER` 同源：只有事务身份能派发
TENANT_ID = "tenant-demo"


def post(service: str, pathname: str, body: dict, identity: str):
    """直接打内部端点（本工具扮演**源系统 / 事务侧**，不是浏览器）。"""
    import urllib.error
    import urllib.request

    ports = json.loads((ROOT / ".v3-dev" / "ports.json").read_text(encoding="utf-8"))
    port = ports[service]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{pathname}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer p0-test")
    request.add_header("X-V3-Identity", identity)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:300]}


def get(service: str, pathname: str, context: dict):
    import urllib.error
    import urllib.request

    ports = json.loads((ROOT / ".v3-dev" / "ports.json").read_text(encoding="utf-8"))
    port = ports[service]
    request = urllib.request.Request(f"http://127.0.0.1:{port}{pathname}", method="GET")
    request.add_header("Authorization", "Bearer p0-test")
    request.add_header("X-V3-Identity", "svc-transaction-service")
    request.add_header("X-Context", json.dumps(context, ensure_ascii=False))
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:300]}


def find_task_id(command_id: str) -> str | None:
    """在命令账本里找这条命令的 task_id（读真相源，不另记索引）。"""
    journal = ROOT / ".v3-dev" / "state" / "commands.jsonl"
    try:
        raw = journal.read_text(encoding="utf-8")
    except OSError:
        return None
    found = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("command_id") != command_id:
            continue
        found = record  # 追加式账本：后写覆盖先写
    if not found:
        return None
    return found.get("task_id") or (found.get("binding") or {}).get("context", {}).get("task_id")


def main() -> int:
    parser = argparse.ArgumentParser(description="演示用源系统回执（补一条权威回执）")
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--outcome", default="SUCCEEDED",
                        choices=["SUCCEEDED", "FAILED", "accepted", "NOT_EXECUTED"])
    parser.add_argument("--posting-status", default="POSTED",
                        choices=["POSTED", "NOT_CONFIRMED"])
    args = parser.parse_args()

    context = {
        "tenant_id": TENANT_ID, "factory_id": "factory-demo", "actor_id": "user-demo",
        "employee_id": "employee-planning", "task_id": None, "trace_id": f"trace-src-{uuid.uuid4().hex[:8]}",
        "data_class": "C2", "schema_version": "1.0.0", "deadline_at": "2026-12-31T00:00:00Z",
    }

    # ⚠️ 工厂只读链路要求 `task_id` **非空**（实测 400 `REQUEST_INVALID`）。
    # 命令自己的 task_id 只有读出来才知道 —— 鸡生蛋。所以先在账本里定位它，
    # 再带着 task_id 去读；否则第一次读就必然失败（实测踩过）。
    task_id = find_task_id(args.command_id)
    if task_id:
        context["task_id"] = task_id
        print(f"命令所属任务：{task_id}")

    status, record = get("transaction-service", f"/internal/v1/commands/{args.command_id}/execution-record", context)
    if status != 200:
        print(f"读不到命令 [{status}]：{record}")
        return 1
    print(f"命令当前状态：{record.get('status')}")

    binding = record.get("binding") or {}
    payload_hash = transactions.canonical_payload_hash(
        tenant_id=TENANT_ID,
        object_id=binding["object_id"],
        expected_version=binding["expected_version"],
        tool_id=binding["tool_id"],
        tool_version=binding["tool_version"],
        command_type=binding["command_type"],
        action_scope=binding["action_scope"],
        payload_ref=binding["payload_ref"],
    )
    print(f"复算 payload_hash：{payload_hash[:16]}…（与内核 `_p3_recompute_hash` 同算法）")

    succeeded = args.outcome == "SUCCEEDED"
    body = {
        "tenant_id": TENANT_ID,
        "context": context,
        "receipt_id": f"receipt-{uuid.uuid4().hex[:16]}",
        "command_id": args.command_id,
        "idempotency_key": f"receipt-key-{uuid.uuid4().hex[:12]}",
        "command_payload_hash": payload_hash,
        "source_system": "erp",
        "adapter_unit": "adapter-erp-inventory",
        "outcome": args.outcome,
        "authority_receipt_ref": f"erp-receipt-{uuid.uuid4().hex[:12]}",
        "authority_receipt_hash": f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
        "authority_verified": True,
        "confirmed_at": "2026-10-06T00:00:01Z",
        "recorded_at": "2026-10-06T00:00:02Z",
        "command_status": "SUCCEEDED" if succeeded else "FAILED",
        "business_success": succeeded,
        "posting_status": args.posting_status,
        "posting_evidence_ref": f"erp-posting-{uuid.uuid4().hex[:12]}",
        "status_source": "erp",
    }
    status, receipt = post(
        "transaction-service", f"/internal/v1/commands/{args.command_id}/receipts", body, "svc-hermes-runtime"
    )
    print(f"回执 [{status}]：outcome={receipt.get('outcome')} business_success={receipt.get('business_success')}")
    if status != 200:
        print(f"  失败原因：{receipt.get('code')} {receipt.get('message')}")
        return 1

    status, final = get("transaction-service", f"/internal/v1/commands/{args.command_id}/execution-record", context)
    print()
    print("终态：")
    print(f"  status            = {final.get('status')}")
    print(f"  business_success  = {final.get('business_success')}")
    print(f"  posting_status    = {final.get('posting_status')}")
    print(f"  authority_receipt = {'有' if final.get('authority_receipt') else '无'}")
    print(f"  posting_evidence  = {final.get('posting_evidence_ref')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
