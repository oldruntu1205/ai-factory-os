# 中台三件套接入说明（MCP Gateway / 定时调度 / 审计回放）

三件套都在 Hermes 中台内实现，不含新增第三方依赖（cron 自行解析，未引入 APScheduler）。

| 能力 | 代码位置 | 对外端点 |
|---|---|---|
| MCP Gateway | `hermes/mcp/` | `POST /mcp`、`GET /mcp/tools`、`POST /mcp/tools/{name}/call`、`GET /mcp/health` |
| 定时调度 | `hermes/scheduler/` | `GET /hermes/scheduler/status`、`POST /hermes/scheduler/jobs/{name}/run`、`POST /hermes/scheduler/reload` |
| 审计回放 | `hermes/audit/` | `GET /hermes/audit/tool-calls`、`GET /hermes/audit/replay/{session_id}`、`GET /hermes/audit/stats` |

---

## 一、MCP Gateway

### 1.1 鉴权

两种 Bearer 都支持，推荐给外部客户端发**企业 API Key**（`api_keys` 表）：

```
Authorization: Bearer <企业API Key 或 用户JWT>
X-Hermes-Agent: quality_agent      # 可选：以某个 AI 员工的权限集调用
Mcp-Session-Id: <自定义会话id>      # 可选：响应会回显，用于审计回放
X-Company-Id: <企业id>              # 仅 platform_admin 需要
```

### 1.2 客户端配置示例（任意 MCP 客户端）

```json
{
  "mcpServers": {
    "hermes-ai-employee": {
      "type": "http",
      "url": "https://<你的域名>/mcp",
      "headers": {
        "Authorization": "Bearer <企业API Key>",
        "X-Hermes-Agent": "sales_agent"
      }
    }
  }
}
```

### 1.3 用 curl 验证

```bash
# 列工具
curl -s -X POST https://<域名>/mcp \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# 调工具
curl -s -X POST https://<域名>/mcp \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"erp.get_stock","arguments":{"product_id":"A1"}}}'
```

### 1.4 安全默认（改配置前请确认）

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `MCP_ENABLED` | `1` | 关闭后 `/mcp` 全部 403 |
| `MCP_ALLOW_WRITE` | `0` | **只读**。写工具（`crm.create_customer` / `email.send` / `quotation.create`）不在清单里且调用被拒 |

写工具名单与判定逻辑在 `hermes/mcp/schemas.py`（`WRITE_TOOLS` + 权限点 `:write/:send` 兜底）。

### 1.5 新增工具时要做的事

1. 在 `hermes/tool/tools.py` 注册 Tool（原有流程不变）。
2. 在 `hermes/mcp/schemas.py` 的 `SCHEMAS` 里补该工具的 `inputSchema`
   （不补也能用，客户端看到的是宽松 schema，但 LLM 填参会不准）。
3. 若是写操作，加进 `WRITE_TOOLS`。

---

## 二、定时调度

### 2.1 执行链路（记住这个解耦）

```
SchedulerService（到点判断）──enqueue──▶ TaskQueue ──▶ Worker ──▶ runtime.execute(agent, prompt)
```

调度器**不直接执行**任务，因此调度器重启不会丢正在跑的任务；Worker 挂了任务会堆在队列里（不丢）。

### 2.2 内置任务

| 任务名 | cron | AI 员工 | 做什么 |
|---|---|---|---|
| `production.delay_scan` | `0 8 * * 1-6` | production_agent | 工作日早 8 点巡检在制订单交期 |
| `quotation.followup_scan` | `30 9 * * 1-5` | sales_agent | 工作日 09:30 找超 3 天未回复报价，生成跟进话术 |
| `quality.daily_summary` | `0 18 * * 1-6` | quality_agent | 工作日 18:00 汇总当日质量异常 |

### 2.3 改 cron / 加任务

改 `hermes/scheduler/jobs.py` 的 `JOBS`，然后调 `POST /hermes/scheduler/reload`（免重启）。
新增任务时**必须**同时在 `JOBS` 里给 `task` 字段一个唯一任务名，`register_scheduler_workers`
会自动把它接到 `runtime.execute`。

cron 语法：`分 时 日 月 周`，支持 `*` `*/n` `a-b` `a-b/n` `a,b`；周 0 或 7 = 周日。

### 2.4 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `HERMES_SCHEDULER_ENABLED` | `1` | 进程内调度线程开关 |
| `HERMES_SCHEDULER_TICK` | `30` | 轮询间隔（秒），最小 5 |

### 2.5 多实例部署注意

去重状态在内存（`_fired`）。多实例同时开调度器会对同一分钟重复投递，
生产多副本时**只让一个实例开** `HERMES_SCHEDULER_ENABLED=1`，或改用系统 crontab
调 `POST /hermes/scheduler/jobs/{name}/run`。

---

## 三、审计与回放

### 3.1 收口点只有一个

`ToolCenter.execute()` 是所有工具调用的唯一收口（agent / API / MCP / 定时任务四条路径都经过），
审计在这里落一次账，不会重复也不会漏。

### 3.2 表结构（`tool_call_audits`）

`company_id / agent_name / tool_name / actor / source / session_id / params_json / params_hash /
ok / error / latency_ms / result_excerpt / ip / created_at`

- `params_json` 已脱敏：`token / password / secret / authorization / api_key ...` → `***`
- `params_hash` 是脱敏后参数的 sha256，用于排查"同参数重复调用"
- 同时镜像一条 `audit_logs`（`action="tool.call"`），让既有的安全审计页也能看到 AI 员工动作

### 3.3 客户问"AI 为什么改了这个报价"时怎么答

```bash
# 1) 按会话回放（正序时间线，含每一步参数与结果摘要）
GET /hermes/audit/replay/{session_id}?company_id=<id>

# 2) 按条件捞（某员工、某工具、失败、时间窗）
GET /hermes/audit/tool-calls?company_id=<id>&agent_name=quotation_agent&ok=false&since=1757000000

# 3) 给老板看的三个数
GET /hermes/audit/stats?company_id=<id>&days=7     # 调用量 / 失败率 / 平均耗时 / Top 工具
```

### 3.4 数据库迁移

`tool_call_audits` 已加入 `models.py`，并有 alembic 迁移
`alembic/versions/b7d41c9a2e13_tool_call_audits.py`（幂等：表已存在则跳过，
因为应用启动的 `create_all` 可能已建表）。SQLite 与 Postgres 都适用。

---

## 四、验收测试

```bash
cd backend
./.venv/Scripts/python.exe tests/test_hermes_mcp_scheduler_audit.py
```

覆盖 64 项：cron 真值表、投递去重、MCP 协议与只读拦截、API Key 鉴权与停用、
调度状态与手动触发、审计脱敏、会话回放、跨租户隔离。

---

## 五、待办（本次未做，按需排期）

1. **管理端页面**：调度任务列表（下次运行时间 + 立即执行按钮）、审计台账查询页。
   后端接口已就绪，前端直接调即可。
2. **审计保留策略**：`tool_call_audits` 会持续增长，建议按企业配置保留期
   （如 180 天）或按月归档，需产品确认后再做。
3. **Worker 常驻**：定时任务的执行依赖 Worker。生产需确保
   `python scripts/hermes_worker.py` 由 systemd/supervisor 常驻。
