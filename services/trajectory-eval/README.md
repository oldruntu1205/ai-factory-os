# trajectory-eval

| 项 | 值 |
| --- | --- |
| 运行单元 | `trajectory-eval` |
| 逻辑模块 | 轨迹收集、审计归档、Eval worker、发布证据 |
| schema owner | `evidence` |
| 迁移目录 | `migrations/evidence/` |
| 契约路由 | （§10.1 未分配业务接口；提供健康与证据落盘） |
| 端口（Mock） | 8108 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/evidence/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
