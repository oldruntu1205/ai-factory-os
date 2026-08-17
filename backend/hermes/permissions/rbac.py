"""
hermes/permissions/rbac.py —— Hermes 权限系统（第8章）

模型：企业 → 部门 → 员工 → Agent → Tool

RBAC 核心：
- 每个 Agent 声明所需 tools
- 每个 Tool 声明所需 permissions（如 crm:read）
- 租户可配置「Agent 角色」决定哪些权限开放
- 默认策略：Agent 只能调用其 manifest 中声明的 tools，Tool 的权限点必须被租户授权

表（数据库层）：users / roles / permissions / agent_permissions / tool_permissions
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass
class RolePolicy:
    """租户内 Agent 角色策略"""
    role: str                       # 如 sales_agent_role
    agent_names: List[str] = field(default_factory=list)
    allowed_permissions: List[str] = field(default_factory=list)


class RBACSystem:
    """
    权限系统（内存版默认策略 + 可持久化到 DB）。

    默认策略：
    - Agent 只能调用 manifest.tools 中列出的 Tool
    - Tool 的 permissions 必须被租户启用
    - 租户可通过 grant_permission / revoke_permission 微调
    """

    def __init__(self):
        # tenant_id -> set(permission)
        self._tenant_permissions: Dict[int, set] = {}
        # tenant_id -> set(agent_name)
        self._tenant_agents: Dict[int, set] = {}
        # 默认开放的企业工具权限（保守最小集）
        self._default_permissions = {
            "crm:read", "knowledge:read", "dashboard:read",
            "crm:write", "email:send", "quotation:write",
            "erp:read", "mes:read",
        }

    # ---------- 租户配置 ----------

    def init_tenant(self, tenant_id: int, agent_names: List[str] = None,
                    permissions: List[str] = None) -> None:
        """初始化租户权限（注册企业时调用）"""
        self._tenant_permissions.setdefault(tenant_id, set(self._default_permissions))
        self._tenant_agents.setdefault(tenant_id, set(agent_names or []))

    def grant_permission(self, tenant_id: int, permission: str) -> None:
        self._tenant_permissions.setdefault(tenant_id, set(self._default_permissions)).add(permission)

    def revoke_permission(self, tenant_id: int, permission: str) -> None:
        self._tenant_permissions.get(tenant_id, set()).discard(permission)

    def allow_agent(self, tenant_id: int, agent_name: str) -> None:
        self._tenant_agents.setdefault(tenant_id, set()).add(agent_name)

    def deny_agent(self, tenant_id: int, agent_name: str) -> None:
        self._tenant_agents.get(tenant_id, set()).discard(agent_name)

    # ---------- 校验 ----------

    def assert_agent_allowed(self, tenant_id: int, agent_name: str) -> None:
        """企业是否有权运行该 Agent"""
        allowed = self._tenant_agents.get(tenant_id, set())
        if not allowed:
            return  # 未配置限制时默认放行（兼容旧版）
        if agent_name not in allowed:
            raise PermissionError(f"企业 {tenant_id} 无权运行 Agent [{agent_name}]")

    def assert_tool_allowed(self, tenant_id: int, agent_name: str, tool_name: str) -> None:
        """Agent 是否被允许调用该 Tool（双重校验：manifest 声明 + 租户权限）"""
        # 1) Agent manifest 声明校验
        from hermes.runtime import get_runtime
        registry = get_runtime().agent_registry
        manifest = registry.get_agent(agent_name)
        if manifest and manifest.tools and tool_name not in manifest.tools:
            raise PermissionError(
                f"Agent [{agent_name}] 未声明 Tool [{tool_name}]，拒绝调用")

        # 2) Tool 权限点校验
        from hermes.tool.tools import BUILTIN_TOOLS
        tool = BUILTIN_TOOLS.get(tool_name)
        if tool and tool.permissions:
            granted = self._tenant_permissions.get(tenant_id, set(self._default_permissions))
            for perm in tool.permissions:
                if perm not in granted:
                    raise PermissionError(
                        f"企业 {tenant_id} 未授权权限点 [{perm}]，Tool [{tool_name}] 被拒绝")

    def check(self, tenant_id: int, agent_name: str, tool_name: str) -> bool:
        """校验（不抛异常版本）"""
        try:
            self.assert_tool_allowed(tenant_id, agent_name, tool_name)
            return True
        except PermissionError:
            return False
