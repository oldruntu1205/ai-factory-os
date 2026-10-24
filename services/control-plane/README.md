# control-plane

| 项 | 值 |
| --- | --- |
| 运行单元 | `control-plane` |
| 逻辑模块 | Employee/Task/Admin、Policy、Approval、Schema/Release Registry |
| schema owner | `control` |
| 迁移目录 | `migrations/control/` |
| 契约路由 | POST /api/v1/tasks；GET /api/v1/tasks/{task_id}；POST /api/v1/approvals/{approval_id}/decisions；POST /internal/v1/policy/evaluate |
| 端口（Mock） | 8102 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/control/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
