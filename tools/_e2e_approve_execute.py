"""端到端：**批准 → 执行 → 权威回执**（全走 BFF 的真实用户接口）。

判据不是"接口返回 200"，而是走完之后：
**任务结论页真的显示「已过账」，而这是权威回执带来的。**

覆盖：
① 发起（带准入结论）→ ② 另一人批准 → ③ 推进执行（BFF 编排 5 步）
→ ④ 源系统回执 → ⑤ 任务结论显示已过账
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(r"H:\制造业 AI 员工平台全量开发")
BFF = "http://127.0.0.1:8300"
V21 = "http://127.0.0.1:8000"
PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"

# 唯一"三项能力位齐备"的 R2 工具 ⇒ 唯一能真正走完批准→执行的工具
TOOL = "erp.inventory.reserve"
# 业务对象编号用**客户会填的那种**（如"3 号仓原料"），
# 不用随机 hex——否则界面上显示的是测试夹具，看不出产品真实样子。
# 这个编号是**调用方自己填的**（内核原样转抄 `body["object_id"]`），
# 所以它就是"客户的业务对象编号"，不是系统生成的 ID。
OBJECT_ID = "仓库3-原料A-2026W41"


def call(url: str, method: str, body=None, token=None):
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:300]}


def login(email: str, password: str):
    status, body = call(f"{V21}/auth/login", "POST", {"email": email, "password": password})
    return body.get("access_token") if status == 200 else None


def main() -> int:
    requester = login("admin@demo.com", "123456")
    approver = login("approver@demo.com", "Approver@123")
    if not requester or not approver:
        print("登录失败")
        return 1

    print(f"工具：{TOOL}（三项能力位齐备的 R2 写）")
    print()
    print("① 发起写入")
    status, created = call(
        f"{BFF}/v3-user/write-request", "POST",
        {"tool_id": TOOL, "object_id": OBJECT_ID, "expected_version": 1},
        token=requester,
    )
    if not created.get("ok"):
        print(f"  失败 [{status}]：{created.get('code')} {created.get('message')}")
        return 1
    admission = created.get("admission") or {}
    print(f"  批准单 {created['approval_id']}")
    print(f"  任务   {created['task_id']}")
    print(f"  准入：能力位齐备={admission.get('unattended_high_risk_allowed')} "
          f"缺={admission.get('missing_capabilities')}")

    print()
    print("② 由另一个人批准")
    status, listing = call(f"{BFF}/v3-user/approvals", "GET", token=approver)
    mine = next((a for a in listing["approvals"] if a["approval_id"] == created["approval_id"]), None)
    if mine is None:
        print("  待批准清单里找不到刚建立的这条")
        return 1
    decision = {
        "approval_id": mine["approval_id"],
        "approved": True,
        "object_id": mine["object_id"],
        "expected_version": mine["expected_version"],
        "tool_id": mine["tool_id"],
        "tool_version": mine["tool_version"],
        "command_type": mine["command_type"],
        "action_scope": mine["action_scope"],
        "payload_hash": mine["payload_hash"],
        "payload_ref": mine["payload_ref"],
        "normalization_ref": mine["normalization_ref"],
        "policy_version": mine["policy_version"],
        "valid_until": mine["valid_until"],
    }
    status, decided = call(f"{BFF}/v3-user/approval-decision", "POST", decision, token=approver)
    print(f"  [{status}] approved={decided.get('approved')} by {decided.get('decided_by')}")
    if status != 200:
        print(f"  失败：{decided.get('code')} {decided.get('message')}")
        return 1

    print()
    print("③ 推进执行（BFF 编排：确保命令 → 租约 → 复核 → 执行 → 派发）")
    status, executed = call(
        f"{BFF}/v3-user/execute", "POST", {"approval_id": created["approval_id"]}, token=requester
    )
    print(f"  [{status}] ok={executed.get('ok')} dispatched={executed.get('dispatched')} "
          f"status_after={executed.get('status_after')}")
    if not executed.get("ok"):
        print(f"  失败：{executed.get('code')}")
        print(f"        {executed.get('message')}")
        if executed.get("reason_ref"):
            print(f"        reason_ref={executed['reason_ref']}")
        if executed.get("failed_checks"):
            print(f"        未通过项={executed['failed_checks']}")
        return 1
    command_id = executed["command_id"]
    print(f"  执行单 {command_id}")

    print()
    print("④ 源系统给权威回执（不是 BFF 造的）")
    result = subprocess.run(
        [str(PY), str(ROOT / "tools" / "v3_source_receipt.py"), "--command-id", command_id],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for line in (result.stdout or "").strip().splitlines():
        print(f"  {line}")
    if result.returncode != 0:
        print(f"  回执失败 exit={result.returncode}")
        print(result.stderr[-400:] if result.stderr else "")
        return 1

    print()
    print("⑤ 任务结论（用户看到的那一页）")
    status, view = call(f"{BFF}/v3-user/task?task_id={created['task_id']}", "GET", token=requester)
    display = view.get("display") or {}
    print(f"  结论       {display.get('label')}")
    print(f"  有权威支撑 {display.get('authoritative')}")
    print(f"  出处       {display.get('reason')}")
    for source in display.get("sources") or []:
        print(f"    - {source.get('name')}: {source.get('status')} (来自 {source.get('source')})")

    print()
    print("=" * 66)
    ok = (
        executed.get("dispatched") is True
        and display.get("outcome") == "POSTED"
        and display.get("authoritative") is True
    )
    print(f"③ 派发成功        : {'PASS' if executed.get('dispatched') else 'FAIL'}")
    print(f"⑤ 结论=已过账     : {'PASS' if display.get('outcome') == 'POSTED' else 'FAIL'}")
    print(f"⑤ 有权威回执支撑  : {'PASS' if display.get('authoritative') else 'FAIL'}")
    print(f"总体              : {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
