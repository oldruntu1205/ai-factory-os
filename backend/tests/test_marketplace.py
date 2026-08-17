"""
tests/test_marketplace.py —— Agent 行业商城 + RabbitMQ 队列测试

覆盖：商城浏览/详情/安装到企业（Hermes 注册+AI员工创建）/卸载/已安装
     + 队列（内存模式入队/处理/事件）+ RabbitMQ 适配器优雅降级
运行：python tests/test_marketplace.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["USE_SQLITE"] = "1"
os.environ["QUEUE_MODE"] = "memory"
os.environ["SQLITE_PATH"] = f"./test_marketplace_{os.getpid()}.db"
_TEST_DB = os.environ["SQLITE_PATH"]

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
    print("Agent 行业商城 + RabbitMQ 队列测试")
    print("=" * 60)

    # 注册新企业（避免种子企业已安装冲突）
    import time
    email = f"market_{int(time.time())}@test.com"
    r = client.post("/auth/register", json={
        "company": "商城测试企业", "email": email, "password": "123456"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    company_id = r.json()["company_id"]

    print("\n[1] 商城浏览")
    r = client.get("/marketplace/packages", headers=headers)
    check("浏览商城", r.status_code == 200, str(r.text))
    packages = r.json()["packages"]
    check("有种子包", len(packages) >= 4, f"count={len(packages)}")
    check("返回安装状态", all("installed" in p for p in packages))

    r = client.get("/marketplace/packages", params={"industry": "glassware"}, headers=headers)
    check("按行业过滤", all(p["industry"] == "glassware" for p in r.json()["packages"]))

    # 行业分类：V2.1 调整为 [{code, name}] 结构，只含当前企业行业 + 通用
    r = client.get("/marketplace/industries", headers=headers)
    inds = r.json()["industries"]
    check("行业分类（对象结构）", isinstance(inds, list) and all(
        isinstance(i, dict) and "code" in i and "name" in i for i in inds))
    check("行业分类含通用", any(i.get("code") == "general" for i in inds))

    print("\n[2] 包详情")
    pkg_id = packages[0]["id"]
    r = client.get(f"/marketplace/packages/{pkg_id}", headers=headers)
    check("包详情", r.status_code == 200)
    check("含 Agent 清单", len(r.json().get("agents", [])) > 0)
    check("含 Tool 清单", len(r.json().get("tools", [])) > 0)

    print("\n[3] 安装到企业（Hermes 注册 + AI 员工创建）")
    r = client.post("/marketplace/install", json={
        "company_id": company_id, "package_id": pkg_id}, headers=headers)
    check("安装成功", r.status_code == 200, str(r.text))
    check("创建了 AI 员工", len(r.json().get("agents", [])) > 0)

    # 已安装列表
    r = client.get("/marketplace/installed", params={"company_id": company_id}, headers=headers)
    check("已安装列表", len(r.json()["installed"]) == 1)

    # 重复安装拦截
    r = client.post("/marketplace/install", json={
        "company_id": company_id, "package_id": pkg_id}, headers=headers)
    check("重复安装被拦截", r.status_code == 400)

    # AI 员工已创建
    r = client.get("/agent/list", params={"company_id": company_id}, headers=headers)
    agent_roles = [a["role"] for a in r.json()["agents"]]
    check("企业出现新 AI 员工", len(r.json()["agents"]) > 0, str(agent_roles))

    print("\n[4] Hermes 注册联动")
    r = client.get("/hermes/agent/list", headers=headers)
    check("Hermes Agent 注册中心", r.status_code == 200)

    print("\n[5] 卸载")
    r = client.post("/marketplace/uninstall", json={
        "company_id": company_id, "package_id": pkg_id}, headers=headers)
    check("卸载成功", r.status_code == 200, str(r.text))
    r = client.get("/marketplace/installed", params={"company_id": company_id}, headers=headers)
    check("卸载后列表为空", len(r.json()["installed"]) == 0)

    print("\n[6] 消息队列（内存模式）")
    r = client.post("/hermes/task/enqueue", json={
        "task_name": "customer_analysis", "payload": {"company_id": company_id}}, headers=headers)
    check("任务入队", r.status_code == 200)
    check("标记 transport", r.json().get("transport") in ("memory", "redis", "rabbitmq"))

    print("\n[7] RabbitMQ 适配器（无服务优雅降级）")
    from hermes.queue.rabbitmq import RabbitMQAdapter
    from config import RABBITMQ_URL
    mq = RabbitMQAdapter(url=RABBITMQ_URL)
    hc = mq.health_check()
    check("health_check 不崩溃", hc["status"] in ("ok", "error"))
    if hc["status"] == "error":
        check("无服务时返回错误信息", "error" in hc)
        print("      （本机无 RabbitMQ，生产环境由 docker-compose 提供）")
    else:
        # 真实连接：测试发布/消费
        import uuid
        test_task = f"unit_test_{uuid.uuid4().hex[:6]}"
        r = mq.publish_task(test_task, {"probe": 1})
        check("真实发布任务", r["status"] == "queued")

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} 通过, {FAIL} 失败")
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
