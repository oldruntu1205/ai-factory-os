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
    - Agent 白名单语义（安全修复）：未显式授权前默认放行（兼容旧版）；
      首次 allow_agent 时并入平台默认 Agent 名单，避免"全放行"翻转为"仅白名单"
      导致内置 Agent 全部被拒的功能回归。
    """

    # 平台内置 Agent 名单（main.py 启动注册；首次授权时并入白名单，保证安装商城包后
    # 内置 AI 员工仍可用）
    DEFAULT_AGENT_NAMES = {
        "customer_agent", "sales_agent", "quotation_agent",
        "production_agent", "quality_agent", "ceo_agent", "process_agent",
    }

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

    # ---------- 持久化（P2 #4：RBAC 落库，重启不丢）----------

    def _db_load(self, tenant_id: int) -> bool:
        """从 DB 加载租户设置（懒加载；无记录返回 False，走默认策略）"""
        try:
            from database import SessionLocal
            from models import HermesRbacSetting
            db = SessionLocal()
            try:
                row = db.query(HermesRbacSetting).filter(
                    HermesRbacSetting.company_id == tenant_id).first()
                if row:
                    self._tenant_permissions[tenant_id] = set(row.permissions or [])
                    self._tenant_agents[tenant_id] = set(row.allowed_agents or [])
                    return True
            finally:
                db.close()
        except Exception as e:
            logger.debug("RBAC 加载失败 tenant=%s: %s", tenant_id, e)
        return False

    def _db_persist(self, tenant_id: int) -> None:
        """把租户设置写回 DB（仅持久化显式配置过的租户）"""
        try:
            from database import SessionLocal
            from models import HermesRbacSetting
            db = SessionLocal()
            try:
                row = db.query(HermesRbacSetting).filter(
                    HermesRbacSetting.company_id == tenant_id).first()
                if not row:
                    row = HermesRbacSetting(company_id=tenant_id)
                    db.add(row)
                row.permissions = sorted(self._tenant_permissions.get(tenant_id, set()))
                row.allowed_agents = sorted(self._tenant_agents.get(tenant_id, set()))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("RBAC 持久化失败 tenant=%s: %s", tenant_id, e)

    def _ensure_loaded(self, tenant_id: int) -> None:
        """懒加载：租户设置尚未在内存时从 DB 载入（每个租户仅首次查询一次）"""
        if tenant_id not in self._tenant_permissions and tenant_id not in self._tenant_agents:
            self._db_load(tenant_id)

    # ---------- 租户配置 ----------

    def init_tenant(self, tenant_id: int, agent_names: List[str] = None,
                    permissions: List[str] = None) -> None:
        """初始化租户权限（注册企业时调用）——优先恢复 DB 持久化配置"""
        self._ensure_loaded(tenant_id)
        self._tenant_permissions.setdefault(tenant_id, set(self._default_permissions))
        if agent_names is not None:
            self._tenant_agents.setdefault(tenant_id, set(agent_names))
        self._db_persist(tenant_id)

    def grant_permission(self, tenant_id: int, permission: str) -> None:
        self._ensure_loaded(tenant_id)
        self._tenant_permissions.setdefault(tenant_id, set(self._default_permissions)).add(permission)
        self._db_persist(tenant_id)

    def revoke_permission(self, tenant_id: int, permission: str) -> None:
        self._ensure_loaded(tenant_id)
        self._tenant_permissions.setdefault(tenant_id, set(self._default_permissions)).discard(permission)
        self._db_persist(tenant_id)

    def allow_agent(self, tenant_id: int, agent_name: str) -> None:
        self._ensure_loaded(tenant_id)
        allowed = self._tenant_agents.setdefault(tenant_id, set())
        # 安全修复：首次授权时并入平台默认 Agent 名单（防"全放行→仅白名单"翻转）
        if not allowed:
            allowed.update(self.DEFAULT_AGENT_NAMES)
        allowed.add(agent_name)
        self._db_persist(tenant_id)

    def deny_agent(self, tenant_id: int, agent_name: str) -> None:
        self._ensure_loaded(tenant_id)
        self._tenant_agents.setdefault(tenant_id, set()).discard(agent_name)
        self._db_persist(tenant_id)

    # ---------- 校验 ----------

    def assert_agent_allowed(self, tenant_id: int, agent_name: str) -> None:
        """企业是否有权运行该 Agent"""
        self._ensure_loaded(tenant_id)
        allowed = self._tenant_agents.get(tenant_id, set())
        if not allowed:
            return  # 未配置限制时默认放行（兼容旧版）
        if agent_name not in allowed:
            raise PermissionError(f"企业 {tenant_id} 无权运行 Agent [{agent_name}]")

    def assert_tool_allowed(self, tenant_id: int, agent_name: str, tool_name: str) -> None:
        """Agent 是否被允许调用该 Tool（双重校验：manifest 声明 + 租户权限）"""
        self._ensure_loaded(tenant_id)
        # 1) Agent manifest 声明校验（按租户解析实际生效 manifest：私有优先、回退平台级）
        from hermes.runtime import get_runtime
        registry = get_runtime().agent_registry
        manifest = registry.resolve_agent(agent_name, tenant_id)
        if manifest and manifest.tools and tool_name not in manifest.tools:
            raise PermissionError(
                f"Agent [{agent_name}] 未声明 Tool [{tool_name}]，拒绝调用")

        # 2) Tool 权限点校验（P2 安全修复：未注册 Tool 默认拒绝，fail-closed）
        from hermes.tool.tools import BUILTIN_TOOLS
        tool = BUILTIN_TOOLS.get(tool_name)
        if tool is None:
            logger.warning("RBAC: 未注册 Tool [%s] 被调用，拒绝（fail-closed）", tool_name)
            raise PermissionError(f"Tool [{tool_name}] 未注册，拒绝调用")
        if tool.permissions:
            granted = self._tenant_permissions.get(tenant_id, set(self._default_permissions))
            for perm in tool.permissions:
                if perm not in granted:
                    raise PermissionError(
                        f"企业 {tenant_id} 未授权权限点 [{perm}]，Tool [{tool_name}] 被拒绝")

    def assert_tool_permission(self, tenant_id: int, tool_name: str, permission: str) -> None:
        """校验租户是否授权某 Tool 的某个权限点（供 ToolCenter 无 Agent 上下文时使用）。

        未配置租户回退默认权限集；权限点缺失即拒绝（fail-closed）。
        """
        self._ensure_loaded(tenant_id)
        granted = self._tenant_permissions.get(tenant_id, set(self._default_permissions))
        if permission not in granted:
            raise PermissionError(
                f"企业 {tenant_id} 未授权权限点 [{permission}]，Tool [{tool_name}] 被拒绝")

    def check(self, tenant_id: int, agent_name: str, tool_name: str) -> bool:
        """校验（不抛异常版本）"""
        try:
            self.assert_tool_allowed(tenant_id, agent_name, tool_name)
            return True
        except PermissionError:
            return False
