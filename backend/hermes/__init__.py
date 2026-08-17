"""
hermes/__init__.py —— Hermes Agent Runtime（AI 员工操作系统内核）

Hermes 不是 Agent，而是 Agent 的运行环境。
类似 Windows 运行应用程序、Kubernetes 调度容器：
  Hermes 运行 AI 员工。

架构（对应落地手册第二篇）：
    Client
      ↓
  API Gateway
      ↓
  Hermes Runtime
  ┌────────┬────────┬────────┬────────┐
  ↓        ↓        ↓        ↓
  Agent   Tool    Memory   Permission
  Registry Center   System   System
  ↓        ↓        ↓        ↓
  Workflow Queue   Event Bus
      ↓
  Business Systems (CRM ERP MES QMS)
      ↓
  Fireworks AI（模型推理层）
"""
