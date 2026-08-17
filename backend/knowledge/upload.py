"""
knowledge/upload.py —— 文件上传系统（第四章）

按企业隔离存储：uploads/{company_id}/{filename}
"""
import os
import uuid
from datetime import datetime

from fastapi import UploadFile, HTTPException

from config import UPLOAD_DIR, MAX_UPLOAD_SIZE_MB

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md", ".csv",
                      ".dxf", ".dwg"}


def save_file(file: UploadFile, company_id: int) -> str:
    """保存上传文件，返回绝对路径"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}，支持 {sorted(ALLOWED_EXTENSIONS)}")

    company_dir = os.path.join(UPLOAD_DIR, str(company_id))
    os.makedirs(company_dir, exist_ok=True)

    # 防止文件名冲突与路径穿越
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    path = os.path.join(company_dir, safe_name)

    content = file.file.read()
    if len(content) > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {MAX_UPLOAD_SIZE_MB}MB 限制")

    with open(path, "wb") as f:
        f.write(content)

    return path
