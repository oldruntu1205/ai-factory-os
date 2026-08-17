"""
agents/drawing_chat.py —— AI 图纸助手「对话级附件」逻辑

设计原则（与用户约定一致）：
- 用户在对话页面上传的图片 / CAD / PDF / 文本，属于**当前对话逻辑**，
  **绝不写入 RAG 知识库**（不调用 knowledge 上传 / 解析入库）。
- 文本/CAD 文件抽取为「随附上下文」，注入 process_agent 的 extra_context，
  复用结构化参数快路径 + 混合检索 + 防误操作提示。
- 图片文件：若当前模型支持视觉（OpenAI gpt-4o 等）则走多模态识别；
  否则仅作提示，引导用户上传 DXF/PDF/文本或切换视觉模型。

输入 files: list of (path, ext, original_name)，由接口层保存到临时文件后传入。
"""
import os
from typing import List, Optional

from knowledge.parser import parse_file
from knowledge.cad_parser import extract_drawing_params
from agents.process import process_agent
from ai.llm import is_mock, is_vision_capable, vision_invoke

# 可抽取文本的附件类型
TEXT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls",
             ".txt", ".md", ".csv", ".dxf", ".dwg"}
# 图片附件类型（需要视觉模型才能识别内容）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

# 单文件抽取文本上限 / 全部随附文本上限（控制 token 成本）
MAX_FILE_TEXT = int(os.getenv("DRAWING_MAX_TEXT", "8000"))
MAX_TOTAL_TEXT = int(os.getenv("DRAWING_MAX_TOTAL", "20000"))


def drawing_chat(message: str, company_id: int,
                 files: List[tuple], db=None, customer_id: int = None) -> dict:
    """files: [(path, ext, original_name), ...]

    返回 {
        "answer": str,
        "mock_mode": bool,
        "vision_used": bool,
        "parsed_files": [ {name, type, chars, params?, note?} ],
        "user_message": str,
    }
    """
    text_segments: List[tuple] = []   # (name, text)
    image_names: List[str] = []
    notes: List[str] = []
    parsed_files: List[dict] = []
    total = 0

    for path, ext, name in files:
        if ext in IMAGE_EXTS:
            image_names.append(name)
            parsed_files.append({"name": name, "type": "image", "chars": 0})
            continue
        if ext in TEXT_EXTS:
            try:
                txt = parse_file(path) or ""
            except Exception as e:
                notes.append(f"文件「{name}」解析失败：{e}")
                parsed_files.append({"name": name, "type": "text",
                                     "chars": 0, "note": f"解析失败：{e}"})
                continue
            # 结构化参数抽取（用于解析摘要展示）
            params = []
            try:
                params = extract_drawing_params(txt, source_file=name)[:30]
            except Exception:
                pass
            if not txt.strip():
                notes.append(f"文件「{name}」未抽取到可识别文本（可能是扫描图片或空文件）。")
            elif total < MAX_TOTAL_TEXT:
                seg = txt[:MAX_FILE_TEXT]
                total += len(seg)
                text_segments.append((name, seg))
            else:
                notes.append(f"文件「{name}」内容过长，已截断（仅取前 {MAX_FILE_TEXT} 字）。")
            parsed_files.append({
                "name": name, "type": "text", "chars": len(txt),
                "params": [{"category": p["category"],
                            "param_name": p["param_name"],
                            "value": p["value"]} for p in params],
            })
        else:
            notes.append(f"文件「{name}」格式不支持（支持 PDF/Word/Excel/文本/CAD 图纸）。")
            parsed_files.append({"name": name, "type": "unsupported",
                                 "chars": 0, "note": "格式不支持"})

    # 组装随附上下文（不进 RAG）
    extra_blocks = [f"【文件: {name}】\n{txt}" for name, txt in text_segments]
    if image_names and is_vision_capable():
        extra_blocks.append("【图片附件（模型可直接识别）】" + "、".join(image_names))
    elif image_names:
        notes.append(
            "当前模型不支持图片识别（如手绘图纸照片）。可上传 DXF/PDF/文本类图纸，"
            "或在「平台设置-模型设置」中切换为支持视觉的模型（如 OpenAI gpt-4o）。"
        )
    extra_context = "\n\n".join(extra_blocks) if extra_blocks else None

    vision_used = bool(image_names and is_vision_capable())
    if vision_used:
        image_paths = [p for (p, e, n) in files if e in IMAGE_EXTS]
        answer = _vision_answer(message, image_paths, text_segments)
    else:
        augmented = message or "（用户仅上传了图纸，未输入文字问题，请基于图纸协助分析）"
        answer = process_agent(augmented, company_id, db=db, extra_context=extra_context)

    # 解析提示追加到答案末尾（不干扰 process_agent 的严谨回答本身）
    if notes:
        answer = answer + "\n\n---\n📎 附件解析提示：\n" + "\n".join("- " + n for n in notes)

    return {
        "answer": answer,
        "mock_mode": is_mock(),
        "vision_used": vision_used,
        "parsed_files": parsed_files,
        "user_message": message,
    }


def _vision_answer(message: str, image_paths: List[str],
                   text_segments: List[tuple]) -> str:
    """视觉多模态路径：直接让视觉模型基于图片 + 问题作答（防误操作优先）。"""
    context = "\n\n".join(f"【文件: {name}】\n{txt}" for name, txt in text_segments)
    prompt = (
        "你是制造企业「工艺/图纸助手」，服务车间、工艺与质检人员。\n"
        "你的首要职责是**防范工人误操作**：基于图纸/手绘稿真实内容给出参数解读、"
        "尺寸/公差/材质判断，不得编造；对易错/禁止操作要明确警示。\n\n"
        "# 用户随附的图片/图纸（请基于图片内容回答）\n"
        + "、".join(image_paths)
        + "\n\n# 用户问题\n"
        + (message or "请描述这张图纸/手绘稿的内容，并说明关键尺寸、材质与加工注意点。")
        + ("\n\n# 随附的可提取文本（如有）\n" + context if context else "")
        + "\n\n# 回答要求\n"
        "1. 若图片含具体型号/尺寸/公差/材质：先给「精确解读」，并标注「来自图片，建议以原图为准」。\n"
        "2. 主动给出「防误操作提示」：易错点、禁止操作、加工/装配注意。\n"
        "3. 信息不足时明确说明，并建议上传更清晰的图纸或 DXF/PDF。"
    )
    return vision_invoke(prompt, image_paths)
