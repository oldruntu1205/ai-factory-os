"""
api/agent_chat.py —— AI 员工对话接口（第十二章）

POST /agent/chat 支持两种调用：
- 指定 agent_id（按角色直调，如销售/报价/生产/质量/CEO）
- 不指定则走 LangGraph 自动意图路由

所有对话记录按企业隔离存储。
"""
import os
import uuid
import tempfile

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import AIAgent, Conversation, Customer
from auth.deps import get_current_user, require_company
from agents.service import execute_agent, execute_specialized
from agents.drawing_chat import drawing_chat as run_drawing_chat
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
        # P2 优化：主聊天路径注入 ToolCenter，让生产/质量/工艺多步 Agent 可取实时数据
        tools = None
        try:
            from hermes.runtime import get_runtime
            tools = get_runtime().tool_center
        except Exception:
            pass
        answer = execute_specialized(agent, data.message, data.company_id,
                                     db, data.customer_id, tools=tools)
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


# ---------- AI 图纸助手：对话级附件（不进 RAG） ----------
# 支持的文件类型（前端 accept 与之对应）
_ALLOWED_EXTS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md", ".csv",
    ".dxf", ".dwg",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
}
_MAX_UPLOAD_MB = 20


@router.post("/drawing-chat")
async def drawing_chat(
    message: str = Form(""),
    company_id: int = Form(...),
    files: List[UploadFile] = File(default=[]),
    customer_id: Optional[int] = Form(None),
    session_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current: dict = Depends(get_current_user),
):
    """AI 图纸助手：上传图片/CAD/PDF/文本，结合提问由工艺/图纸助手作答。

    ⚠️ 关键约定：附件属于**当前对话逻辑**，**绝不写入 RAG 知识库**
    （不调用 knowledge 上传 / 解析入库），仅作为本次对话的随附上下文。
    """
    require_company(company_id, current)
    require_verified(current.get("license_verified", False))

    session_id = session_id or uuid.uuid4().hex[:16]

    saved: List[str] = []
    ok_files: List[tuple] = []   # (path, ext, original_name)
    try:
        for up in files:
            if not up.filename:
                continue
            ext = os.path.splitext(up.filename)[1].lower()
            if ext not in _ALLOWED_EXTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的文件类型：{ext}（支持 PDF/Word/Excel/文本/CAD/图片）")
            content = await up.read()
            if not content:
                continue
            if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(
                    status_code=400,
                    detail=f"文件过大（上限 {_MAX_UPLOAD_MB}MB）：{up.filename}")
            suffix = ext or ".bin"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.close()
            with open(tmp.name, "wb") as f:
                f.write(content)
            saved.append(tmp.name)
            ok_files.append((tmp.name, ext, up.filename))

        result = run_drawing_chat(
            message or "", company_id, ok_files, db=db, customer_id=customer_id)
    finally:
        # 无论成败都清理临时文件（不落盘、不进 RAG）
        for p in saved:
            try:
                os.remove(p)
            except OSError:
                pass

    answer = result["answer"]
    user_msg = (message or "").strip()
    if ok_files:
        names = "、".join(n for (_, _, n) in ok_files)
        user_msg = (user_msg + f"\n[随附文件: {names}]").strip()

    conv = Conversation(
        session_id=session_id,
        agent_id=None,            # 图纸助手为会话级，无固定 agent_id
        user_message=user_msg,
        agent_answer=answer,
        channel="drawing_chat",
        company_id=company_id,
    )
    db.add(conv)
    db.commit()

    return {
        "answer": answer,
        "session_id": session_id,
        "agent": "工艺/图纸助手",
        "mock_mode": result.get("mock_mode", is_mock()),
        "vision_used": result.get("vision_used", False),
        "parsed_files": result.get("parsed_files", []),
    }
