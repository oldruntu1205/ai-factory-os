# P1.5 简报（喂给 L1 · Codex gpt-6.1-sol）

你是架构与验收的**约束层（L1）**。产出**契约扩展 + 可执行验收断言**，不写业务实现。

## 0. 上游约束

唯一上位文档：`C:\Users\Administrator\Desktop\AI_Factory_V3.0_系统架构.md`（v3.0.0）。
本次处理上游 **§11（同步、异步与事件一致性）+ §12（生命周期、检查点与恢复）+ §13（存储与所有权）**
里**不依赖外部系统**的那部分；这些是 §16 中 **P2 数据孤岛闭环**与 **P3** 的前置。

明确不做：ERP/PMC/MES/WMS 真实字段映射与真实 adapter（那是 P2 的外部依赖，**没有输入就不做**，
不得编造字段名）。本阶段只做"事件与持久化语义"，用 Mock 验证。

## 1. 已完成的前置（不要重做、不要修改）

- **P0**（已交付，门禁 exit 0）：`packages/contracts/` 的 11 个 P0 schema + `openapi/p0.openapi.json`；
  `services/` 八个真实进程；`tests/p0_runtime` 19 项；`tests/contracts/test_p0_contracts.py` 18 项。
  **P0 契约文件已被哈希锁定，你不得修改。**
- **P1**（已交付，门禁 exit 0）：`openapi/p1.openapi.json` + 6 个 `p1-*.json`；`tests/p1_runtime` 26 项；
  `tests/contracts/test_p1_contracts.py` 188 项。**P1 契约文件同样已被锁定，你不得修改。**
- 权威 P0 契约 `event-envelope.json` 已存在，含必填字段与 `x-delivery` 声明；上游 §11 的
  14 类首期事件目录已登记在 P0 门禁的期望表里。
- 门禁命令：`backend\.venv\Scripts\python.exe scripts\gate.py --stages p0,p1`（当前 exit 0）。

## 2. 已实测、须在本阶段修掉的真实缺陷（L1 只需给出验收断言，L2 修实现）

① **Task/Run 状态是进程内内存**，跨进程不可见。实测后果：P1 的
`GET /api/v1/tasks/{task_id}/knowledge-answer` 依赖任务存在性，而 `manufacturing-core`
是另一个进程，读不到 control-plane 的 Task —— 目前靠"任务投影另写一份 journal"绕过，
**两个真相源并存**。上游 §12 要求 Task 状态可查询、Command 终态由 Transaction 依据权威回执决定，
不能有两个真相源。

② **事件层完全未实现**：`outbox`/`inbox`、按 tenant+aggregate 版本收敛、迟到不覆盖、
DLQ 与重放去重，P0/P1 都没有。而 P2 的交付物明确包含 outbox/inbox 与对账。

## 3. 你要产出的东西（只允许写这 4 类路径）

1. `packages/contracts/openapi/p15.openapi.json`：OpenAPI 3.1.0，**只含本次新增**接口。
   约束：
   - **不得**新增接口分层；只能挂在既有 `/api/v1/`、`/internal/v1/` 前缀下。
   - 复用 P0/P1 的认证受众、统一 `Error`、公共 Context（**沿用 P1 的做法：Context 嵌在
     `context` 字段下**，不要再用"平铺 + 嵌套并存"的混合形态）。
   - 明确区分"命令"（请求执行动作，`202` 受理）与"事件"（已发生事实，只能由权威 adapter 在取得凭证后发布）。
2. `packages/contracts/schemas/p15-*.json`：**新增**文件（不得修改已有文件），至少覆盖：
   - `p15-outbox.json`：发件箱记录（生产者：状态与 outbox **同事务**；投递成功后才标记已发）
   - `p15-inbox.json`：收件箱/投影去重（消费者：投影与 inbox 去重**同事务**；按 tenant+aggregate 版本收敛）
   - `p15-dlq.json`：死信（保留错误原因、次数、人工处理状态；重放仍走原去重规则）
   - `p15-receipt.json`：权威执行回执（**只有回执能驱动业务成功**；`accepted`/`UNKNOWN` 绝不能显示已过账）
3. `tests/contracts/test_p15_contracts.py`：静态门禁，只读 `packages/contracts/`，必须能在**没有实现**时跑绿。至少断言：
   - **P0 与 P1 契约文件未被改动**（sha256 常量锁定；哈希值你自己算出来写进去）。
   - `$ref` 不得与语义关键字同级（这是 P1 踩过的真实坑；已有一个通用检查在
     `tests/contracts/test_contract_meta_validation.py`，你只需保证新契约不触发它）。
   - 事件 14 类目录与 P0 期望表逐项一致；事件 Envelope 必填字段与 P0 契约一致（**不得**在 P1.5 重新定义一份）。
   - 幂等语义：outbox 同一事件不得产生两条投递记录；inbox 同 `event_id` 去重；版本按 tenant+aggregate 单调。
   - `UNKNOWN` 不得被任何状态字段表述为成功；`accepted` 不得被表述为已过账（与 P0 的 A14 断言对齐）。
   - 迟到事件（`aggregate_version` 小于已应用版本）不得覆盖新事实；缺版本进入补拉队列而非直接应用。
4. `docs/v3-upgrade/03-阶段-P1.5-持久化与事件一致性.md`：SPEC 正文。必须包含：
   - 新增接口表（方法/路径/行为/owner 运行单元）
   - outbox/inbox 的**事务边界**表（谁与谁同事务、谁不能与谁同事务）
   - 事件一致性规则表（去重、排序、迟到、缺版本、DLQ、重放）
   - 「L2 实现约束清单」（必须 / 建议基线 / 待冻结）
   - 「L3 运行验收断言表」（故障注入方式 + 可观察断言）
   - **P1 缺陷①的收敛方案**：Task/Run 状态如何做到单一真相源，且不违反
     §13「禁止跨 owner 直接写表/join/外键」。方案要说明"谁拥有哪份状态、另一个 owner 通过什么读"。

## 4. 硬约束（违反即失败）

- **不得**修改 `packages/contracts/openapi/p0.openapi.json`、`p1.openapi.json` 与任何
  已有 schema 文件（用哈希锁定并在测试里断言）。
- **不得**新增接口分层（L0–L5 之外没有 L6）；不得自创上游没有的接口路径语义。
- **不得**编造 ERP/PMC/MES/WMS 的真实字段名、表名或产品版本；未冻结项写「待冻结」。
- 失败必须 fail-closed 或进入 DLQ/人工，**不得**变成"静默丢弃"或"当作成功"。
- 消息语义：**至少一次投递，不承诺全局 exactly-once**；跨库不共享事务；
  同一 tenant+aggregate 按版本排序；DLQ 重放仍走原去重规则。
- 敏感内容不得进入事件正文：正文只允许 `payload_ref`/`payload_hash` 这类受控引用（沿用 P0 做法）。
- 示例值只用 `tenant-demo / factory-demo / user-demo / employee-planning / task-demo / trace-demo`；
  不得出现真实域名、账号、密钥、内网 IP。时间一律 UTC ISO8601（Z 结尾）。
- 你**只写文件**：不执行 shell、不装依赖、不联网、不运行测试。写完即止。

## 5. L3 运行验收断言表（写进 SPEC，但由 L2 实现、L3 执行）

SPEC 里必须给出下表（可观察断言）：

| 场景 | 可观察断言 |
| --- | --- |
| 生产者崩溃 | 状态与 outbox 要么都提交、要么都没有；不存在"状态已改但事件丢失" |
| 重复投递 | 同 `event_id` 重复到达：投影只应用一次，inbox 有去重记录，不产生重复副作用 |
| 乱序到达 | 迟到事件不覆盖新事实；缺版本进补拉队列，不直接应用 |
| 投递失败 | 进入 DLQ 并保留原因与次数；重放后仍走原去重规则 |
| 回执缺失 | 命令保持 `UNKNOWN`；查询源系统确认未执行才用**原幂等键**重试；绝不换键 |
| accepted 语义 | `accepted` / `UNKNOWN` 绝不显示"已过账"；只有权威回执驱动成功 |
| 单一真相源 | Task/Run 状态在跨进程读写下一致；不存在两个互相矛盾的真相源 |

## 6. 输出要求

完成后用**不超过 30 行**汇报：写了哪些文件、每个文件作用、你留下的「待冻结」清单、
以及你认为 L2 最容易做错的 3 个点。不要粘贴文件全文。
