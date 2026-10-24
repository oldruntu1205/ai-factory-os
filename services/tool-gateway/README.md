# tool-gateway

| 项 | 值 |
| --- | --- |
| 运行单元 | `tool-gateway` |
| 逻辑模块 | 工具注册执行、Schema 校验、权限执行点、adapter 调用 |
| schema owner | `tools` |
| 迁移目录 | `migrations/tools/` |
| 契约路由 | POST /internal/v1/tool-requests；POST /internal/v1/tool-executions |
| 端口（Mock） | 8106 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/tools/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
