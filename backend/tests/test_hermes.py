"""
tests/test_hermes.py —— Hermes Agent Runtime 全链路测试（落地手册第二篇）

覆盖：Runtime 装配 → Agent 注册/发现 → Tool 权限校验 → Agent 执行
     → 记忆读写 → 任务队列 → 多租户隔离
运行：python tests/test_hermes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["USE_SQLITE"] = "1"
os.environ["SQLITE_PATH"] = f"./test_hermes_{os.getpid()}.db"
_TEST_DB = os.environ["SQLITE_PATH"]
import base64

from fastapi.testclient import TestClient
from main import app

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def main():
    with TestClient(app) as client:
        _run(client)


def _run(client):
    global PASS, FAIL
    print("=" * 60)
    print("Hermes Agent Runtime - 全链路测试")
    print("=" * 60)

    # 登录拿 token
    r = client.post("/auth/login", json={"email": "admin@demo.com", "password": "123456"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    company_id = r.json()["company_id"]

    # 让演示企业通过认证（满足新版 require_verified 校验）
    import io, time
    png = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
    client.post("/company/license/upload",
                data={"company_id": str(company_id)},
                files={"file": ("lic.png", io.BytesIO(png), "image/png")},
                headers=headers)
    time.sleep(1.5)   # 等待 AI 自动审核

    print("\n[1] Hermes Runtime 装配")
    r = client.get("/hermes/agent/list", headers=headers)
    check("Agent 注册中心返回内置 Agent", r.status_code == 200)
    agents = r.json()["agents"]
    names = [a["name"] for a in agents]
    check("内置 6 个 Agent 已注册", len(agents) >= 6, str(names))
    check("sales_agent 已注册", "sales_agent" in names)
    check("customer_agent 已注册", "customer_agent" in names)

    print("\n[2] Tool 系统")
    r = client.get("/hermes/tool/list", headers=headers)
    check("Tool 清单", r.status_code == 200)
    tool_names = [t["name"] for t in r.json()["tools"]]
    check("8 个内置 Tool", len(tool_names) >= 8, str(tool_names))
    check("crm.get_customer 存在", "crm.get_customer" in tool_names)

    print("\n[3] Agent 执行（Hermes Runtime 编排）")
    r = client.post("/hermes/execute", json={
        "agent_name": "sales_agent",
        "message": "我们需要采购1000个500ml玻璃烧杯",
        "company_id": company_id}, headers=headers)
    check("执行 sales_agent", r.status_code == 200, str(r.text))
    check("返回结果", len(r.json().get("result", "")) > 0)
    session_id = r.json().get("session_id")
    check("生成 session", bool(session_id))

    print("\n[4] Tool 调用（Tool Router + Permission Check）")
    r = client.post("/hermes/tool/call", json={
        "session_id": session_id,
        "tool_name": "crm.get_customer",
        "params": {}}, headers=headers)
    check("调用 crm.get_customer", r.status_code == 200, str(r.text))
    check("返回 ok", r.json().get("ok") is True)

    r = client.post("/hermes/tool/call", json={
        "session_id": session_id,
        "tool_name": "dashboard.snapshot",
        "params": {}}, headers=headers)
    check("调用 dashboard.snapshot", r.json().get("ok") is True)

    # 未注册 Tool 拒绝（权限系统拦截：Agent 未声明该 Tool → 403）
    r = client.post("/hermes/tool/call", json={
        "session_id": session_id,
        "tool_name": "hack.delete_all",
        "params": {}}, headers=headers)
    check("未知 Tool 被权限系统拒绝", r.status_code in (403, 404) or
          r.json().get("ok") is False, f"status={r.status_code}")

    print("\n[5] 三层记忆")
    r = client.get("/hermes/memory/short", params={"session_id": session_id}, headers=headers)
    check("短期记忆可读取", r.status_code == 200)
    check("短期记忆含对话", r.json().get("count", 0) >= 1, str(r.text))

    print("\n[6] 消息队列")
    r = client.post("/hermes/task/enqueue", json={
        "task_name": "customer_analysis",
        "payload": {"company_id": company_id}}, headers=headers)
    check("任务入队", r.status_code == 200, str(r.text))
    check("返回 task_id", bool(r.json().get("task_id")))

    r = client.post("/hermes/task/process", headers=headers)
    check("同步处理任务", r.status_code == 200)

    print("\n[7] 权限管理（RBAC）")
    r = client.post("/hermes/permissions", json={
        "company_id": company_id,
        "action": "revoke",
        "permission": "crm:write"}, headers=headers)
    check("撤销 crm:write 权限", r.status_code == 200)

    print("\n[8] Agent 注册（动态加载）")
    r = client.post("/hermes/agent/register", json={
        "name": "procurement_agent",
        "version": "1.0",
        "industry": "glassware",
        "label": "AI采购",
        "description": "供应链采购分析",
        "tools": ["erp.get_stock", "dashboard.snapshot"]}, headers=headers)
    check("注册新 Agent", r.status_code == 200, str(r.text))
    r = client.get("/hermes/agent/list", params={"industry": "glassware"}, headers=headers)
    check("按行业发现 Agent", any(a["name"] == "procurement_agent" for a in r.json()["agents"]))

    print("\n" + "=" * 60)
    print(f"Hermes 测试结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)

    for f in [_TEST_DB]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
