# migrations/manufacturing

**owner**: `manufacturing`

P0 阶段此目录只有说明，没有迁移。原因：P0 不落库；每个 owner 的 schema、
独立 DB role 与首个迁移在 P1/P2 落地（依据上游 §13 与 §16 的 P1/P2 交付物）。

硬约束（上游 §13）：
- 只允许本 owner 在此目录写迁移；**不得**修改其他 owner 的迁移目录。
- 已应用的迁移不得修改，只能新增。
- **禁止**跨 owner 直接写表、跨服务 join、跨 owner 外键；跨边界用 ID 与 API/事件引用。
- 所有业务主键、唯一索引与幂等域必须包含 `tenant`。
- 表名前缀建议 `manufacturing`_ ；RLS 作为第二道隔离，应用层身份检查仍为必须。

选型（PostgreSQL）属上游「建议基线」，实际消息产品/向量后端/依赖版本
在 P0 选型 ADR 中记录。
