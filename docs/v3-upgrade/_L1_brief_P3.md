# P3 简报（喂给 L1 · Codex gpt-6.1-sol）

你是架构与验收的**约束层（L1）**。产出**契约扩展 + 可执行验收断言**，不写业务实现。

## 0. 上游约束

唯一上位文档：`C:\Users\Administrator\Desktop\AI_Factory_V3.0_系统架构.md`（v3.0.0）。
本次是 §16 的 **P3 受控写入**。完成条件（原文）：
**「timeout/重启/并发批准/补偿失败可收敛；Mock 通过再 sandbox」**。

关联上游条款（务必逐条对齐，不要自行发挥）：
- §2 原则 5：副作用有幂等键、版本校验、状态查询、补偿和人工收敛路径。
- §3：Transaction **不假设外部 API 超时即失败，不宣称跨系统原子事务**；
  Business Control 不把模型自报置信度或自然语言"批准"当作授权。
- §5 关键流程：Hermes → Harness → Tool Gateway 规范化 → Business Control 风险与审批 →
  Transaction 持久化命令 → **执行时再验证权限/批准/版本** → Tool Gateway 执行 →
  adapter → 权威系统回执 → Saga/Task 投影 → 用户反馈。
- §5 审批绑定：批准必须绑定**具体对象、规范化 payload 哈希、策略版本、动作范围及有效期**；
  payload 改变需重新审批；规范化与哈希由**可信服务**完成，不能只信 Agent 提供的值。
- §5 恢复：Transaction 保存每步的**前置状态、命令键、执行凭证、可补偿动作和补偿权限**；
  MES/WMS 实物域与 ERP 账务域的恢复**先核实并恢复实物状态，再执行账务冲销**；
  已发货、已过账等阶段必须分支处理；**补偿是新的业务动作，不是数据库 rollback**。
- §5 UNKNOWN：执行超时 → UNKNOWN → 查询源系统命令/凭证 → 已成功则记录成功 →
  **确认未执行才使用同一幂等键重试** → 无法确认则人工接管。
  **禁止 UNKNOWN 换新键重发或直接补偿**；外部接口无幂等/状态查询能力时，
  由适配器台账、串行对象锁和人工对账兜底，**不开放无人值守的高风险写入**。
- §10.3：ToolSpec 必须含 `idempotency_support` / `status_query` / `compensation`；
  **无这些能力必须明确为 false，不得伪造成功语义**。
- §12：`AUTHORIZED 不是永续许可`，EXECUTING 前必须重新验证批准有效、对象版本和当前权限；
  Checkpoint 恢复**先查既有 command 再继续**，不重跑已成功步骤；worker 用**租约与 fencing token**
  防旧 worker 提交；对象更新用 CAS/expected_version；**取消只能停止未开始步骤**，
  在途或已完成的副作用需确认及适用补偿，不能把 Task 设为 CANCELLED 即假定撤销。
- §12 状态机：`Command: ACCEPTED → AUTHORIZED → EXECUTING → SUCCEEDED/FAILED/UNKNOWN；可进入 MANUAL_REVIEW`；
  `Saga: RUNNING → SUCCEEDED；失败后 COMPENSATING → COMPENSATED 或 MANUAL_REVIEW`。
- §17 断言 A05 / A06 / A08 / A14 与本阶段直接相关。

## 1. 前置（已完成，不要重做、不要修改）

- **P0/P1/P1.5 已交付且门禁 exit 0**：
  `packages/contracts/{openapi/p0,p1,p15.openapi.json}` + 23 个 schema（P0 11 / P1 6 / P1.5 6）；
  `services/` 八个真实进程；`tests/{p0,p1,p15}_runtime`、`tests/p15_events`。
  **这些契约文件已被 sha256 锁定，你不得修改。**
- 已有可复用语义（P1.5）：原子 outbox、inbox 去重与版本收敛、DLQ、权威回执 `p15-receipt.json`
  （`business_success` 与 `posting_status` 分离，`posting_status` 当前恒为 `NOT_CONFIRMED`）。
- **新增契约必须登记进 `tests/contracts/contract_inventory.py` 的 `CONTRACT_INVENTORY`**，
  否则既有门禁会报「未经阶段登记」。你可以修改该文件（追加 `p3` 段），并需为新阶段加 sha256 锁定。
- 门禁命令：`backend\.venv\Scripts\python.exe scripts\gate.py --stages p0,p1,p15`（当前 exit 0）。

## 2. 本次要建的能力（契约层面）

上游明确"不宣称跨系统原子事务"，因此契约必须能表达**收敛**而不是"保证成功"：

1. **批准绑定与 TOCTOU 防护**：批准是一个独立事实（谁、何时、对哪个对象/哈希/版本/动作范围、
   有效期多久），执行前必须重新核验；`AUTHORIZED` 不是永续许可。
2. **命令执行与权威回执**：`EXECUTING` 之前的再验证、执行凭证、fencing token、串行对象锁。
3. **UNKNOWN 收敛**：超时/无回执时的状态与**必须的下一步**（查源系统 → 记录 → 同键重试或人工），
   以及"**禁止换新键重发/禁止直接补偿**"的契约表达。
4. **Saga 与补偿**：多步事务的步骤账（前置状态、命令键、执行凭证、可补偿动作、补偿权限），
   实物域先于账务域的恢复顺序，阶段分支（已发货/已过账），补偿失败进人工队列。
5. **取消语义**：只能停止未开始步骤；在途/已完成需要确认与补偿，不能靠改 Task 状态视为撤销。
6. **租约与 fencing**：防止旧 worker 继续提交。

## 3. 你要产出的东西（只允许写这 4 类路径）

1. `packages/contracts/openapi/p3.openapi.json`：OpenAPI 3.1.0，**只含 P3 新增**接口。
   约束：**不得**新增接口分层（L0–L5 之外没有 L6），只能挂既有 `/api/v1/`、`/internal/v1/`；
   复用既有认证受众、统一 `Error`；Context **沿用 P1/P1.5 的嵌套形态**（`context` 字段）。
2. `packages/contracts/schemas/p3-*.json`：**新增**文件（不得修改已有文件），至少覆盖：
   - 批准事实与执行前复核（含绑定字段、有效期、复核结论）
   - 命令执行凭证与 fencing/租约
   - UNKNOWN 收敛记录（含"候选动作"与"被禁止的动作"）
   - Saga 步骤账与补偿（含实物/账务阶段分支、补偿权限、人工队列）
   - 取消请求与结果（区分已开始/未开始步骤）
3. `tests/contracts/test_p3_contracts.py`：静态门禁，只读 `packages/contracts/`，
   必须能在**没有实现**时跑绿。至少断言：
   - **P0/P1/P1.5 契约文件未被改动**（sha256 常量锁定；哈希你自己算）。
   - `$ref` 不得与语义关键字同级（已有通用检查 `test_contract_meta_validation.py`，保证不触发它）。
   - 状态机与 §12 一致；`UNKNOWN` 不得被任何字段表述为成功；`accepted`/`AUTHORIZED` 不得被表述为已过账。
   - **合同层禁止项可断言**：UNKNOWN 收敛的候选动作里**不得出现** "换新幂等键重发" 或 "直接补偿"。
   - 补偿顺序可断言：实物域步骤必须先于账务域；已过账/已发货阶段的补偿分支必须显式存在。
   - ToolSpec 能力位（`idempotency_support`/`status_query`/`compensation`）为 false 时，
     契约必须能表达"不得开放无人值守高风险写入"。
4. `docs/v3-upgrade/04-阶段-P3-受控写入.md`：SPEC 正文。必须包含：
   - P3 新增接口表（方法/路径/行为/owner 运行单元）
   - 批准绑定字段表与 TOCTOU 复核点
   - UNKNOWN 收敛决策表（含**禁止动作**与人工接管条件）
   - Saga 步骤账与补偿顺序表（实物→账务、阶段分支）
   - 取消语义表
   - 「L2 实现约束清单」（必须 / 建议基线 / 待冻结）
   - 「L3 运行验收断言表」（故障注入方式 + 可观察断言）
   - 明确写出**待冻结**：真实外部字段映射、核销口径、审批岗位与限额、sandbox 环境
     （这些属外部依赖，**不得编造**）。

## 4. 硬约束（违反即失败）

- **不得**修改 `packages/contracts/openapi/{p0,p1,p15}.openapi.json` 与任何已有 schema 文件。
- **不得**新增接口分层；不得自创上游没有的状态、错误码语义或角色。
- **不得**在契约里承诺"跨系统原子事务"或"超时即失败"。
- 失败必须 fail-closed / 进 DLQ / 进人工队列，**不得**静默丢弃或当成功。
- 补偿必须是**新的业务动作**（有自己的命令、幂等键、回执），不得建模成"回滚"。
- 敏感内容不得进入契约示例或审计：正文只用受控引用与哈希。
- 示例值只用 `tenant-demo / factory-demo / user-demo / employee-planning / task-demo / trace-demo`；
  不得出现真实域名、账号、密钥、内网 IP。时间一律 UTC ISO8601（Z 结尾）。
- 你**只写文件**：不执行 shell、不装依赖、不联网、不运行测试。写完即止。

## 5. L3 运行验收断言表（写进 SPEC，但由 L2 实现、L3 执行）

| 场景 | 可观察断言 |
| --- | --- |
| 执行超时 | 命令进入 `UNKNOWN`；**不得**出现换新键重发、不得直接补偿；候选动作只有"查源系统/同键重试/人工接管" |
| 同键异 payload | 409；同键同 payload 返回**同一** command 且不重复产生副作用 |
| 并发批准 | 只有一个批准生效；过期/旧版本批准被拒；批准后 payload 变化必须重新批准 |
| TOCTOU | 批准后对象版本或权限变更 → 执行被阻止，且给出明确原因 |
| 重启恢复 | 先查既有 command 再继续；已成功步骤不重跑；旧 worker 的 fencing token 失效 |
| 补偿失败 | 进入人工队列（`MANUAL_REVIEW`），Saga 不谎报 `COMPENSATED` |
| 补偿顺序 | 实物域步骤先于账务域；已过账阶段走分支而不是直接冲销 |
| 取消 | 未开始步骤被停止；在途/已完成不被"取消"抹掉，需确认与补偿 |
| 无幂等/查询能力的工具 | 拒绝无人值守高风险写入，走人工对账兜底 |
| 权威回执 | 只有回执驱动 `SUCCEEDED`；`posting_status` 与业务成功分离 |

## 6. 输出要求

完成后用**不超过 30 行**汇报：写了哪些文件、每个文件作用、你留下的「待冻结」清单、
以及你认为 L2 最容易做错的 3 个点。不要粘贴文件全文。
