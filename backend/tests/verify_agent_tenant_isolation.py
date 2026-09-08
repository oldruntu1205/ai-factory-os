"""
verify_agent_tenant_isolation.py —— 跨租户 Agent 隔离回归（安全修复验证）

验证目标（docs/review/fix-plan-1-agent-tenant-isolation.md）：
1. 企业 A/B 可各自注册同名自定义 Agent（DB 复合唯一 (company_id,name) 允许共存）
2. B 重复注册同名不再覆盖 A 的 manifest（原 P1 主攻击链）
3. A 的 list/get 只暴露平台级 + 本企业私有（不泄露 B 的 system_prompt）
4. A 注销本企业 Agent 不影响 B（属主定位删除）
5. 平台级内置 Agent（company_id=0）对所有企业仍可见可解析
6. registry.resolve_agent 按企业解析私有优先、平台级回退

运行：backend 目录下 python tests/verify_agent_tenant_isolation.py
"""
import os, sys, time
os.environ["USE_SQLITE"] = "1"
os.environ["TESTING"] = "1"
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient
from main import app
from database import Base, engine, SessionLocal, _lightweight_migrate
from models import User, Company, AgentManifestRecord
from auth.security import hash_password

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")

Base.metadata.create_all(bind=engine)
_lightweight_migrate()
from main import _init_hermes, _seed_demo_data
_seed_demo_data()          # 幂等：确保 admin@demo.com（公司1）与平台超管存在
_init_hermes()

client = TestClient(app)
_s = SessionLocal()

# ---- 准备：公司 A（demo 企业1）+ 公司 B（新建）----
# A 登录
ra = client.post("/auth/login", json={"email": "admin@demo.com", "password": "123456"})
assert ra.status_code == 200, ra.text
HA = {"Authorization": f"Bearer {ra.json()['access_token']}"}
company_a = ra.json()["company_id"]

# B：平台超管建公司 + admin 用户（role=admin）
_s.query(User).filter(User.email == "platform@demo.com").update({"company_id": None})
_s.commit()
rp = client.post("/auth/login", json={"email": "platform@demo.com", "password": "Platform@123"})
assert rp.status_code == 200, rp.text
HP = {"Authorization": f"Bearer {rp.json()['access_token']}"}

tag = int(time.time() * 1000) % 1000000
co_b = _s.query(Company).filter(Company.name == f"隔离验证公司{tag}").first()
if not co_b:
    co_b = Company(name=f"隔离验证公司{tag}", plan="enterprise", max_agents=10)
    _s.add(co_b); _s.commit(); _s.refresh(co_b)
    _s.add(User(email=f"admin_b_{tag}@test.com", password=hash_password("Test@12345"),
                name="B管理员", role="admin", company_id=co_b.id))
    _s.commit()
rb = client.post("/auth/login", json={"email": f"admin_b_{tag}@test.com", "password": "Test@12345"})
assert rb.status_code == 200, rb.text
HB = {"Authorization": f"Bearer {rb.json()['access_token']}"}
company_b = rb.json()["company_id"]

agent_name = f"iso_agent_{tag}"

print(f"\n===== Agent 跨租户隔离验证（A=company {company_a}，B=company {company_b}）=====")

# ---- 1. A/B 各自注册同名自定义 Agent ----
r = client.post("/hermes/agent/register", headers=HA, json={
    "name": agent_name, "industry": "general", "label": "C1_ISO",
    "description": "A 企业私有", "tools": ["knowledge.search"],
    "system_prompt": "你是公司1专属助手。", })
check("A 注册成功", r.status_code == 200, r.text)
r = client.post("/hermes/agent/register", headers=HB, json={
    "name": agent_name, "industry": "general", "label": "C2_ISO",
    "description": "B 企业私有", "tools": ["knowledge.search"],
    "system_prompt": "你是公司2专属助手。", })
check("B 注册同名 Agent 成功（复合唯一允许共存）", r.status_code == 200, r.text)

# ---- 2. DB 两行共存（不再整行覆盖）----
rows = _s.query(AgentManifestRecord).filter(
    AgentManifestRecord.name == agent_name).all()
check("DB 中两行共存 (A/B)", len(rows) == 2, f"rows={[(x.company_id, x.name) for x in rows]}")
check("两行属主正确", {x.company_id for x in rows} == {company_a, company_b})

# ---- 3. list 隔离：A 只见自己的私有版本，B 只见自己的 ----
r = client.get("/hermes/agent/list", headers=HA)
a_agents = {a["name"]: a for a in r.json()["agents"]}
check("A list 含内置 sales_agent（平台级）", "sales_agent" in a_agents)
check("A list 中 iso agent label=C1_ISO（不泄露 B）",
      a_agents.get(agent_name, {}).get("label") == "C1_ISO",
      str(a_agents.get(agent_name)))
r = client.get("/hermes/agent/list", headers=HB)
b_agents = {a["name"]: a for a in r.json()["agents"]}
check("B list 中 iso agent label=C2_ISO",
      b_agents.get(agent_name, {}).get("label") == "C2_ISO",
      str(b_agents.get(agent_name)))

# ---- 4. get 详情按属主解析（不泄露他企 system_prompt）----
r = client.get(f"/hermes/agent/{agent_name}", headers=HA)
check("A 详情 system_prompt=公司1", r.status_code == 200 and "公司1" in r.json().get("system_prompt", ""), r.text)
r = client.get(f"/hermes/agent/{agent_name}", headers=HB)
check("B 详情 system_prompt=公司2", r.status_code == 200 and "公司2" in r.json().get("system_prompt", ""), r.text)

# ---- 5. 原漏洞回归：B 再次注册同名（不同内容）不得覆盖 A ----
r = client.post("/hermes/agent/register", headers=HB, json={
    "name": agent_name, "industry": "general", "label": "C2_ISO_v2",
    "description": "B 更新", "tools": ["knowledge.search"],
    "system_prompt": "你是公司2专属助手 v2。", })
check("B 重复注册（自身 upsert）成功", r.status_code == 200, r.text)
r = client.get(f"/hermes/agent/{agent_name}", headers=HA)
check("A 的 manifest 未被 B 覆盖（仍公司1）",
      r.json().get("system_prompt", "").startswith("你是公司1"), r.text)
# B 自身已是 v2
r = client.get(f"/hermes/agent/{agent_name}", headers=HB)
check("B 的 manifest 已更新为 v2（自身 upsert 生效）",
      "v2" in r.json().get("system_prompt", ""), r.text)

# ---- 6. registry.resolve_agent 执行解析：私有优先、平台级回退 ----
import hermes.runtime as _rt
reg = _rt.runtime.agent_registry
m_a = reg.resolve_agent(agent_name, company_a)
m_b = reg.resolve_agent(agent_name, company_b)
check("resolve(A) → A 私有 manifest", m_a is not None and m_a.company_id == company_a)
check("resolve(B) → B 私有 manifest", m_b is not None and m_b.company_id == company_b)
m_c = reg.resolve_agent("sales_agent", company_a)   # 内置为平台级
check("resolve(内置 sales_agent, A) → 平台级(0) 回退",
      m_c is not None and m_c.company_id == 0 and m_c.name == "sales_agent")
m_none = reg.resolve_agent("no_such_agent_xyz", company_a)
check("resolve(不存在) → None", m_none is None)

# ---- 7. 注销属主隔离：A 注销不影响 B ----
r = client.delete(f"/hermes/agent/{agent_name}", headers=HA)
check("A 注销本企业 Agent 成功", r.status_code == 200, r.text)
r = client.get(f"/hermes/agent/{agent_name}", headers=HA)
check("A 注销后 A 详情 404", r.status_code == 404, r.text)
r = client.get(f"/hermes/agent/{agent_name}", headers=HB)
check("B 的 Agent 不受 A 注销影响（仍公司2）",
      r.status_code == 200 and "公司2" in r.json().get("system_prompt", ""), r.text)
# B 注销自己
r = client.delete(f"/hermes/agent/{agent_name}", headers=HB)
check("B 注销成功", r.status_code == 200, r.text)
rows = _s.query(AgentManifestRecord).filter(
    AgentManifestRecord.name == agent_name).all()
check("DB 中两行均已清除", len(rows) == 0)

# ---- 8. 他企管理员无法注销对方私有 Agent（正名不存在即 404，不误删平台级）----
r = client.post("/hermes/agent/register", headers=HA, json={
    "name": f"a_only_{tag}", "tools": [], "system_prompt": "A 私有", "label": "A_only"})
check("A 注册仅 A 私有 Agent", r.status_code == 200, r.text)
r = client.delete(f"/hermes/agent/a_only_{tag}", headers=HB)
check("B 注销 A 的私有 Agent → 404（拒绝）", r.status_code == 404, r.text)
r = client.get(f"/hermes/agent/a_only_{tag}", headers=HA)
check("A 的私有 Agent 仍存在", r.status_code == 200, r.text)
client.delete(f"/hermes/agent/a_only_{tag}", headers=HA)   # 清理

_s.close()
print(f"\n===== 结果：PASS {PASS} / FAIL {FAIL} =====")
sys.exit(1 if FAIL else 0)
