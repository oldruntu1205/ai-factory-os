"""
agents/state.py —— Agent 状态管理（第五章）
"""
from typing import TypedDict, Optional


class AgentState(TypedDict):
    message: str
    company_id: int
    customer_id: Optional[int]
    intent: str
    result: str
