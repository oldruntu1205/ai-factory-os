"""
hermes/registry/models.py —— Agent 注册表模型（第5章）

Agent 注册信息（对应手册示例）：
{
    "name": "sales_agent",
    "version": "1.0",
    "industry": "glassware",
    "tools": ["crm", "email", "quotation"]
}
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class AgentManifest:
    """Agent 注册清单：声明 Agent 的身份、能力与生命周期"""
    name: str                                   # 唯一名，如 sales_agent
    version: str = "1.0"
    industry: str = "general"                   # 行业标签，如 glassware
    label: str = ""                             # 展示名，如 "AI销售员工"
    description: str = ""
    tools: List[str] = field(default_factory=list)   # 允许调用的 Tool 名单
    permissions: List[str] = field(default_factory=list)  # 所需权限点
    system_prompt: str = ""                     # 自定义 Agent 系统提示词（prompt 驱动编排）
    handler: Optional[Callable] = None          # 处理函数 (message, runtime_ctx) -> str
    workflow: Optional[str] = None              # 可选 LangGraph 工作流名
    status: str = "registered"                  # registered / running / disabled
    company_id: int = 0                         # 注册企业（0=平台级）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "industry": self.industry,
            "label": self.label,
            "description": self.description,
            "tools": self.tools,
            "permissions": self.permissions,
            "system_prompt": self.system_prompt,
            "workflow": self.workflow,
            "status": self.status,
        }
