"""冒烟测试：开票闭环（保存开票信息 → 升级套餐 → 支付 → 申请开票 → 平台开具）"""
import os, sys, time
os.environ["USE_SQLITE"] = "1"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from main import app
from database import Base, engine, _lightweight_migrate

# 建表 + 轻量迁移（新列/新表）
Base.metadata.create_all(bind=engine)
_lightweight_migrate()

client = TestClient(app)

def reg_and_login():
    email = f"inv_{int(time.time()*1000)}@example.com"
    pwd = "Test@12345"
    # 注册
    r = client.post("/auth/register", json={
        "company": "开票测试公司", "email": email, "password": pwd,
        "name": "张三",
    })
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text}"
    r = client.post("/auth/login", json={"email": email, "password": pwd})
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    return r.json()["access_token"], r.json().get("company_id") or r.json().get("user", {}).get("company_id")

token, company_id = reg_and_login()
H = {"Authorization": f"Bearer {token}"}
assert company_id, f"no company_id: {token}"
print(f"[OK] 注册/登录 company_id={company_id}")

# 1. 保存开票信息
r = client.put(f"/billing/invoice-info?company_id={company_id}", headers=H, json={
    "invoice_title": "开票测试公司（宁波）有限公司",
    "invoice_tax_no": "91330200MA2XXXXXXX",
    "invoice_bank": "中国工商银行宁波分行",
    "invoice_bank_account": "3901111111111111111",
    "invoice_address": "浙江省宁波市海曙区",
    "invoice_phone": "0574-88888888",
    "invoice_type": "normal",
})
assert r.status_code == 200, f"save info: {r.status_code} {r.text}"
print("[OK] 保存开票信息:", r.json()["message"])

# 2. 读取开票信息
r = client.get(f"/billing/invoice-info?company_id={company_id}", headers=H)
assert r.status_code == 200 and r.json()["info"]["category"] == "服务费", r.text
print("[OK] 读取开票信息 category=", r.json()["info"]["category"])

# 3. 未支付账单申请开票 → 应拒绝
r = client.post("/billing/invoice-request", headers=H, json={"bill_no": "BILLNOPE"})
assert r.status_code == 404, f"expect 404: {r.text}"
print("[OK] 不存在的账单申请被拒")

# 4. 升级套餐 → 待支付账单
r = client.post("/billing/upgrade", headers=H, json={"company_id": company_id, "plan": "professional"})
assert r.status_code == 200, f"upgrade: {r.status_code} {r.text}"
bill_no = r.json()["bill_no"]
assert r.json()["status"] == "unpaid"
print("[OK] 生成待支付账单", bill_no)

# 5. 未支付时申请开票 → 应拒绝
r = client.post("/billing/invoice-request", headers=H, json={"bill_no": bill_no})
assert r.status_code == 400, f"expect 400: {r.text}"
print("[OK] 未支付账单申请被拒:", r.json()["detail"])

# 6. 支付账单
r = client.post(f"/billing/pay?bill_no={bill_no}", headers=H)
assert r.status_code == 200, f"pay: {r.status_code} {r.text}"
print("[OK] 支付成功")

# 7. 申请开票
r = client.post("/billing/invoice-request", headers=H, json={"bill_no": bill_no})
assert r.status_code == 200, f"apply: {r.status_code} {r.text}"
req_no = r.json()["req_no"]
assert r.json()["category"] == "服务费", r.text
print("[OK] 开票申请提交:", req_no, "类别:", r.json()["category"])

# 8. 重复申请 → 应拒绝
r = client.post("/billing/invoice-request", headers=H, json={"bill_no": bill_no})
assert r.status_code == 400, f"expect dup reject: {r.text}"
print("[OK] 重复申请被拒")

# 9. 企业侧申请列表
r = client.get(f"/billing/invoice-requests?company_id={company_id}", headers=H)
assert r.status_code == 200 and len(r.json()["requests"]) == 1, r.text
print("[OK] 企业侧申请列表 1 条, status:", r.json()["requests"][0]["status"])

# 10. 平台侧（超管）查看并开具
# 确保种子超管 company_id=None（防测试库历史数据污染）
from database import SessionLocal
from models import User
_s = SessionLocal()
_s.query(User).filter(User.email == "platform@demo.com").update({"company_id": None})
_s.commit()
_s.close()

r = client.post("/auth/login", json={"email": "platform@demo.com", "password": "Platform@123"})
assert r.status_code == 200, f"platform login: {r.status_code} {r.text}"
token_p = r.json()["access_token"]
HP = {"Authorization": f"Bearer {token_p}"}
r = client.get("/platform/invoice-requests", headers=HP)
assert r.status_code == 200, f"platform list: {r.status_code} {r.text}"
reqs = r.json()["requests"]
assert len(reqs) >= 1, "平台侧应有申请"
target = [x for x in reqs if x["req_no"] == req_no]
assert target, f"平台侧未见 {req_no}"
rid = target[0]["id"]
print("[OK] 平台侧看到申请:", req_no, "status:", target[0]["status"])

# 开具
r = client.post(f"/platform/invoice-requests/{rid}/issue", headers=HP, params={"invoice_no": "2026080012345678"})
assert r.status_code == 200, f"issue: {r.status_code} {r.text}"
print("[OK] 平台开具:", r.json()["message"])

# 企业侧确认状态
r = client.get(f"/billing/invoice-requests?company_id={company_id}", headers=H)
rr = r.json()["requests"][0]
assert rr["status"] == "issued" and rr["invoice_no"] == "2026080012345678", r.text
print("[OK] 企业侧看到已开票 + 发票号:", rr["invoice_no"])

# 驳回流程（对已开票的不允许重复操作）
r = client.post(f"/platform/invoice-requests/{rid}/reject", headers=HP, params={"reason": "x"})
assert r.status_code == 400, f"expect 400: {r.text}"
print("[OK] 已开具申请不可再驳回")

print("\n===== 开票闭环冒烟测试全部通过 =====")
