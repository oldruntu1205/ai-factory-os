# security-gateway

| 项 | 值 |
| --- | --- |
| 运行单元 | `security-gateway` |
| 逻辑模块 | 入口安全、模型治理、DLP、Token Vault、模型路由 |
| schema owner | `security` |
| 迁移目录 | `migrations/security/` |
| 契约路由 | POST /internal/v1/model-invocations |
| 端口（Mock） | 8101 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/security/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
