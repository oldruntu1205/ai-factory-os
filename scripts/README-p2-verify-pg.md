# P2 真实执行校验（一次性 PostgreSQL 集群）

## 这个脚本做什么

在**独立端口 + 独立数据目录**的一次性 PostgreSQL 集群上真实执行
`migrations/<owner>/0001_schema.sql`，然后实测 §13 的隔离是否**真的**成立：

| # | 实测项 | 期望 |
| --- | --- | --- |
| 1 | 8 个 owner 的迁移能否无错执行 | 全部 exit 0 |
| 2 | schema / 角色数量 | 8 / 16 |
| 3 | app 角色**未设置** `app.tenant_id` 时读表 | 0 行（fail-closed） |
| 4 | 设置 `app.tenant_id='tenant-demo'` 后 | 只看到本租户 1 行 |
| 5 | **表属主**（FORCE RLS 生效） | 同样受策略约束 |
| 6 | `control_app` 读 `transaction-service` 的表 | 被拒（跨 owner） |
| 7 | app 角色是否带超级权限 | 0 个 |
| 8 | 真库里的跨 schema 外键 | 0 个 |

## 安全边界

- **不碰你现有的 5432 实例**：不读它的配置、不改它的 `pg_hba.conf`、不重启它；
- 一次性集群用 `--auth=trust`，只监听 `127.0.0.1`，端口 `5455`，不对外；
- **无论成败都关停并删除**数据目录（失败时保留日志目录便于排查）。

## 运行方式

```powershell
pwsh -NoProfile -File scripts\p2_verify_pg.ps1
```

需要本机有 PostgreSQL 客户端与 `initdb`（本机：`C:\pg\pgsql\bin`）。

## ⚠️ 已知的环境坑（**请勿用管道包装这个脚本**）

本机实测**三次各挂起 600 秒未返回**，根因是：

> `pg_ctl start` 会派生**常驻的后台 postgres**，子进程继承 stdout/stderr。
> 任何"要等输出流结束"的调用方式都会**永久等待**，因为那个 postgres 不会退出。

踩过的三种写法**全部失败**：

| 写法 | 结果 |
| --- | --- |
| `& pg_ctl ... start \| Select-Object -Last 2` | 挂起 |
| `& pg_ctl ... start 1> file 2>&1` | 挂起 |
| `Start-Process -NoNewWindow -Wait` | 挂起 |

因此当前脚本改用 **`Start-Job` 启动 postgres + 轮询端口判就绪 + 按 PID 显式关停**。

**同时注意**：不要在外层用管道或 `Tee-Object` 包装整个脚本，
也不要在脚本运行期间用 `Stop-Process` 批量杀进程——实测这会连带终止
工具自身的作业运行器（表现为 `exit code 4294967295`）。

若需后台运行，直接重定向到文件后**另开一次调用**读文件，不要 `-Wait` 等它。

## 当前状态（诚实声明）

| 项 | 状态 |
| --- | --- |
| 脚本本身 | 已写好，含 8 项实测断言与收尾清理 |
| **完整跑通** | **尚未**——受上述进程/句柄限制，本轮未能在本环境跑完 |
| DDL 语法正确性 | 已用官方文档核对（`CREATE ROLE` 的 `BYPASSRLS \| NOBYPASSRLS`） |
| §13 不变量 | 已由 `tests/p2_migrations` 在**文本层**验证（35 用例） |

**因此：`docs/v3-upgrade/handoff/P2-交付清单.md` 里"DDL 未在真实 PG 执行过"这条限制，
在脚本成功跑通前**依然成立**。** 跑通后请把输出附到
`docs/v3-upgrade/evidence/` 下，再把该限制划掉。
