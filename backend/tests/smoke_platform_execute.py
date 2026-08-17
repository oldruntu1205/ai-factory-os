"""验证：平台超管登录 company_id=0 + Hermes 执行不再 422"""
import os, sys
os.environ["USE_SQLITE"] = "1"
os.environ["TESTING"] = "1"
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient
from main import app, _init_hermes
from database import Base, engine, SessionLocal, _lightweight_migrate
from models import User

Base.metadata.create_all(bind=engine)
_lightweight_migrate()
_init_hermes()

client = TestClient(app)

# 确保平台超管 company_id=None（种子污染防护）
_s = SessionLocal()
_s.query(User).filter(User.email == "platform@demo.com").update({"company_id": None})
_s.commit()
_s.close()

r = client.post("/auth/login", json={"email": "platform@demo.com", "password": "Platform@123"})
assert r.status_code == 200, r.text
body = r.json()
print("login company_id:", body["company_id"], "(应=0)")
assert body["company_id"] == 0, "平台超管 company_id 应为 0"
HP = {"Authorization": f"Bearer {body['access_token']}"}

# 前端 Number("0")=0 → 执行不再 422
r = client.post("/hermes/execute", headers=HP, json={
    "agent_name": "quality_agent",
    "message": "你能干啥",
    "company_id": 0,
})
print("execute status:", r.status_code)
if r.status_code != 200:
    print("detail:", r.text)
assert r.status_code == 200, f"执行应成功: {r.text}"
print("[OK] 平台超管执行成功:", str(r.json()["result"])[:60])

print("\n===== 平台超管 company_id 修复验证通过 =====")
