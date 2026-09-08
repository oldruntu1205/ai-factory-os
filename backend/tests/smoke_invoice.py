"""冒烟测试：开票闭环（保存开票信息 → 升级套餐 → 支付 → 申请开票 → 平台开具）"""
import os, sys, time
os.environ["USE_SQLITE"] = "1"
os.environ["TESTING"] = "1"
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

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

# 6. 对公转账：查看付款账号
r = client.get("/billing/pay-account", headers=H)
assert r.status_code == 200 and r.json()["pay_method"] == "bank_transfer", r.text
print("[OK] 对公账户:", r.json()["account"]["account_name"])

# 7. 上传转账凭证 → pending_review + 套餐临时生效（24h 缓冲）
import io
tiny_png = b"\x89PNG\r\n\x1a\n" + b"0" * 64  # 极简 PNG（仅测上传链路）
r = client.post("/billing/pay", headers=H,
                data={"bill_no": bill_no, "note": "2026-08-18 对公转账"},
                files={"voucher": ("voucher.png", io.BytesIO(tiny_png), "image/png")})
assert r.status_code == 200, f"pay submit: {r.status_code} {r.text}"
assert r.json()["status"] == "pending_review", r.text
assert r.json()["grace_expires_at"], r.text
print("[OK] 凭证已提交 pending_review, 缓冲至:", r.json()["grace_expires_at"])

# 8. 重复提交 → 应拒绝
r = client.post("/billing/pay", headers=H,
                data={"bill_no": bill_no, "note": ""},
                files={"voucher": ("v2.png", io.BytesIO(tiny_png), "image/png")})
assert r.status_code == 400, f"expect dup submit reject: {r.text}"
print("[OK] 重复提交凭证被拒:", r.json()["detail"])

# 8.5 平台侧（超管）登录（供步骤 9 使用）
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
print("[OK] 平台超管登录")

# 9. 平台侧待审核列表
r = client.get("/platform/bills/pending-review", headers=HP)
assert r.status_code == 200, f"pending list: {r.status_code} {r.text}"
pb = [x for x in r.json()["bills"] if x["bill_no"] == bill_no]
assert pb and pb[0]["voucher_path"], r.text
print("[OK] 平台待审核列表见凭证:", pb[0]["voucher_path"])

# 10. 平台审核通过 → 正式生效
r = client.post(f"/platform/bills/{bill_no}/review", headers=HP,
                json={"action": "approve", "note": "已核对到账"})
assert r.status_code == 200 and r.json()["status"] == "paid", r.text
print("[OK] 平台审核通过:", r.json()["message"])

# 11. 已支付账单申请开票
r = client.post("/billing/invoice-request", headers=H, json={"bill_no": bill_no})
assert r.status_code == 200, f"apply: {r.status_code} {r.text}"
req_no = r.json()["req_no"]
assert r.json()["category"] == "服务费", r.text
print("[OK] 开票申请提交:", req_no, "类别:", r.json()["category"])

# 12. 重复申请 → 应拒绝
r = client.post("/billing/invoice-request", headers=H, json={"bill_no": bill_no})
assert r.status_code == 400, f"expect dup reject: {r.text}"
print("[OK] 重复申请被拒")

# 13. 企业侧申请列表
r = client.get(f"/billing/invoice-requests?company_id={company_id}", headers=H)
assert r.status_code == 200 and len(r.json()["requests"]) == 1, r.text
print("[OK] 企业侧申请列表 1 条, status:", r.json()["requests"][0]["status"])

# 14. 平台侧查看并自动开票（mock provider 生成 PDF）
r = client.get("/platform/invoice-requests", headers=HP)
assert r.status_code == 200, f"platform list: {r.status_code} {r.text}"
reqs = r.json()["requests"]
assert len(reqs) >= 1, "平台侧应有申请"
target = [x for x in reqs if x["req_no"] == req_no]
assert target, f"平台侧未见 {req_no}"
rid = target[0]["id"]
print("[OK] 平台侧看到申请:", req_no, "status:", target[0]["status"])

# 自动开票（无 invoice_no 参数，provider 生成发票号 + PDF）
r = client.post(f"/platform/invoice-requests/{rid}/issue", headers=HP)
assert r.status_code == 200, f"issue: {r.status_code} {r.text}"
inv_no = r.json()["invoice_no"]
assert inv_no and r.json()["pdf_url"].endswith(".pdf"), r.text
assert r.json()["provider"] == "mock", r.text
print("[OK] 自动开票成功:", inv_no, "| PDF:", r.json()["pdf_url"])

# 企业侧确认状态 + PDF 链接
r = client.get(f"/billing/invoice-requests?company_id={company_id}", headers=H)
rr = r.json()["requests"][0]
assert rr["status"] == "issued" and rr["invoice_no"] == inv_no, r.text
assert rr["pdf_url"], r.text
print("[OK] 企业侧看到已开票 + 发票号 + PDF:", rr["pdf_url"])

# 发票 PDF 鉴权下载（安全修复：匿名直接访问应 401；带企业/平台头可下载）
r = client.get(rr["pdf_url"])
assert r.status_code == 401, f"匿名应 401（防越权泄露）: {r.status_code}"
r = client.get(rr["pdf_url"], headers=H)
assert r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"), r.text
print("[OK] 发票 PDF 鉴权可下载 / 匿名 401")

# 已开具不可重复开
r = client.post(f"/platform/invoice-requests/{rid}/issue", headers=HP)
assert r.status_code == 400, f"expect 400: {r.text}"
print("[OK] 已开具不可重复开票")

# 驳回流程（对已开票的不允许重复操作）
r = client.post(f"/platform/invoice-requests/{rid}/reject", headers=HP, params={"reason": "x"})
assert r.status_code == 400, f"expect 400: {r.text}"
print("[OK] 已开具申请不可再驳回")

print("\n===== 开票闭环冒烟测试全部通过 =====")
