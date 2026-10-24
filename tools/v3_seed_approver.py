"""用 V2.1 的**正规邀请流程**给 company 1 加一个批准人。

## 为什么不用直接插库

V2.1 有正式的成员管理（`POST /members/invite` → `POST /members/invite/accept`）。
绕过它去写 `users` 表会有两个后果：
① 邀请台账（`member_invites`）与实际成员不一致；
② 下次谁再走正规流程时会撞上"用户已存在"这类本可避免的冲突。

## 为什么需要这个人

B4（用户侧写操作）的核心是**批准**。而批准受一条硬约束：

    `revalidate_before_execution` → `DUTIES_NOT_SEPARATED`
    （上游 §7「明确岗位批准、职责分离」）

即**审批人不得是发起人**。此前公司里只有一个用户，于是"自己批自己"
必然被拒——那不是 bug，是系统在正确地拦。

所以要让批准可用，必须**同一租户里有两个不同的人**。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ADMIN = ("admin@demo.com", "123456")
APPROVER_EMAIL = "approver@demo.com"
APPROVER_PASSWORD = "Approver@123"


def call(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:300]}


def login(email: str, password: str):
    return call("POST", "/auth/login", {"email": email, "password": password})


def main() -> int:
    status, admin = login(*ADMIN)
    if status != 200:
        print("管理员登录失败:", status, admin)
        return 1
    token = admin["access_token"]
    print(f"管理员登录成功 company_id={admin.get('company_id')}")

    # 已经存在就不用再邀请
    status, existing = login(APPROVER_EMAIL, APPROVER_PASSWORD)
    if status == 200:
        print(f"批准人已存在 company_id={existing.get('company_id')}")
        return 0

    status, invited = call(
        "POST", "/members/invite",
        {"company_id": admin["company_id"], "email": APPROVER_EMAIL, "role": "admin"},
        token=token,
    )
    print("邀请:", status, json.dumps(invited, ensure_ascii=False)[:200])
    if status != 200:
        return 1

    code = invited.get("code") or invited.get("invite_code")
    if not code:
        print("邀请响应里没有邀请码，字段为:", list(invited))
        return 1

    status, accepted = call(
        "POST", "/members/invite/accept",
        {"code": code, "email": APPROVER_EMAIL, "password": APPROVER_PASSWORD, "name": "演示审批人"},
    )
    print("接受邀请:", status, json.dumps(accepted, ensure_ascii=False)[:200])
    if status != 200:
        return 1

    status, who = login(APPROVER_EMAIL, APPROVER_PASSWORD)
    print(f"批准人登录验证: {status} company_id={who.get('company_id')} role={who.get('role')}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
