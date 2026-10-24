# hermes-runtime

| 项 | 值 |
| --- | --- |
| 运行单元 | `hermes-runtime` |
| 逻辑模块 | Run、Memory、Skill、Checkpoint、人工等待 |
| schema owner | `runtime` |
| 迁移目录 | `migrations/runtime/` |
| 契约路由 | POST /internal/v1/runs；POST /internal/v1/runs/{run_id}/resume |
| 端口（Mock） | 8104 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/runtime/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
