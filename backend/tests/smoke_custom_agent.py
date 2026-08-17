"""冒烟测试：自定义 Agent 注册即可用（prompt 驱动编排 + 持久化 + 注销）"""
import os, sys, time
os.environ["USE_SQLITE"] = "1"
os.environ["TESTING"] = "1"
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient
from main import app
from database import Base, engine, SessionLocal, _lightweight_migrate
from models import User

Base.metadata.create_all(bind=engine)
_lightweight_migrate()

# 测试环境不触发 startup 事件：手动装配 Hermes Runtime
from main import _init_hermes
_init_hermes()

client = TestClient(app)

# 平台超管登录
_s = SessionLocal()
_s.query(User).filter(User.email == "platform@demo.com").update({"company_id": None})
_s.commit()
_s.close()
r = client.post("/auth/login", json={"email": "platform@demo.com", "password": "Platform@123"})
assert r.status_code == 200, r.text
HP = {"Authorization": f"Bearer {r.json()['access_token']}"}
print("[OK] 平台超管登录")

# 1. 注册自定义 Agent（带系统提示词 + 工具白名单）
name = f"test_purchaser_{int(time.time()) % 100000}"
r = client.post("/hermes/agent/register", headers=HP, json={
    "name": name,
    "version": "1.0",
    "industry": "glassware",
    "label": "测试采购Agent",
    "description": "采购专家测试",
    "tools": ["crm.get_customer", "erp.get_stock"],
    "system_prompt": "你是采购专家。查询客户和库存后给出采购建议。",
})
assert r.status_code == 200, f"register: {r.status_code} {r.text}"
assert r.json()["mode"] == "prompt_driven", r.text
print("[OK] 注册成功 mode=prompt_driven:", name)

# 2. Agent 列表可见
r = client.get("/hermes/agent/list", headers=HP)
agents = {a["name"]: a for a in r.json()["agents"]}
assert name in agents and agents[name]["system_prompt"], r.text
print("[OK] 列表可见且 system_prompt 返回")

# 3. 持久化：重新装配 runtime（模拟重启，会走 load_custom）
import hermes.runtime as _rt
_rt.runtime = None
_init_hermes()   # main._init_hermes 内部会 init_hermes + load_custom
r = client.get("/hermes/agent/list", headers=HP)
agents2 = {a["name"]: a for a in r.json()["agents"]}
assert name in agents2, "重启后自定义 Agent 应仍在（持久化失败）"
print("[OK] 重启后自定义 Agent 持久化生效")

# 4. 执行：prompt 驱动编排（工具白名单校验）
# 用企业账号执行（平台超管 company_id=0 无认证）；注册企业并标记认证
import time as _t
_em = f"agentuser_{int(_t.time()*1000)}@example.com"
r = client.post("/auth/register", json={
    "company": "Agent测试公司", "email": _em, "password": "Test@12345", "name": "t"})
assert r.status_code in (200, 201), r.text
r = client.post("/auth/login", json={"email": _em, "password": "Test@12345"})
assert r.status_code == 200, r.text
token = r.json()["access_token"]
cid = r.json().get("company_id") or r.json().get("user", {}).get("company_id")
_s = SessionLocal()
from models import Company
_s.query(Company).filter(Company.id == cid).update({"license_verified": True})
_s.commit()
_s.close()
H = {"Authorization": f"Bearer {token}"}
print(f"[OK] 企业 {cid} 已认证")

r = client.post("/hermes/execute", headers=H, json={
    "agent_name": name,
    "message": "查一下客户 Alpha Chem 的库存情况",
    "company_id": cid,
})
assert r.status_code == 200, f"execute: {r.status_code} {r.text}"
print("[OK] 执行成功（LLM 返回内容或工具结果）:", str(r.json()["result"])[:60])

# 5. 注销
r = client.delete(f"/hermes/agent/{name}", headers=HP)
assert r.status_code == 200, f"unregister: {r.status_code} {r.text}"
print("[OK] 注销成功")

# 6. 注销后列表不可见 + 持久化记录已删
r = client.get("/hermes/agent/list", headers=HP)
assert name not in {a["name"] for a in r.json()["agents"]}, "注销后应不可见"
_s = SessionLocal()
from models import AgentManifestRecord
row = _s.query(AgentManifestRecord).filter(AgentManifestRecord.name == name).first()
assert row is None, "持久化记录应删除"
_s.close()
print("[OK] 注销后内存与持久化均已清除")

# 7. 内置 Agent 名不可覆盖
r = client.post("/hermes/agent/register", headers=HP, json={
    "name": "sales_agent", "label": "x", "tools": ["crm.get_customer"],
    "system_prompt": "x"})
assert r.status_code == 400, r.text
print("[OK] 内置 Agent 名覆盖被拒")

print("\n===== 自定义 Agent 注册即可用冒烟测试全部通过 =====")
