"""冒烟测试：对公转账 24h 缓冲——过期自动取消升级 + 审核驳回回退套餐"""
import os, sys, time
os.environ["USE_SQLITE"] = "1"
os.environ["TESTING"] = "1"
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from main import app
from database import Base, engine, SessionLocal, _lightweight_migrate
from models import User, Company, Bill

Base.metadata.create_all(bind=engine)
_lightweight_migrate()

client = TestClient(app)

def new_company():
    email = f"grace_{int(time.time()*1000)}@example.com"
    r = client.post("/auth/register", json={
        "company": "缓冲测试公司", "email": email, "password": "Test@12345", "name": "测试员"})
    assert r.status_code in (200, 201), r.text
    r = client.post("/auth/login", json={"email": email, "password": "Test@12345"})
    token = r.json()["access_token"]
    cid = r.json().get("company_id") or r.json().get("user", {}).get("company_id")
    return token, cid

# 平台超管
_s = SessionLocal()
_s.query(User).filter(User.email == "platform@demo.com").update({"company_id": None})
_s.commit()
_s.close()
r = client.post("/auth/login", json={"email": "platform@demo.com", "password": "Platform@123"})
HP = {"Authorization": f"Bearer {r.json()['access_token']}"}

def submit_pay(token, cid, plan="professional"):
    H = {"Authorization": f"Bearer {token}"}
    r = client.post("/billing/upgrade", headers=H, json={"company_id": cid, "plan": plan})
    bill_no = r.json()["bill_no"]
    tiny_png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    r = client.post("/billing/pay", headers=H,
                    data={"bill_no": bill_no, "note": "转账测试"},
                    files={"voucher": ("v.png", __import__("io").BytesIO(tiny_png), "image/png")})
    assert r.status_code == 200 and r.json()["status"] == "pending_review", r.text
    return bill_no

def company_plan(cid):
    _s = SessionLocal()
    try:
        c = _s.query(Company).filter(Company.id == cid).first()
        return c.plan
    finally:
        _s.close()

# ===== 场景 1：24h 缓冲过期 → 自动取消升级（套餐回退）=====
print("--- 场景 1：缓冲过期自动取消 ---")
token, cid = new_company()
prev = company_plan(cid)
print(f"[OK] 企业 {cid} 原套餐: {prev}")
bill_no = submit_pay(token, cid)
assert company_plan(cid) == "professional", f"缓冲期内应临时生效: {company_plan(cid)}"
print("[OK] 提交凭证后套餐临时生效: professional")

# 手动把缓冲截止时间改到过去，模拟过期
_s = SessionLocal()
b = _s.query(Bill).filter(Bill.bill_no == bill_no).first()
b.grace_expires_at = datetime.utcnow() - timedelta(minutes=1)
_s.commit()
_s.close()

from tasks.billing import expire_grace_bills
res = expire_grace_bills()
assert res["expired"] >= 1, res
print("[OK] 定时任务执行，过期账单数:", res["expired"])

assert company_plan(cid) == prev, f"过期后应回退到 {prev}: {company_plan(cid)}"
_s = SessionLocal()
b = _s.query(Bill).filter(Bill.bill_no == bill_no).first()
assert b.status == "expired", b.status
_s.close()
print(f"[OK] 账单状态 expired，套餐已回退至 {prev}")

# ===== 场景 2：财务驳回 → 套餐回退 =====
print("--- 场景 2：审核驳回回退 ---")
token2, cid2 = new_company()
prev2 = company_plan(cid2)
bill_no2 = submit_pay(token2, cid2)
assert company_plan(cid2) == "professional"
r = client.post(f"/platform/bills/{bill_no2}/review", headers=HP,
                json={"action": "reject", "note": "未查询到该笔到账"})
assert r.status_code == 200 and r.json()["status"] == "rejected", r.text
assert company_plan(cid2) == prev2, f"驳回后应回退: {company_plan(cid2)}"
_s = SessionLocal()
b2 = _s.query(Bill).filter(Bill.bill_no == bill_no2).first()
assert b2.status == "rejected" and b2.review_note == "未查询到该笔到账"
_s.close()
print(f"[OK] 驳回后套餐回退至 {prev2}，驳回原因已记录")

# ===== 场景 3：待审核时不可重复下单 =====
print("--- 场景 3：待审核账单阻塞重复升级 ---")
token3, cid3 = new_company()
bill_no3 = submit_pay(token3, cid3)
H3 = {"Authorization": f"Bearer {token3}"}
r = client.post("/billing/upgrade", headers=H3, json={"company_id": cid3, "plan": "enterprise"})
assert r.status_code == 400, r.text
print("[OK] 存在待审核账单时重复升级被拒:", r.json()["detail"])

print("\n===== 对公转账 24h 缓冲冒烟测试全部通过 =====")
