"""
hermes/tool/center.py —— Tool Center（第6章）

Tool Router：Agent → Tool Router → Permission Check → 企业系统 API
"""
import logging
from typing import Dict, Optional

from hermes.tool.tools import BUILTIN_TOOLS, Tool

logger = logging.getLogger(__name__)


class ToolCenter:
    """工具中心：注册 Tool、路由 Tool 调用"""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        for name, tool in BUILTIN_TOOLS.items():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.info(f"ToolCenter: tool [{tool.name}] 注册成功")

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def description_of(self, tool_name: str, company_id: int = None) -> str:
        """返回 Tool 描述：优先企业级覆盖（tool_overrides 表），回退内置硬编码。"""
        if company_id:
            try:
                from hermes.evolution.store import get_tool_override
                ov = get_tool_override(company_id, tool_name)
                if ov:
                    return ov
            except Exception:
                pass
        tool = self.get_tool(tool_name)
        return tool.description if tool else ""

    def list_tools(self, company_id: int = None) -> list:
        out = []
        for t in self._tools.values():
            overridden = False
            desc = t.description
            if company_id:
                try:
                    from hermes.evolution.store import get_tool_override
                    ov = get_tool_override(company_id, t.name)
                    if ov:
                        desc, overridden = ov, True
                except Exception:
                    pass
            out.append({"name": t.name, "description": desc,
                        "permissions": t.permissions, "overridden": overridden})
        return out

    def execute(self, tool_name: str, params: dict, company_id: int,
                permission_checker=None, agent_name: str = None) -> dict:
        """执行 Tool（含权限校验），并把调用步骤回写到当前执行轨迹。

        安全加固（RBAC 深度改造 Step1/2）：
        - 未显式传入 permission_checker 时，自动使用全局 Hermes RBAC 校验：
          * 传了 agent_name → 完整校验（manifest 工具声明 + 租户权限点）
          * 未传 agent_name → 租户权限点校验
        - 默认权限集下行为不变；租户 revoke 后调用即被拦截。
        - runtime 未初始化（启动早期/单元测试）时保持不拦截，避免启动失败。
        """
        tool = self.get_tool(tool_name)
        if not tool:
            return {"ok": False, "tool": tool_name, "error": f"Tool [{tool_name}] 不存在"}

        # 权限校验：显式 checker 优先；否则回退全局 RBAC。
        # fail-closed 修复：agent 上下文无条件执行完整校验（manifest 工具白名单 + 租户权限点），
        # 不再因 tool.permissions 为空而跳过 manifest 声明校验（原『空清单=放行』漏洞路径）。
        if permission_checker is None:
            try:
                from hermes.runtime import get_runtime
                perm_sys = get_runtime().permission_system
                if perm_sys is not None:
                    if agent_name:
                        def _full_check(_perm):
                            perm_sys.assert_tool_allowed(company_id, agent_name, tool_name)
                        permission_checker = _full_check
                    elif tool.permissions:
                        permission_checker = lambda perm: perm_sys.assert_tool_permission(
                            company_id, tool_name, perm)
            except Exception:
                permission_checker = None  # runtime 未初始化时不拦截

        if permission_checker:
            for perm in (tool.permissions or [None]):
                permission_checker(perm)

        import time as _time
        from hermes.evolution.trace import get_recorder
        rec = get_recorder()
        started = _time.perf_counter()
        result = tool.execute(params, company_id)
        latency_ms = (_time.perf_counter() - started) * 1000.0
        if rec is not None:
            rec.record_tool(tool_name, params, result, latency_ms)
        return result
