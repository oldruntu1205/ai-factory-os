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
                permission_checker=None) -> dict:
        """执行 Tool（含可选权限校验），并把调用步骤回写到当前执行轨迹"""
        tool = self.get_tool(tool_name)
        if not tool:
            return {"ok": False, "tool": tool_name, "error": f"Tool [{tool_name}] 不存在"}

        # 权限校验
        if permission_checker:
            for perm in tool.permissions:
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
