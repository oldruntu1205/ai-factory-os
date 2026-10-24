# P0 简报（喂给 L1 · Codex gpt-6.1-sol）

你是架构与验收的**约束层（L1）**。你的产出是**契约 + 可执行验收断言**，不是业务实现代码。
实现由 L2 完成；你只写 SPEC 与测试。

## 0. 上游约束

唯一上位文档：`C:\Users\Administrator\Desktop\AI_Factory_V3.0_系统架构.md`（v3.0.0）。
其规范用语：**必须** = 硬契约；**建议基线** = 可不改边界地 ADR 调整；**待冻结** = 阻塞对应生产写入，不阻塞 P0。

本次任务是该文档 §16 的 **P0 契约与骨架**。
P0 完成条件（原文）：**核心服务可启动；拒绝未知 tenant/工具；Mock 闭环可追踪。**

## 1. 现状基线（已实测，可作为事实使用，不必再去探索仓库）

- 仓库：`H:\制造业 AI 员工平台全量开发`。单体：`backend/`（FastAPI，30k 行，25 个 router，无 `/api/v1` 版本化路径）+ `frontend/`（React+Vite，10k 行）。
- 栈：FastAPI 0.141.1 / SQLAlchemy 2.0.52 / Pydantic 2.13.4 / Python 3.13（`backend/.venv`）。DB：compose 内 postgres:16，Orchestration 另有 Redis / RabbitMQ / Qdrant。
- 数据层：`backend/models.py` 37 张表，全部落在单一 schema，靠 `create_all` 建表；alembic 仅有 1 个 baseline + 1 个增量。多租户靠 `company_id`（38 处），`tenant_id` 仅 62 处且未统一。
- 已有能力（可复用，但接口形状不符合 V3.0）：Hermes runtime（registry / tool center / memory / permissions / queue）、MCP Gateway（`backend/hermes/mcp/`）、工具调用审计（`backend/hermes/audit/`，表 `tool_call_audits`）、定时调度（`backend/hermes/scheduler/`）。
- **不存在**：服务拆分、契约包、C0–C4 数据分级、出域治理、Command/幂等/Saga/对账、审批绑定、事件 outbox/inbox、Trajectory/Eval/Release Registry、Edge。
- 现有测试基线：`backend/tests/test_hermes_mcp_scheduler_audit.py` 独立可跑，**64 PASS / 0 FAIL，exit 0**。

## 2. 开工结构决策（已定，不必再讨论）

V3.0 的 8 个运行单元建在**新目录** `services/`（与既有 `backend/` 并存），契约在 `packages/contracts/`。
理由：既有 `backend/` 在产、不破坏；契约优先要求先立契约再迁实现；迁移按能力对齐（RAG / 渠道 / 行业包→manufacturing-core 与 integrations）增量进行。

## 3. 你要产出的东西（只允许写这 4 类路径）

1. `packages/contracts/openapi/`：**OpenAPI 3.1** 文档，覆盖文档 §10.1 的全部接口，另加一个健康基线。要求：
   - 路径前缀严格遵守 V3.0 的 `/api/v1/...`、`/internal/v1/...`、`/edge/v1/...`，不得自创。
   - 每个接口给出请求/响应 Schema 引用（`$ref` 到 `components/schemas`），不得写"宽松 object"。
   - 统一错误体 `code / message / trace_id / retryable / details`，并在 components 里给出标准错误响应（400/401/403/404/409/422/429/503）。
   - 统一公共 Context 字段：`tenant_id / factory_id / actor_id / employee_id / task_id / trace_id / data_class / schema_version / deadline_at`。
2. `packages/contracts/schemas/`：**JSON Schema（2020-12）** 分文件定义，至少覆盖：
   - `common-context.json`、`error.json`
   - `command.json`（含 §10.2 给出的完整示例字段，逐字段 required 明确）
   - `decision-result.json`（`decision_id / route / action / category / confidence / schema_version / model_version / calibration_version / rule_version / reason_code / evidence_refs / fallback`）
   - `event-envelope.json`（`event_id / event_type / schema_version / tenant_id / factory_id / source_system / aggregate_type / aggregate_id / aggregate_version / occurred_at / received_at / trace_id / causation_id / correlation_id / data_class / payload`）
   - `tool-spec.json`（`tool_id / version / input_schema / output_schema / read_write / risk_class / required_scopes / timeout / retry_policy / idempotency_support / status_query / compensation / data_class_rules / adapter_owner`）
   - `task.json`、`command-status.json`、`service-manifest.json`
   约束：JSON Schema 里**不得**使用 `$ref` 指向外部 URL；跨文件引用用仓库内相对路径；`additionalProperties: false` 用于所有枚举型/契约型对象，避免静默扩展。
3. `tests/contracts/test_p0_contracts.py`：**纯 Python 标准库 + pytest**，只读上面两个目录后做机器验证。**这条用例是 P0 的门禁主判据**，必须能在**没有实现代码**的情况下独立跑绿。至少断言：
   - 契约文件可解析（JSON/YAML 合法）、OpenAPI 版本为 3.1.x。
   - `$ref` 全部可解析到仓库内文件/`components`，无悬空引用，无外网 URL。
   - 文档 §10.1 的每一行接口（方法 + 路径）在 OpenAPI 中都存在（用一张硬编码的期望表逐项断言，期望表从文档原文抄写）。
   - 每个接口都有显式 4xx 错误响应定义，且引用了统一 `error` schema。
   - 所有契约对象都带 `tenant_id` 或显式声明为全局对象（列出白名单，白名单要小）。
   - 枚举值封闭性：`data_class ⊆ {C0,C1,C2,C3,C4}`、`risk_class ⊆ {R0,R1,R2,R3}`、command 状态 ⊆ 文档 §12 的状态机、task/run/command/saga/edge-release 状态机与 §12 表格逐项一致。
   - 命令契约里 `idempotency_key`、`expected_version`、`payload_version` 为 required，且 `payload_hash` 语义（由可信服务计算）在 description 里写明。
4. `docs/v3-upgrade/01-阶段-P0-契约与骨架.md`：P0 的 SPEC 正文（简洁、可审阅），包含：接口目录期望表、状态机表、错误码表、以及「L2 实现约束清单」（哪些是必须、哪些是建议基线、哪些待冻结）。

## 4. 硬约束（违反即失败）

- **必须 fail-closed**：未知 `tenant_id`、未知 `tool_id`、未知 `data_class`、C3/C4 且模型后端非本地 —— 契约层面必须能表达"拒绝"，不得设计成可选字段或默认放行。
- **不得**出现真实密钥、域名、账号、内网 IP。示例值只用 `tenant-demo / factory-demo / user-demo / employee-planning / task-demo / trace-demo` 这类占位。
- **不得**编造上游文档没有的接口路径、状态机状态、错误码语义；文档没写的就留 `待冻结` 并在 SPEC 中单列。
- 时间是 UTC ISO8601；数量金额用十进制定点（JSON Schema 用 `string` + `pattern`，不要用 `number`）。
- 你**只写文件**，不执行任何 shell 命令、不安装依赖、不访问网络。写完即止，不要运行测试（测试由 L3 运行）。

## 5. 输出要求

完成后用不超过 30 行汇报：写了哪些文件、每个文件的作用、你在实现时留下的「待冻结」清单、以及你认为 L2 最容易做错的 3 个点。不要粘贴文件全文。
