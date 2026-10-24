# manufacturing-core

| 项 | 值 |
| --- | --- |
| 运行单元 | `manufacturing-core` |
| 逻辑模块 | Harness、只读上下文/知识服务、adapter 模块、事实投影 |
| schema owner | `manufacturing` |
| 迁移目录 | `migrations/manufacturing/` |
| 契约路由 | POST /internal/v1/contexts/query（本地验证另挂 /edge/v1/**） |
| 端口（Mock） | 8105 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/manufacturing/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
