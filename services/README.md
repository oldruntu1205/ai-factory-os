# 制造业 AI 员工平台 · V3.0 运行单元（P0 骨架，Mock 环境）

本目录是 V3.0 八个运行单元的 **P0 骨架实现**，依据：

- 上游架构文档：`C:\Users\Administrator\Desktop\AI_Factory_V3.0_系统架构.md`（v3.0.0）
- 契约包：`packages/contracts/`（由 L1 产出，**本骨架只读，不修改**）
- 阶段 SPEC：`docs/v3-upgrade/01-阶段-P0-契约与骨架.md`

## 当前状态：P0 / 环境 `mock`

骨架实现的是**契约形状、准入拒绝语义与 Mock 闭环**，不含生产业务逻辑。
所有未冻结项按上游 §16.1 实现为显式配置 + Mock，不填编造的生产值。

## 目录结构

```
services/
├── _kernel/                    共享内核（L2 实现，纯标准库）
│   ├── registry.py             可信登记表加载 + fail-closed 准入判定
│   ├── context.py              公共 Context 校验与提取
│   ├── errors.py               统一错误体（code/message/trace_id/retryable/details）
│   ├── evidence.py             轨迹证据追加（Mock 落 JSONL）
│   ├── http.py                 最小 HTTP 服务（stdlib，路由 + 准入 + 统一错误）
│   ├── routes.py               契约 §10.1 的 15 条业务接口实现
│   └── service.py              通用服务装配（读 manifest + 注册 owner 路由）
├── security-gateway/           自述文件 + __main__.py
├── control-plane/
├── decision-service/
├── hermes-runtime/
├── manufacturing-core/
├── tool-gateway/
├── transaction-service/
└── trajectory-eval/
```

## 启动单个服务

```powershell
# 依赖登记表就绪（否则 fail-closed，一切业务请求 403）
$env:V3_TEST_ENV_FILE = "services\_kernel\registry.mock.json"

# 启动（端口见 registry.mock.json 的 runtime_deployment.port）
cd services\control-plane
backend\..\..\backend\.venv\Scripts\python.exe -m services.control_plane
```

每个服务只注册**自己 owner** 的接口；请求打到不属于本服务的路径返回 404。

## 健康检查语义（不是固定 true）

- `GET /health/live` → 进程存活即 200。
- `GET /health/ready` → 逐项核实声明依赖与策略版本；任一不就绪则 **503**，且 `ready=false`。
  依赖探测方式：登记的 `probe` 为 `file:<path>`、`tcp:<host>:<port>` 或 `always`。

## P0 有意未做（不属本阶段）

- 真实数据库、每 owner schema/role、迁移 DDL（仅建了 owner 迁移目录占位）。
- 独立 UID/身份源、TLS、真实认证受众（Bearer 目前只校验存在性与登记身份匹配）。
- 事件 outbox/inbox、broker、Saga 补偿、对账 worker。
- Edge-RLCD 全部内容（`edge/v1` 仅按契约返回显式未就绪，不做推理）。
- 生产 SLO、RPO/RTO、密钥管理。

## 验证

```powershell
# 契约静态门禁（L1 产出，必须绿）
backend\.venv\Scripts\python.exe -m pytest tests\contracts -q

# P0 运行验收（本骨架，必须绿）
backend\.venv\Scripts\python.exe -m pytest tests\p0_runtime -q
```
