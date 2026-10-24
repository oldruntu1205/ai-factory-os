# transaction-service

| 项 | 值 |
| --- | --- |
| 运行单元 | `transaction-service` |
| 逻辑模块 | Command、Idempotency、Saga、重试/补偿、对账 worker |
| schema owner | `transaction` |
| 迁移目录 | `migrations/transaction/` |
| 契约路由 | POST /internal/v1/commands；GET /internal/v1/commands/{command_id} |
| 端口（Mock） | 8107 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/transaction/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
