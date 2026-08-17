"""
api/knowledge.py —— 企业知识库接口（第十二章 + RAG 查询）

- POST /knowledge/upload   上传文档并向量化入库
- GET  /knowledge/list     企业文档列表
- GET  /knowledge/search   RAG 检索（返回相关片段）
- DELETE /knowledge/{doc_id} 删除文档及向量
"""
import os
import logging

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field
from typing import Optional

from database import get_db
from models import Document, DrawingParam
from auth.deps import get_current_user, require_company
from api.company import require_verified
from knowledge.upload import save_file
from knowledge.parser import parse_file
from knowledge.splitter import split_text
from knowledge.embedding import create_embedding
from knowledge.vector import save_vectors, delete_document_vectors, delete_company_collection
from knowledge.cad_parser import extract_drawing_params
from knowledge.hybrid import (
    index_chunks, remove_document, clear_bm25, hybrid_search,
)

router = APIRouter(prefix="/knowledge", tags=["企业知识库"])


class SearchIn(BaseModel):
    question: str
    company_id: int
    # 安全修复：limit 约束 1-20（此前无上限可被滥用拖垮检索）
    limit: int = Field(5, ge=1, le=20)
    score_threshold: Optional[float] = Field(None, ge=-1.0, le=1.0)


@router.post("/upload")
def upload(
    company_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: dict = Depends(get_current_user),
):
    """上传企业知识文档：保存 -> 解析 -> 切片 -> 向量化 -> 入库（需企业已认证）"""
    require_company(company_id, current)
    require_verified(current.get("license_verified", False))

    path = save_file(file, company_id)
    size = os.path.getsize(path)

    doc = Document(
        filename=file.filename,
        path=path,
        file_type=os.path.splitext(file.filename or "")[1].lstrip("."),
        size=os.path.getsize(path),
        status="parsed",
        company_id=company_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    bm25_ok = True
    try:
        text = parse_file(path)
        if not text or not text.strip():
            doc.status = "failed"
            db.commit()
            return {"status": "warning", "message": "未能从文档中提取文本", "doc_id": doc.id}
        chunks = split_text(text)
        vectors = create_embedding(chunks)
        save_vectors(vectors, chunks, company_id, document_id=doc.id,
                     source_file=file.filename)
        # 混合检索：同步建立 BM25 关键词索引（带 document_id 便于精准回收）；
        # 落库失败不阻塞主流程，返回 False 由内部补写队列自动重试
        bm25_ok = index_chunks(chunks, company_id, source_file=file.filename,
                               document_id=doc.id)
        doc.chunk_count = len(chunks)
        doc.status = "vectorized"
        db.commit()
    except Exception as e:
        doc.status = "failed"
        db.commit()
        raise HTTPException(status_code=500, detail=f"文档处理失败: {e}")

    # 结构化参数抽取（CAD/PDF/DOC/XLSX 通用）：写入 DrawingParam 表
    param_count = 0
    try:
        params = extract_drawing_params(text, source_file=file.filename)
        for p in params:
            dp = DrawingParam(
                company_id=company_id,
                document_id=doc.id,
                product_model=p.get("product_model", ""),
                category=p.get("category", ""),
                param_name=p.get("param_name", ""),
                value=p.get("value", ""),
                raw=p.get("raw", ""),
                source_file=p.get("source_file", ""),
                location=p.get("location", ""),
            )
            db.add(dp)
        db.commit()
        param_count = len(params)
    except Exception as e:
        # 参数抽取失败不影响主流程（向量检索仍可用）
        logger.warning("图纸参数抽取失败（不影响向量检索）: %s", e)

    return {"status": "success", "chunks": len(chunks), "doc_id": doc.id,
            "filename": file.filename, "extracted_params": param_count,
            "bm25_persisted": bm25_ok,
            "message": "文档处理成功" + ("" if bm25_ok else "；BM25 稀疏索引落库暂缓，系统将自动补写")}


@router.get("/list")
def list_documents(company_id: int, db: Session = Depends(get_db),
                   current: dict = Depends(get_current_user)):
    """企业知识文档列表"""
    require_company(company_id, current)
    docs = db.query(Document).filter(Document.company_id == company_id)\
        .order_by(Document.created_at.desc()).all()
    return {"documents": [{
        "id": d.id, "filename": d.filename, "file_type": d.file_type,
        "size": d.size, "chunk_count": d.chunk_count, "status": d.status,
        "created_at": d.created_at.isoformat(),
    } for d in docs]}


@router.post("/search")
def search(data: SearchIn, current: dict = Depends(get_current_user)):
    """RAG 检索：问题 -> 企业知识片段（混合检索：稠密向量 + BM25 关键词融合）"""
    require_company(data.company_id, current)
    results = hybrid_search(data.question, data.company_id, data.limit,
                            score_threshold=data.score_threshold)
    return {"question": data.question, "results": results, "count": len(results)}


@router.delete("/{doc_id}")
def delete_document(doc_id: int, company_id: int, db: Session = Depends(get_db),
                    current: dict = Depends(get_current_user)):
    """删除文档（保留企业其他文档向量）"""
    require_company(company_id, current)
    doc = db.query(Document).filter(
        Document.id == doc_id, Document.company_id == company_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    # 先删向量，失败则不上库（避免"文档删了但知识仍在召回"）
    try:
        delete_document_vectors(company_id, doc.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"向量删除失败: {e}")
    # 同步回收结构化参数 + BM25 稀疏索引（否则删了图仍能查到参数/命中稀疏召回）
    db.query(DrawingParam).filter(DrawingParam.document_id == doc.id).delete()
    remove_document(company_id, doc.id)
    db.commit()
    try:
        if os.path.exists(doc.path):
            os.remove(doc.path)
    except OSError:
        pass
    db.delete(doc)
    db.commit()
    return {"message": "delete success"}


@router.delete("/clear/{company_id}")
def clear_company(company_id: int, db: Session = Depends(get_db),
                  current: dict = Depends(get_current_user)):
    """清空企业全部知识库"""
    require_company(company_id, current)
    docs = db.query(Document).filter(Document.company_id == company_id).all()
    # 同步清空该企业的结构化参数 + BM25 稀疏索引
    db.query(DrawingParam).filter(DrawingParam.company_id == company_id).delete()
    clear_bm25(company_id)
    for d in docs:
        try:
            if os.path.exists(d.path):
                os.remove(d.path)
        except OSError:
            pass
        db.delete(d)
    delete_company_collection(company_id)
    db.commit()
    return {"message": "clear success"}
