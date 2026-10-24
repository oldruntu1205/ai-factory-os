"""
hermes/mcp/gateway.py —— MCP Gateway（把 Hermes 工具标准化输出给外部 Agent）

定位：MCP 是"出口"，不是新的执行路径。
    MCP 客户端 ──▶ Gateway ──▶ RBAC 校验 ──▶ ToolCenter.execute ──▶ 企业系统
                                        └─▶ 审计台账（tool_call_audits）

三条硬约束：
1. 不绕过 RBAC：所有调用都带 company_id + agent_name 走原有权限系统，MCP 不是后门。
2. 默认只读：写工具（crm.create_customer / email.send / quotation.create）需显式放行。
3. 不直接碰数据库：Gateway 只经 ToolCenter 访问业务系统，保持单一入口。

协议版本：MCP 2025-06-18（stdio 与 Streamable HTTP 两种传输由部署决定，本实现提供 HTTP）。
"""
import json
import logging
from typing import Optional

from hermes.mcp.schemas import schema_for, is_write_tool

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "hermes-mcp"
SERVER_VERSION = "1.0.0"

# JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class McpError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class McpGateway:
    """MCP 网关：tools/list + tools/call，含权限前置校验与审计。"""

    def __init__(self, runtime=None):
        self._runtime = runtime

    # ---------- 依赖 ----------

    @property
    def runtime(self):
        if self._runtime is None:
            from hermes.runtime import get_runtime
            self._runtime = get_runtime()
        return self._runtime

    # ---------- tools/list ----------

    def list_tools(self, company_id: int, agent_name: Optional[str] = None,
                   allow_write: bool = False) -> list:
        """返回该租户（该 Agent）可用的 MCP 工具清单。

        过滤三层：ToolCenter 注册表 → Agent manifest 声明 → RBAC 权限点。
        """
        center = self.runtime.tool_center
        perm_sys = self.runtime.permission_system
        raw = {t["name"]: t for t in center.list_tools(company_id)}

        # Agent 声明的工具集（未指定 agent 时不过滤）
        declared = None
        if agent_name:
            try:
                manifest = self.runtime.agent_registry.resolve_agent(agent_name, company_id)
                declared = set(manifest.tools or []) if manifest else set()
            except Exception:
                declared = set()

        out = []
        for name, meta in raw.items():
            if declared is not None and name not in declared:
                continue
            if perm_sys is not None and agent_name:
                try:
                    if not perm_sys.check(company_id, agent_name, name):
                        continue
                except Exception:
                    pass
            write = is_write_tool(name, meta.get("permissions"))
            if write and not allow_write:
                continue
            out.append({
                "name": name,
                "description": meta.get("description") or "",
                "inputSchema": schema_for(name),
                "annotations": {
                    "readOnlyHint": not write,
                    "hermes/permissions": meta.get("permissions") or [],
                },
            })
        return out

    # ---------- tools/call ----------

    def call_tool(self, company_id: int, tool_name: str, arguments: dict,
                  agent_name: Optional[str] = None, actor: str = "mcp",
                  session_id: Optional[str] = None, ip: str = "",
                  allow_write: bool = False, source: str = "mcp") -> dict:
        """调用工具，返回 MCP 规范的结果结构 {content, isError}。"""
        center = self.runtime.tool_center
        tool = center.get_tool(tool_name)
        if tool is None:
            raise McpError(INVALID_PARAMS, f"Tool [{tool_name}] 不存在")

        if is_write_tool(tool_name, tool.permissions) and not allow_write:
            raise McpError(
                INVALID_PARAMS,
                f"Tool [{tool_name}] 属于写操作，当前连接为只读模式；"
                f"如需启用请联系管理员开启 MCP 写权限")

        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            result = center.execute(
                tool_name, arguments, company_id,
                agent_name=agent_name,
                actor=actor, source=source,
                session_id=session_id, ip=ip,
            )
        except PermissionError as e:
            # 权限拒绝也进审计（ToolCenter 内部已记录执行失败的调用；此处为前置拒绝）
            from hermes.audit.service import record_tool_call
            record_tool_call(company_id, tool_name, arguments,
                             {"ok": False, "error": str(e)}, 0.0,
                             agent_name=agent_name or "", actor=actor,
                             source=source, session_id=session_id, ip=ip)
            raise McpError(INVALID_PARAMS, str(e))

        ok = bool(result.get("ok")) if isinstance(result, dict) else True
        return {
            "content": [{
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, default=str)[:20000],
            }],
            "isError": not ok,
            "structuredContent": result if isinstance(result, dict) else {"result": result},
        }

    # ---------- JSON-RPC 分发 ----------

    def handle_rpc(self, payload: dict, ctx: dict) -> Optional[dict]:
        """处理一条 JSON-RPC 消息；通知类消息返回 None（无需响应）。"""
        if not isinstance(payload, dict):
            return _error_response(None, INVALID_REQUEST, "请求体必须是 JSON 对象")

        rpc_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if not method:
            return _error_response(rpc_id, INVALID_REQUEST, "缺少 method")

        try:
            result = self._dispatch(method, params, ctx)
        except McpError as e:
            return _error_response(rpc_id, e.code, e.message, e.data)
        except PermissionError as e:
            return _error_response(rpc_id, INVALID_PARAMS, str(e))
        except Exception as e:
            logger.exception("MCP method [%s] 执行异常", method)
            return _error_response(rpc_id, INTERNAL_ERROR, f"内部错误: {e}")

        # 通知（id 缺省）不需要响应
        if rpc_id is None and method.startswith("notifications/"):
            return None
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def _dispatch(self, method: str, params: dict, ctx: dict):
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Hermes AI 员工平台的工具网关。"
                    "tools/list 返回当前企业已授权的工具；"
                    "tools/call 调用工具，全程走企业 RBAC 与审计台账。"
                ),
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.list_tools(
                ctx["company_id"], ctx.get("agent_name"),
                allow_write=bool(ctx.get("allow_write")))}
        if method == "tools/call":
            name = params.get("name")
            if not name:
                raise McpError(INVALID_PARAMS, "tools/call 缺少 name")
            return self.call_tool(
                ctx["company_id"], name, params.get("arguments") or {},
                agent_name=ctx.get("agent_name"), actor=ctx.get("actor", "mcp"),
                session_id=ctx.get("session_id"), ip=ctx.get("ip", ""),
                allow_write=bool(ctx.get("allow_write")),
                source=ctx.get("source", "mcp"))
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise McpError(METHOD_NOT_FOUND, f"不支持的方法: {method}")


def _error_response(rpc_id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


# 全局单例（与 runtime 同生命周期）
_gateway: Optional[McpGateway] = None


def get_gateway() -> McpGateway:
    global _gateway
    if _gateway is None:
        _gateway = McpGateway()
    return _gateway
