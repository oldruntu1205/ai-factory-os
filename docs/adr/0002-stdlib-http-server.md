# ADR-0002 · 中心服务用 Python 标准库 `http.server`，不引 Web 框架

- 状态：**已接受**
- 日期：2026-10-05
- 上游依据：§15「建议 Web 用 TypeScript，**中心 API 与 Edge 优先 Python**」；
  §10 接口分层 L0–L5

## 背景

上游对中心 API 的语言建议是 **Python 优先**，但没有指定框架。
本项目要交付的核心是**契约与语义**：路由归属、身份/上下文、数据分级、
幂等、租约、回执驱动的状态、错误码可定位。

## 决策

8 个单元共用一个自写的 dispatch（`services/_kernel/http.py`），
基于 Python 标准库 `ThreadingHTTPServer`，路由表由 `routes.py` 声明。

关键实现点：

- **路由先于认证**：不属于本服务的路径一律 404 且**在认证之前**返回——
  否则"路径是否存在"会变成一条侧信道。
- **分级兜底**：dispatch 层统一把"缺**请求字段**"的 `KeyError` 转成 400
  `REQUEST_INVALID`，而不是 503。判据是「这个名字确实是客户端该提供的字段」
  （`_is_request_field` 对着 openapi 契约问），而不是吞掉所有 `KeyError`。
- **证据与日志**：健康与指标端点（`/health/live`、`/health/ready`、
  `/internal/v1/operations/metrics`）由同一层提供。

## 后果

**好处**

- 8 个单元共享同一份路由与校验逻辑，**契约漂移无处可藏**。
- 零框架依赖 ⇒ 门禁能在任何有 Python 的机器上跑真实多进程 HTTP 测试。
- 契约校验可以直接对着 `packages/contracts/openapi/*.json` 做
  （包括跨 spec 的 `$ref`、外部片段 `../schemas/x.json#/$defs/Y`）。

**代价（如实记录）**

- 请求体校验、序列化、并发都要自己写。已经因此踩过并修好的坑：
  - 缺字段抛 `KeyError` ⇒ 503（33 个 POST 端点一次性修好）；
  - `_declared_body_fields` 因跨 spec 合并 `components` 而返回 0 个属性；
  - 真实 id 路径不匹配模板 ⇒ 需要 `_literal_positions` + `_normalize` 去问契约。
- 性能与生产级特性（连接池、优雅停机、TLS）**不在当前范围**——
  生产形态见 ADR-0003 与部署清单。

## 备选方案与不选的理由

| 方案 | 为什么不选 |
| --- | --- |
| FastAPI / Flask | 会引入框架层的请求校验语义，与"契约即判据"产生**第二套真相**；且 8 个单元各写一套 app 容易分叉。 |
| 直接复用 V2.1 的 FastAPI 单体 | 那正是要拆掉的形态；沿用会把单体边界带进 V3。 |
