# 制造业 AI 员工平台 V1.0/V2.1

面向制造业的 AI 员工 SaaS 平台。基于 **Hermes Agent Runtime（AI 员工操作系统内核）** + Fireworks AI 模型推理层 + 企业多租户 + RAG 知识库 + Multi-Agent + 工业软件连接层。

## 总体架构（落地实施手册）

```
                    工业企业
                       ↓
           Hermes Agent Runtime
          （AI 员工操作系统内核）
                       ↓
┌──────────┬──────────┬──────────┬──────────┐
↓          ↓          ↓          ↓
客服Agent  销售Agent  报价Agent  生产Agent
↓          ↓          ↓          ↓
CRM       ERP        MES       QMS
                       ↓
                 企业数据资产
                       ↓
              Fireworks AI（模型推理层）
```

Hermes 类似 Salesforce Lightning / AWS Lambda / Kubernetes：**它不是 Agent，而是让任何行业 AI 员工运行的平台**。

### Hermes Runtime 核心模块（backend/hermes/）

| 模块 | 路径 | 能力 |
|---|---|---|
| 运行时编排 | `hermes/runtime.py` | Agent 生命周期、Tool 调度、任务派发 |
| Agent 注册中心 | `hermes/registry/` | 注册/发现/版本/生命周期（动态加载 AI 员工） |
| Tool 调用系统 | `hermes/tool/` | Agent → Tool Router → Permission Check → 企业系统 |
| 三层记忆 | `hermes/memory/` | 短期(Redis) / 工作(PostgreSQL) / 长期(Qdrant) |
| RBAC 权限 | `hermes/permissions/` | 企业→部门→员工→Agent→Tool 权限链 |
| 消息队列 | `hermes/queue/` | RabbitMQ 企业版（交换机/死信/优先级）/ Redis / 内存三后端 |

### RabbitMQ 企业版队列（第7章）

| 能力 | 实现 |
|---|---|
| 交换机路由 | direct（任务 `hermes.tasks`）+ topic（事件 `hermes.events`） |
| 死信队列 | 任务失败 2 次后转入 `hermes.dead`（DLX） |
| 优先级队列 | x-max-priority=10，高优任务先消费 |
| 持久化 | 消息 delivery_mode=2 + 队列 durable，重启不丢 |
| 消费确认 | ack/nack + prefetch 限流 |
| 三后端自适应 | `QUEUE_MODE=auto`：RabbitMQ → Redis → 内存 自动降级 |

```bash
# 本地无 RabbitMQ 时自动降级 Redis/内存；生产 docker-compose 自动起 rabbitmq
# 管理控制台: http://localhost:15672 (hermes/hermes123)
```

### Agent 行业商城（第24章）

| API | 说明 |
|---|---|
| `GET /marketplace/packages` | 浏览商城（按行业/关键词，含已安装标记） |
| `GET /marketplace/packages/{id}` | 包详情（AI 员工清单 + 授权工具） |
| `POST /marketplace/install` | 一键安装：创建企业 AI 员工 + 注册 Hermes Agent + 授权 Tool |
| `POST /marketplace/uninstall` | 卸载并停用包内 AI 员工 |
| `GET /marketplace/installed` | 企业已安装列表 |

预置 4 个行业包：实验室玻璃仪器·AI销售包 / AI报价包 / AI质量包 / 企业大脑·CEO经营包。

## 已实现模块（对应开发文档全 6 阶段）

| 阶段 | 模块 | 后端路径 |
|---|---|---|
| 二 | 用户注册/登录/JWT、企业多租户、AI员工管理、文件上传、文档解析 | `auth/` `api/agent.py` `knowledge/upload.py` |
| 三 | PDF/Word/Excel 解析、切片、Embedding、Qdrant、RAG 检索 | `knowledge/` `api/knowledge.py` |
| 四 | LangGraph 意图路由、客服/销售/报价 Agent、CRM 线索 | `agents/` `ai/llm.py` `api/agent_chat.py` |
| 五 | WhatsApp Webhook、AI 开发信、AI 报价+PDF、SaaS 套餐、**企业微信 + 邮件自动化** | `channels/` `quote_pdf.py` `api/quotation.py` |
| V2.0 | ERP/MES/IoT 连接层、生产/质量/CEO Agent、驾驶舱 | `connectors/` `agents/` `api/dashboard.py` |
| V2.1 | AI 客户发现、客户评分、邮件跟进、Celery 调度 | `sales/` `api/leads.py` `tasks/` |
| Hermes | Agent Runtime 内核（注册中心/Tool/记忆/权限/队列） | `hermes/` `api/hermes.py` |
| Hermes+ | RabbitMQ 企业版队列 + Agent 行业商城 | `hermes/queue/rabbitmq.py` `api/marketplace.py` |

## 快速启动（本地开发，SQLite 降级）

```bash
cd backend
pip install -r requirements.txt
python main.py
```

- API 文档: http://localhost:8000/docs
- 演示账号: `admin@demo.com` / `123456`
- Hermes API: `/hermes/agent/list` `/hermes/execute` `/hermes/tool/list` `/hermes/task/enqueue`

## 生产启动（Docker Compose）

```bash
cp backend/.env.example .env   # 填入 FIREWORKS_API_KEY 或 LLM_API_KEY
docker compose up -d --build
```

- 前端: http://localhost:3000
- 后端 API: http://localhost:8000/docs
- RabbitMQ 管理台: http://localhost:15672（hermes/hermes123）
- hermes-worker: Celery 异步任务消费者（自动启动）

## 配置 LLM（默认 DeepSeek）

```ini
LLM_GATEWAY=deepseek          # 可选: deepseek(默认)/fireworks/openai/zhipu/dashscope/ollama
LLM_API_KEY=sk-xxxx            # platform.deepseek.com 获取
LLM_MODEL=deepseek-chat        # deepseek-chat（V3 通用）/ deepseek-reasoner（R1 深度推理）
```

未配置 Key 时自动使用 MockLLM 演示模式，全链路可运行；填入 Key 并重启后即为真实 AI 员工。

## API 快速体验（Hermes）

```bash
# 1. 查看已注册的 AI 员工 Agent
curl http://localhost:8000/hermes/agent/list -H "Authorization: Bearer <token>"

# 2. 通过 Hermes Runtime 执行 Agent
curl -X POST http://localhost:8000/hermes/execute \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent_name":"sales_agent","message":"采购1000个烧杯","company_id":1}'

# 3. Agent 调用 Tool（权限自动校验）
curl -X POST http://localhost:8000/hermes/tool/call \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"session_id":"<session_id>","tool_name":"crm.get_customer","params":{}}'

# 4. 注册新 Agent（动态加载）
curl -X POST http://localhost:8000/hermes/agent/register \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name":"procurement_agent","industry":"glassware","tools":["erp.get_stock"]}'
```

## 目录结构

```
backend/                  FastAPI 后端
├── hermes/               Hermes Agent Runtime（核心）
│   ├── runtime.py        运行时编排
│   ├── registry/         Agent 注册中心
│   ├── tool/             Tool 调用系统
│   ├── memory/           三层记忆
│   ├── permissions/      RBAC 权限
│   └── queue/            消息队列
├── auth/                 认证（bcrypt + JWT）
├── api/                  API 路由（agent/knowledge/crm/hermes...）
├── agents/               Multi-Agent（LangGraph）
├── ai/                   LLM Gateway（Fireworks 模型路由）
├── knowledge/            RAG（Qdrant）
├── channels/             外部渠道（wecom 企业微信 / whatsapp / email 自动化）
├── connectors/           工业连接层（erp/mes/iot）
├── sales/                AI 销售（scoring/discovery）
├── tasks/                Celery 任务
└── quote_pdf.py          报价单 PDF
frontend/                 React + Vite 管理界面（含 Hermes Runtime / 渠道中心页）
docker-compose.yml        基础设施 + backend + hermes-worker
```
