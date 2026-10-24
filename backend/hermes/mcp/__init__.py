"""
hermes/mcp —— MCP Gateway（Hermes 工具的标准化出口）

对外接口：
    from hermes.mcp.gateway import get_gateway, McpGateway, McpError

用途：
- 让外部 Agent（任何支持 MCP 的客户端）以标准协议列出并调用本平台的工具
- 同时作为内部集成口：接 ERP/MES 时不必为每家写一套适配层

边界：
- 不绕过 RBAC，不直连数据库，默认只读（写工具需显式放行）
- 所有调用都写 hermes/audit 台账，可回放
"""
from hermes.mcp.gateway import McpGateway, McpError, get_gateway

__all__ = ["McpGateway", "McpError", "get_gateway"]
