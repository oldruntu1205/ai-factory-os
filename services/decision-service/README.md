# decision-service

| 项 | 值 |
| --- | --- |
| 运行单元 | `decision-service` |
| 逻辑模块 | Rules、Jev 接口、分类/排序/门禁决策 |
| schema owner | `decision` |
| 迁移目录 | `migrations/decision/` |
| 契约路由 | POST /internal/v1/decisions |
| 端口（Mock） | 8103 |

**P0 状态**：契约形状 + 准入拒绝 + Mock 闭环。无持久化，重启即空。
每个 owner 的数据库 schema、独立 DB role 与真实迁移在 P1/P2 落地；
`migrations/decision/` 当前只有占位说明，**不得**把其他 owner 的表写进这里。
