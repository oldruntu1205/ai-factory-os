"""
tests/test_api.py —— 全链路 API 测试（FastAPI TestClient）

覆盖：注册 -> 登录 -> 创建AI员工 -> RAG上传 -> 对话 -> CRM -> 报价 -> 驾驶舱
运行：python tests/test_api.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["USE_SQLITE"] = "1"
os.environ["SQLITE_PATH"] = f"./test_industrial_ai_{os.getpid()}.db"
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
    global client
    with TestClient(app) as client:
        _run_all(client)


def _run_all(client):
    global PASS, FAIL
    print("=" * 60)
    print("工业 AI 员工平台 - 全链路 API 测试")
    print("=" * 60)

    # 0. 初始检查（确保种子数据已建表）
    r = client.get("/health")
    check("健康检查", r.status_code == 200)

    # 2. 注册
    print("\n[2] 企业注册（多租户）")
    r = client.post("/auth/register", json={
        "company": "测试企业", "email": "test@test.com", "password": "123456"})
    check("注册成功", r.status_code == 200, str(r.text))
    check("返回 token", "access_token" in r.json())
    company_id = r.json().get("company_id")

    # 3. 登录
    print("\n[3] 登录 / JWT")
    r = client.post("/auth/login", json={"email": "test@test.com", "password": "123456"})
    check("登录成功", r.status_code == 200, str(r.text))
    token = r.json().get("access_token")
    headers = {"Authorization": f"Bearer {token}"}
    check("返回 company_id", r.json().get("company_id") == company_id)

    # 升级测试企业套餐（Starter 限制 1 个 AI 员工，Professional 允许 5 个）
    from database import SessionLocal
    from models import Company
    _db = SessionLocal()
    _c = _db.query(Company).filter(Company.id == company_id).first()
    _c.plan = "professional"
    _c.license_status = "approved"
    _c.license_verified = True
    _c.max_agents = 5
    _db.commit()
    _db.close()

    # 4. 未授权拦截
    print("\n[4] 权限控制")
    r = client.get("/agent/list", params={"company_id": company_id})
    check("无 token 返回 401", r.status_code == 401)

    # 5. 创建 AI 员工
    print("\n[5] AI 员工管理")
    r = client.post("/agent/create", json={
        "name": "Lisa", "role": "sales", "description": "测试销售",
        "company_id": company_id}, headers=headers)
    check("创建销售 AI", r.status_code == 200, str(r.text))
    r = client.post("/agent/create", json={
        "name": "Amy", "role": "customer", "company_id": company_id}, headers=headers)
    check("创建客服 AI", r.status_code == 200)
    r = client.post("/agent/create", json={
        "name": "Quinn", "role": "quotation", "company_id": company_id}, headers=headers)
    check("创建报价 AI", r.status_code == 200)

    r = client.get("/agent/list", params={"company_id": company_id}, headers=headers)
    check("AI 员工列表", len(r.json()["agents"]) == 3)

    # 6. 多租户隔离
    print("\n[6] 多租户数据隔离")
    r = client.post("/auth/register", json={
        "company": "另一企业", "email": "other@test.com", "password": "123456"})
    other_company_id = r.json()["company_id"]
    r = client.get("/agent/list", params={"company_id": other_company_id}, headers=headers)
    check("企业A访问企业B返回403", r.status_code == 403)
    r = client.get("/agent/list", params={"company_id": other_company_id})
    check("跨企业无权限返回 401", r.status_code == 401)

    # 7. 知识库上传（生成测试文档）
    print("\n[7] RAG 知识库")
    test_doc = "test_doc.md"
    with open(test_doc, "w", encoding="utf-8") as f:
        f.write("# 产品目录\n\n" + "玻璃烧杯 500ml 硼硅酸盐材质，耐高温500度，适用于实验室加热实验。\n" * 20)
    with open(test_doc, "rb") as f:
        r = client.post("/knowledge/upload",
                        data={"company_id": str(company_id)},
                        files={"file": (test_doc, f, "text/markdown")},
                        headers=headers)
    check("文档上传+向量化", r.status_code == 200, str(r.text))
    check("生成知识片段", r.json().get("chunks", 0) > 0)

    r = client.get("/knowledge/list", params={"company_id": company_id}, headers=headers)
    check("文档列表", len(r.json()["documents"]) == 1)

    r = client.post("/knowledge/search", json={
        "question": "玻璃烧杯的材质是什么？", "company_id": company_id}, headers=headers)
    check("RAG 检索", r.status_code == 200)

    # 8. AI 员工对话
    print("\n[8] Multi-Agent 对话")
    r = client.post("/agent/chat", json={
        "message": "我们需要采购1000个500ml玻璃烧杯", "company_id": company_id},
        headers=headers)
    check("智能路由对话", r.status_code == 200, str(r.text))
    check("返回答案", len(r.json()["answer"]) > 0)

    agents = client.get("/agent/list", params={"company_id": company_id},
                        headers=headers).json()["agents"]
    sales_agent = [a for a in agents if a["role"] == "sales"][0]
    r = client.post("/agent/chat", json={
        "message": "德国客户需要2000个硼硅酸盐烧杯", "company_id": company_id,
        "agent_id": sales_agent["id"]}, headers=headers)
    check("指定销售Agent对话", r.status_code == 200)

    # 9. CRM
    print("\n[9] CRM 客户")
    r = client.post("/crm/customer", json={
        "name": "Alpha Chem", "country": "Germany", "email": "a@a.de",
        "requirement": "500ml烧杯1000个", "company_id": company_id}, headers=headers)
    check("创建客户", r.status_code == 200)
    customers = client.get("/crm/customers", params={"company_id": company_id},
                           headers=headers).json()["customers"]
    check("客户列表", len(customers) >= 1)
    cust_id = customers[0]["id"]
    r = client.put(f"/crm/customer/{cust_id}", json={"status": "quoted"},
                   params={"company_id": company_id}, headers=headers)
    check("更新客户状态", r.status_code == 200)

    # 10. 报价
    print("\n[10] AI 报价单")
    r = client.post("/quotation/create", json={
        "requirement": "采购1000个500ml玻璃烧杯，30天内交货",
        "company_id": company_id, "customer_id": cust_id}, headers=headers)
    check("AI 生成报价", r.status_code == 200, str(r.text))
    quotes = client.get("/quotation/list", params={"company_id": company_id},
                        headers=headers).json()["quotations"]
    check("报价单列表", len(quotes) >= 1)

    # 11. 驾驶舱
    print("\n[11] 驾驶舱")
    r = client.get("/dashboard/overview", params={"company_id": company_id}, headers=headers)
    check("经营总览", r.status_code == 200)
    r = client.get("/dashboard/sales", params={"company_id": company_id}, headers=headers)
    check("销售驾驶舱", r.status_code == 200)
    r = client.get("/dashboard/daily-report", params={"company_id": company_id}, headers=headers)
    check("CEO 经营日报", r.status_code == 200)

    # 12. V2.1 AI 自主销售
    print("\n[12] V2.1 AI 自主销售")
    r = client.post("/leads/discover", json={"company_id": company_id}, headers=headers)
    check("AI 客户发现", r.status_code == 200, str(r.text))
    leads = client.get("/leads/list", params={"company_id": company_id},
                       headers=headers).json()["leads"]
    check("线索列表", len(leads) > 0)
    lead_id = leads[0]["id"]
    r = client.post(f"/leads/{lead_id}/follow", json={"company_id": company_id},
                    headers=headers)
    check("线索跟进邮件", r.status_code == 200)

    # 13. 系统
    print("\n[13] 系统接口")
    r = client.get("/system/plans")
    check("SaaS 套餐", r.status_code == 200 and len(r.json()["plans"]) == 3)
    r = client.post("/system/order", json={
        "order_no": "ORD10001", "product": "玻璃烧杯", "quantity": 10000,
        "deadline_days": 30, "progress": 72, "line": "A01", "load_rate": 70,
        "company_id": company_id}, headers=headers)
    check("写入 MES 订单", r.status_code == 200)
    r = client.post("/system/quality", json={
        "line": "A03", "defect_rate": 2.8, "defect_type": "crack",
        "total_count": 1000, "defect_count": 28, "company_id": company_id}, headers=headers)
    check("写入质量数据", r.status_code == 200)

    # 14. 渠道
    print("\n[14] 外部渠道")
    r = client.post("/channels/email/generate", json={
        "company": "Alpha Chemical Lab GmbH", "product": "laboratory glassware"},
        headers=headers)
    check("AI 生成开发信", r.status_code == 200)

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)

    # 清理（容错：回收站不可用时不阻塞）
    for f in [test_doc, _TEST_DB]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
