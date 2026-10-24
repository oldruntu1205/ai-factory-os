# P1 简报（喂给 L1 · Codex gpt-6.1-sol）

你是架构与验收的**约束层（L1）**。产出**契扩展 + 可执行验收断言**，不写业务实现。
实现由 L2 完成；你只写 SPEC 与测试。

## 0. 上游约束

唯一上位文档：`C:\Users\Administrator\Desktop\AI_Factory_V3.0_系统架构.md`（v3.0.0）。
本次是 §16 的 **P1 安全与只读闭环**。完成条件（原文）：
**「C3/C4 无出域路径；跨租户不可见；只读问答来源可追溯。」**

## 1. 已完成的前置（P0，不要重做、不要修改）

- 契约：`packages/contracts/`（OpenAPI 3.1 `openapi/p0.openapi.json` + 11 个 JSON Schema）。
  **P0 契约文件已被门禁锁定，你不得修改它们。**
- 骨架：`services/`（8 个运行单元为真实进程，`backend\.venv\Scripts\python.exe -m services.<service-id>`）。
  **你不得修改 `services/` 或 `tests/p0_runtime/`。**
- P0 门禁：`backend\.venv\Scripts\python.exe scripts\gate_p0.py` → 18 + 19 + 64 全绿、exit 0。

## 2. 本次开工前提（已由 L2 决策，照此设计）

P1 **不引入新的接口分层**。上游 §10 只定义 L0–L5，没有 L6；
§14.1 的"批准的外部模型域"与内部管理面同属企业 IT 本地数据域内的服务，
因此 P1 新增接口一律挂在既有 `/api/v1/`、`/internal/v1/` 前缀下，
新增契约写在**新文件** `packages/contracts/openapi/p1.openapi.json`（与 p0 并存，不覆盖）。

## 3. 你要产出的东西（只允许写这 4 类路径）

1. `packages/contracts/openapi/p1.openapi.json`：OpenAPI 3.1.0，**只含 P1 新增**接口
   （p0 已有接口不重复定义；但方案里的 `$ref` 可以指向 p0 的 components）。
2. `packages/contracts/schemas/`：**新增** JSON Schema 文件（不得修改已有文件），至少覆盖：
   - `p1-classification.json`：分类/派生上下文重分级结果（§4.1）
   - `p1-egress-audit.json`：出域尝试审计记录（§4.3 最后一步）
   - `p1-token-vault.json`：Tokenization 映射与恢复语义（§4.3）
   - `p1-knowledge-source.json`：只读问答的来源可追溯引用（§6.1）
3. `tests/contracts/test_p1_contracts.py`：纯标准库 + pytest 的**静态门禁**，
   只读 `packages/contracts/`，不导入 `services/`、不访问网络、不连 DB。必须能在**没有实现**时跑绿。
   至少断言：
   - P1 契约文件可解析；p0 契约文件**未被改动**（用 sha256 常量锁定 p0 各文件的哈希，
     这是防止"P1 顺手改了 P0"的唯一可靠手段；哈希值请你自己算出来写进去）。
   - §4.1 的四级分类语义在 schema 中可表达：C0–C4 封闭枚举、未知等级拒绝、
     **合并上下文取最高等级**、**输出继承输入最高等级**、匿名化不自动降级。
   - §4.3 治理九步在 schema/描述中可逐项定位：身份租户校验 → 访问范围 → 输入分类 →
     最小化脱敏 → DLP/出域 → 模型白名单 → 调用 → 输出 Schema/泄露扫描 → 本地 Token 恢复 → 消费 → 分级审计。
   - §6.1 只读闭环的来源可追溯：检索前过滤 tenant/工厂/岗位 ACL、返回时再校验、
     来源必须含 source_system/source_id/source_version/occurred_at/data_class。
   - P1 新增接口必须显式声明 4xx 错误响应并引用统一 `Error`（复用 p0 的 error.json）。
4. `docs/v3-upgrade/02-阶段-P1-安全与只读闭环.md`：P1 SPEC 正文。必须包含：
   - P1 新增接口表（方法与路径 + 行为 + 归属 owner）
   - 数据分级判定规则表（输入分类、派生上下文、输出继承、合并取最高）
   - 出域判定矩阵（data_class × model_backend → 允许/拒绝/排队）
   - 只读问答链路与来源可追溯字段表
   - 「L2 实现约束清单」（必须 / 建议基线 / 待冻结）
   - 「L3 运行验收断言表」（见下）

## 4. 硬约束（违反即失败）

- **不得**新增接口分层、不得自创 L6/L7；**不得**修改 `packages/contracts/openapi/p0.openapi.json`
  与任何已有 schema 文件（用哈希锁定并在测试里断言）。
- **不得**编造上游没有的状态、错误码语义或数据等级。文档没写的写「待冻结」。
- fail-closed 必须能在契约层面表达：未知等级、未知工具、未知 tenant、
  **C3/C4 且非本地后端**、**本地后端故障时禁止自动降级公网** —— 全部是"拒绝或排队"，
  不得设计成默认放行或可选项。
- 敏感内容不得进入日志/审计/证据：审计记录只允许摘要、引用、哈希与版本。
- 示例值只用 `tenant-demo / factory-demo / user-demo / employee-planning / task-demo / trace-demo` 占位；
  不得出现真实域名、账号、密钥、内网 IP。
- 时间是 UTC ISO8601（Z 结尾）；数量金额用 string + pattern。
- 你**只写文件**：不执行 shell、不装依赖、不联网、不运行测试。写完即止。

## 5. L3 运行验收断言表（写进 SPEC，但由 L2 实现、L3 执行）

SPEC 里必须给出下表（可观察断言），L2 照此实现、L3 照此验收：

| 场景 | 可观察断言 |
| --- | --- |
| C3/C4 出域 | 拒绝，且出域审计里没有对外部后端的**成功**调用记录 |
| 未知等级 | 拒绝，不推断为低等级 |
| 混合上下文 | 按最高等级判定，不是按平均值或首个输入 |
| 模型输出泄漏 | 输出扫描拦下未授权原文/令牌 |
| 本地后端故障 | 拒绝或排队，**不得**出现对公网后端的调用 |
| 只读问答 | 能按 tenant/task/trace 串联来源，且来源含版本与时间 |
| 跨租户检索 | 检索前即被过滤，不靠 LLM 判断可见性 |

## 6. 输出要求

完成后用**不超过 30 行**汇报：写了哪些文件、每个文件作用、你留下的「待冻结」清单、
以及你认为 L2 最容易做错的 3 个点。不要粘贴文件全文。
