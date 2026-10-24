# 第三方组件声明

本文件对应 [`LICENSE`](./LICENSE)（XCIUL 1.0）第 5.3 条。

本软件在运行与构建过程中依赖下列第三方开源组件。这些组件**不受 XCIUL 1.0 约束**，
各自适用其原始许可证条款。使用本软件时，你同样需要遵守这些组件各自的许可条件。

本清单列出主要直接依赖，不含传递依赖的完整树。完整依赖树可通过
`pip freeze`（Python）与 `npm ls --all`（Node）导出。

---

## Python（后端 `backend/requirements.txt`）

### Web 框架与数据校验

| 组件 | 用途 | 许可证 |
|---|---|---|
| FastAPI | HTTP API 框架 | MIT |
| uvicorn | ASGI 服务器 | BSD-3-Clause |
| python-multipart | 表单与文件上传解析 | Apache-2.0 |
| pydantic / pydantic-settings | 数据模型与配置 | MIT |
| email-validator | 邮箱格式校验 | CC0-1.0 |

### 数据库与认证

| 组件 | 用途 | 许可证 |
|---|---|---|
| SQLAlchemy | ORM | MIT |
| psycopg2-binary | PostgreSQL 驱动 | LGPL-3.0（binary 例外） |
| alembic | 数据库迁移 | MIT |
| passlib | 密码哈希封装 | BSD-3-Clause |
| bcrypt | 密码哈希算法 | Apache-2.0 |
| python-jose | JWT 签发与校验 | MIT |

### RAG 与 AI

| 组件 | 用途 | 许可证 |
|---|---|---|
| langchain-community / langchain-core | LLM 应用框架 | MIT |
| langchain-text-splitters | 文本切分 | MIT |
| langchain-openai | OpenAI 兼容接口适配 | MIT |
| langgraph | Agent 状态图编排 | MIT |
| fastembed | 本地向量化 | Apache-2.0 |
| qdrant-client | 向量库客户端 | Apache-2.0 |

### 文档解析与办公

| 组件 | 用途 | 许可证 |
|---|---|---|
| pymupdf | PDF 解析 | AGPL-3.0 |
| docx2txt | Word 文本提取 | MIT |
| pandas | 数据处理 | BSD-3-Clause |
| openpyxl | Excel 读写 | MIT |
| ezdxf | CAD DXF 图纸解析 | MIT |
| reportlab | 报价单 PDF 生成 | BSD-3-Clause |

### 任务队列与通信

| 组件 | 用途 | 许可证 |
|---|---|---|
| celery | 异步任务队列 | BSD-3-Clause |
| redis | Redis 客户端 | MIT |
| websockets | WebSocket 通信 | BSD-3-Clause |
| requests | HTTP 客户端 | Apache-2.0 |

---

## Node.js

### 前端（`frontend/package.json`）

| 组件 | 用途 | 许可证 |
|---|---|---|
| React / React DOM | UI 框架 | MIT |
| react-router-dom | 前端路由 | MIT |
| axios | HTTP 客户端 | MIT |
| echarts | 图表可视化 | Apache-2.0 |
| Vite | 构建工具 | MIT |
| @vitejs/plugin-react | Vite React 插件 | MIT |

`apps/web-admin` 为零依赖实现，仅使用 Node.js 内置模块（MIT）。

---

## 特别提示

**PyMuPDF（AGPL-3.0）** 的许可证具有传染性。若你计划对第三方提供基于本软件的服务，
须自行评估 AGPL-3.0 项下的义务，包括在特定情形下提供对应源码的要求。
XCIUL 1.0 不减免你因使用第三方组件而承担的任何许可义务。

**psycopg2-binary（LGPL-3.0）** 以动态链接方式使用，通常不触发源码开放义务，
但再分发时须保留其许可证声明。

---

## 模型与外部服务

本软件可对接下列外部服务，其使用受各自服务条款约束，不属于本仓库分发的组件：

- DeepSeek / Fireworks AI / OpenAI / 智谱 / 阿里云百炼（LLM 推理）
- 企业微信、WhatsApp、SMTP 服务商（消息渠道）
- Qdrant、PostgreSQL、Redis、RabbitMQ（基础设施）

相应的 API 密钥、服务费用与合规责任由使用者自行承担。

---

如发现本清单遗漏或被依赖版本变更导致不准确，请以各组件官方许可证原文为准。
