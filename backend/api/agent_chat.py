"""
api/agent_chat.py —— AI 员工对话接口（第十二章）

POST /agent/chat 支持两种调用：
- 指定 agent_id（按角色直调，如销售/报价/生产/质量/CEO）
- 不指定则走 LangGraph 自动意图路由

所有对话记录按企业隔离存储。
"""
import uuid

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AIAgent, Conversation, Customer
from auth.deps import get_current_user, require_company
from agents.service import execute_agent, execute_specialized
from ai.llm import is_mock
from api.company import require_verified

router = APIRouter(prefix="/agent", tags=["AI员工对话"])


class ChatIn(BaseModel):
    message: str
    company_id: int
    agent_id: Optional[int] = None
    customer_id: Optional[int] = None
    session_id: Optional[str] = None


@router.post("/chat")
def chat(data: ChatIn, db: Session = Depends(get_db),
         current: dict = Depends(get_current_user)):
    require_company(data.company_id, current)
    require_verified(current.get("license_verified", False))

    session_id = data.session_id or uuid.uuid4().hex[:16]

    if data.agent_id:
        agent = db.query(AIAgent).filter(
            AIAgent.id == data.agent_id,
            AIAgent.company_id == data.company_id).first()
        if not agent:
            raise HTTPException(status_code=404, detail="AI 员工不存在")
        if agent.status != "active":
            raise HTTPException(status_code=400, detail="该 AI 员工已停用")
        answer = execute_specialized(agent, data.message, data.company_id,
                                     db, data.customer_id)
        role_label = agent.role_label
    else:
        answer = execute_agent(data.message, data.company_id, data.customer_id)
        role_label = "AI员工"

    conv = Conversation(
        session_id=session_id,
        agent_id=data.agent_id,
        user_message=data.message,
        agent_answer=answer,
        channel="web",
        company_id=data.company_id,
    )
    db.add(conv)
    db.commit()

    return {
        "answer": answer,
        "session_id": session_id,
        "agent": role_label,
        "mock_mode": is_mock(),
    }


@router.get("/conversations")
def list_conversations(company_id: int, page: int = 1, page_size: int = 100,
                       db: Session = Depends(get_db),
                       current: dict = Depends(get_current_user)):
    """企业对话历史（分页，默认返回前 100 条兼容旧前端）"""
    require_company(company_id, current)
    query = db.query(Conversation).filter(Conversation.company_id == company_id)
    total = query.count()
    convs = query.order_by(Conversation.created_at.desc())\
        .offset((page - 1) * page_size).limit(page_size).all()
    return {
        "conversations": [{
            "id": c.id, "session_id": c.session_id, "agent_id": c.agent_id,
            "user_message": c.user_message, "agent_answer": c.agent_answer,
            "channel": c.channel, "created_at": c.created_at.isoformat(),
        } for c in convs],
        "total": total, "page": page, "page_size": page_size,
    }
