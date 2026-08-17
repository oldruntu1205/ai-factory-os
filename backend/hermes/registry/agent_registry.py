"""
hermes/registry/agent_registry.py —— Agent 注册中心（第5章）

功能：Agent 注册 / 发现 / 版本管理 / 生命周期管理。
让平台能动态加载任意行业的 AI 员工（销售/客服/报价/生产/质量...）。
"""
import logging
from typing import Dict, List, Optional

from hermes.registry.models import AgentManifest

logger = logging.getLogger(__name__)


class AgentRegistry:
    """内存版注册中心。生产可扩展为 Redis/PostgreSQL 存储 + 分布式同步。"""

    def __init__(self):
        self._agents: Dict[str, AgentManifest] = {}

    # ---------- 注册 / 注销 ----------

    def register(self, manifest: AgentManifest) -> AgentManifest:
        """注册 Agent（同名新版本覆盖旧版本，实现版本管理）"""
        self._agents[manifest.name] = manifest
        logger.info(f"Registry: agent [{manifest.name}] v{manifest.version} 注册成功, tools={manifest.tools}")
        return manifest

    def unregister(self, name: str) -> bool:
        """注销 Agent"""
        return self._agents.pop(name, None) is not None

    # ---------- 发现 ----------

    def get_agent(self, name: str) -> Optional[AgentManifest]:
        return self._agents.get(name)

    def list_agents(self, industry: Optional[str] = None,
                    status: Optional[str] = None) -> List[AgentManifest]:
        result = []
        for a in self._agents.values():
            if industry and a.industry != industry:
                continue
            if status and a.status != status:
                continue
            result.append(a)
        return result

    def discover(self, industry: str) -> List[AgentManifest]:
        """按行业发现可用 Agent"""
        return [a for a in self._agents.values()
                if a.industry == industry and a.status != "disabled"]

    # ---------- 生命周期 ----------

    def activate(self, name: str) -> bool:
        a = self._agents.get(name)
        if a:
            a.status = "registered"
            return True
        return False

    def deactivate(self, name: str) -> bool:
        a = self._agents.get(name)
        if a:
            a.status = "disabled"
            return True
        return False

    # ---------- 执行 ----------

    def invoke_agent(self, name: str, message: str, runtime_ctx: dict) -> str:
        """调用 Agent 实现（handler 或默认工作流）"""
        manifest = self.get_agent(name)
        if not manifest:
            raise ValueError(f"Agent [{name}] 未注册")
        if manifest.status == "disabled":
            raise ValueError(f"Agent [{name}] 已停用")

        if manifest.handler:
            return manifest.handler(message, runtime_ctx)

        # 无 handler 时，退回通用 LangGraph 工作流
        from agents.service import execute_agent
        return execute_agent(message, runtime_ctx["company_id"])
