# 修复方案 1 · 自定义 Agent 跨租户接管（B P1-1/P1-2 + D P1-4）

> 依据：`docs/review/B-hermes-core.md` P1-1/P1-2、`docs/review/D-agent-llm.md` P1-4，主会话已逐行验证
> （`models.py:723` name 全局唯一、`agent_registry.py:73-74` persist 查重无属主、`api/hermes.py` list/get/delete 无属主过滤）。
> 状态：**方案（未改代码）**。改动量：8 个文件 + 1 个迁移 + 测试。

## 1. 根因与语义约定

### 根因
| 层 | 现状缺陷 | 位置 |
|---|---|---|
| DB | `agent_manifests.name` **全局唯一**（非 `(company_id,name)` 复合） | `models.py:723` |
| 持久化 | `persist()` 按 name 查重不带 company_id → 命中他租户行即整行覆盖（含 company_id） | `agent_registry.py:73-86` |
| 内存 | `_agents` 以 `name` 为 key → 同进程多租户同名互斥 | `agent_registry.py:23` |
| API | register 无妨（已传属主）；**list/get 无企业过滤（泄露他企 system_prompt）、delete 无属主校验（可删他企）**、execute 按全局 name 解析（可能执行被覆盖后的他企 manifest） | `api/hermes.py:175-215` |
| 执行 | `start_agent`/RBAC 均 `get_agent(name)` 无属主 | `runtime.py:44`、`rbac.py:159` |

### 语义约定（修复后）
- **平台级** `company_id=0`：内置 6 Agent（`main.py:450-462` 注册，未传 company_id → 默认 0）+ marketplace 包级 Agent（`marketplace.py:156-163` 同上）。**所有租户可见、可执行**（现状即此，需保持）。
- **企业私有** `company_id>0`：企业自定义 Agent（`api/hermes.py:115` 传 `current["company_id"]`）。**仅本企业可见、可执行**。
- **执行解析**：`resolve_agent(name, cid)` = 先查 `(cid, name)` 私有 manifest，未命中回退 `(0, name)` 平台级 → 内置 Agent 无需每租户复制，行为兼容；企业同名私有 Agent 天然互不干扰。

## 2. 改动清单（按依赖序）

### ① `backend/models.py` — DB 复合唯一
```python
# 改前（L718-734）
class AgentManifestRecord(Base):
    __tablename__ = "agent_manifests"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, index=True)   # ← 全局唯一
    ...
# 改后
from sqlalchemy import UniqueConstraint   # 顶部已有 import 则复用
class AgentManifestRecord(Base):
    __tablename__ = "agent_manifests"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_agent_manifests_company_name"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(100), index=True)                 # 去掉 unique=True
    ...
```
> 存量库不会由 create_all 变更，需 ⑧ 迁移补 DDL。

### ② `backend/hermes/registry/models.py` — `to_dict()` 补属主（便于校验/调试）
```python
def to_dict(self) -> dict:
    return { ..., "company_id": self.company_id, }   # 追加
```

### ③ `backend/hermes/registry/agent_registry.py` — 内存 key 属主化（核心）
```python
class AgentRegistry:
    def __init__(self):
        self._agents: Dict[tuple, AgentManifest] = {}   # key = (company_id, name)

    @staticmethod
    def _key(company_id, name):
        return (int(company_id or 0), name)

    # 注册 / 注销 / 查询 全部 key 化
    def register(self, manifest):
        self._agents[self._key(manifest.company_id, manifest.name)] = manifest

    def unregister(self, name, company_id=0) -> bool:
        return self._agents.pop(self._key(company_id, name), None) is not None

    def get_agent(self, name, company_id=0):
        agent = self._agents.get(self._key(company_id, name))
        if agent is not None:
            return agent
        return self._load_one_from_db(name, company_id)   # 多 worker 兜底，同样带属主

    def resolve_agent(self, name, company_id):
        """实际生效 manifest：企业私有优先，回退平台级(0)"""
        cid = int(company_id or 0)
        a = self.get_agent(name, cid)
        if a is None and cid != 0:
            a = self.get_agent(name, 0)
        return a

    def _load_one_from_db(self, name, company_id=0):
        # query filter: name==name AND company_id==company_id  （原 L121-122 补条件）
        # 载入后缓存 key = self._key(company_id, row.name)

    def load_custom(self):
        # 每行 → self._agents[self._key(row.company_id or 0, row.name)] = manifest
        # （key 化后不同租户同名不互斥；重启多 worker 各自全量加载，语义一致）

    def list_agents(self, industry=None, status=None, company_id=None):
        # 内存遍历过滤：company_id is None → 全量；否则 (a.company_id in (0, company_id))
        # DB 合并与 _is_custom 同步删除逻辑同步改按 key 判断

    def persist(self, manifest):   # 原 L67-91 属主化
        # row = query.filter(name==manifest.name, company_id==manifest.company_id).first()
        # 命中 → 更新本行；未命中 → 新建。 他租户行永不可达。
        # row.company_id = manifest.company_id 保留（两条件本就同属主）
```
> 不变量：同一 `(company_id, name)` 才互斥 → DB 复合唯一兜底 + 内存天然隔离。

### ④ `backend/hermes/runtime.py` — 执行解析走 resolve
```python
# start_agent L44
agent = self.agent_registry.resolve_agent(agent_name, company_id)   # 私有优先→平台级
# execute() 无需改（走 start_agent；runtime_ctx["company_id"] 已带）
```

### ⑤ `backend/hermes/registry/agent_registry.py` — `invoke_agent` 同改
```python
def invoke_agent(self, name, message, runtime_ctx):
    manifest = self.resolve_agent(name, runtime_ctx.get("company_id", 0))
    if not manifest:
        raise ValueError(f"Agent [{name}] 未注册")
    ...
```

### ⑥ `backend/api/hermes.py` — API 属主校验（堵泄露面）
```python
# DELETE /agent/{name}  L175-198
def unregister_agent(name, current=...):
    _require_admin(current)
    if name in _BUILTIN_AGENT_NAMES:
        raise HTTPException(400, "内置 Agent 不可注销")
    cid = current["company_id"]
    reg = get_runtime().agent_registry
    if not reg.unregister(name, company_id=cid):
        # 平台级（marketplace 包级，company_id=0）仅平台超管可注销
        if cid == 0 or not reg.unregister(name, company_id=0):
            raise HTTPException(404, "本企业无此自定义 Agent")
    # DB 删除加属主条件
    row = db.query(AgentManifestRecord).filter(
        AgentManifestRecord.name == name,
        AgentManifestRecord.company_id == cid).first()
    if row: db.delete(row); db.commit()
    return {"message": f"agent [{name}] unregistered"}

# GET /agent/list  L201-206
agents = reg.list_agents(industry=industry, company_id=current["company_id"])
#   → 仅平台级(0) + 本企业私有；他企 system_prompt 不可见

# GET /agent/{name}  L209-215
agent = reg.resolve_agent(name, current["company_id"])
#   → 本企业私有优先、回退平台级；他企私有恒 404（不泄露）
```

### ⑦ `backend/hermes/permissions/rbac.py` + `backend/api/marketplace.py` — 关联调用点
```python
# rbac.py assert_tool_allowed L158-159（manifest 工具声明校验对齐实际生效的 manifest）
registry = get_runtime().agent_registry
manifest = registry.resolve_agent(agent_name, tenant_id)

# marketplace.py L155（查平台级是否已注册，语义不变）
if not runtime.agent_registry.get_agent(manifest_name, company_id=0):
```

### ⑧ `backend/database.py` — `_lightweight_migrate()` 补索引迁移（幂等）
```python
# 在 _INDEX_MIGRATIONS 区（PG）与 SQLite 分支各加：
#   SQLite:
#     DROP INDEX IF EXISTS 不支持 → 用 PRAGMA index_list('agent_manifests') 查存在则 DROP INDEX ix_agent_manifests_name
#     CREATE UNIQUE INDEX uq_agent_manifests_company_name ON agent_manifests(company_id, name)
#   PostgreSQL:
#     DROP INDEX IF EXISTS ix_agent_manifests_name;
#     CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_manifests_company_name ON agent_manifests(company_id, name);
# 迁移前先查重复：SELECT company_id, name, COUNT(*) FROM agent_manifests GROUP BY company_id, name HAVING COUNT(*)>1
# （旧 schema name 全局唯一 → 理论无冲突；若演示库有 company_id 缺失/0 混杂，需先人工确认归属）
```
> 注：`_lightweight_migrate()` 是既有幂等迁移挂载点（`database.py:132` 起），新增 DDL 走同机制。

## 3. 不修改项（防 scope 膨胀）
- register 端点逻辑（`api/hermes.py:84-172`）：company_id 已取 current（L115），persist 属主化后即天然正确；内置名禁注册（L97）保留。
- 前端 `Hermes.jsx`/`Agents.jsx`：list 可见集合语义不变（平台 + 本企业），无需改动；register 表单不涉及 company_id。
- marketplace 卸载/安装既有路径：仅 L155 查询对齐（见⑦）。

## 4. 验证方案
### 新增测试（建议追加到 `backend/tests/test_hermes.py` 或独立 `test_agent_tenant_isolation.py`）
1. **跨租户同名共存**：企业 A(1) 注册 `my_agent`（system_prompt 带标记 A），企业 B(2) 注册同名（标记 B）→ DB 两行、A/B `GET /agent/list` 各只见本企业私有 + 平台级。
2. **覆盖漏洞回归**：B 再注册同名 → A 的 manifest（tools/system_prompt）不被篡改（原 P1 主场景）。
3. **执行隔离**：`POST /execute`（A 会话）调 `my_agent` 输出含 A 标记；B 会话输出含 B 标记。
4. **注销属主**：A 注销 `my_agent` → B 的仍存在可执行；他企员工 DELETE 他企私有 → 404。
5. **平台级兼容**：内置 `sales_agent`（company_id=0）在 A/B 均可执行；list 对 A/B 均可见。
6. **重启保持**：模拟 `load_custom()` 重载 → 隔离依旧。
7. **泄露面回归**：A 员工 GET `/agent/<B私有名>` → 404（system_prompt 不泄露）。
### 回归
`python -m pytest backend/tests -q`（存量 test_hermes/test_marketplace/test_api 全绿）+ 启动冒烟（`smoke_custom_agent.py` 对齐新语义）。

## 5. 回滚
- 代码：git revert（本方案改动集中在 8 个文件，无数据迁移后的代码依赖）。
- 数据：迁移前先 `pg_dump`/备份 `agent_manifests` 表；回滚时 `DROP INDEX uq_agent_manifests_company_name; CREATE UNIQUE INDEX ix_agent_manifests_name ON agent_manifests(name)`（若期间出现跨租户同名数据，回滚前须先合并/清理，属预期迁移风险）。

## 6. 遗留风险
- 平台级（company_id=0）自定义语义：marketplace 动态注册的包级 Agent 仍为全局共享，若两个包注册同名不同 industry 会被后注册覆盖（现状行为，非本次范围；如需包级命名空间可后续按包加前缀）。
- `_tenant_agents`（RBAC 授权名单）仍按 name 记——同名私有/平台级共享一个授权名条目（`allow_agent(cid, name)`），语义为「该租户可用此名解析出的 manifest」，与 resolve 一致，无需改。
- 演示库若有 company_id=0 的企业自定义 Agent 存量行（早期版本），迁移后会被视为平台级；上线前人工核对归属（迁移前查询步骤已列出）。
