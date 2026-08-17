"""
api/cms.py —— 平台官网内容管理（CMS）

- GET  /cms/content          公开：返回官网展示内容（前台官网页调用）
- GET  /cms/content/admin    平台超管：返回可编辑内容（结构同上）
- PUT  /cms/content          平台超管：全量更新官网内容
- POST /cms/upload           平台超管：上传图片（客户 LOGO / 首屏背景）返回可访问 URL
"""
import os
import uuid
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import SiteContent
from auth.deps import get_current_user

router = APIRouter(prefix="/cms", tags=["官网内容管理"])

# 图片统一存放到 backend/uploads/site/，前端通过 /static/site/xxx 访问
SITE_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads", "site")
os.makedirs(SITE_UPLOAD_DIR, exist_ok=True)

# 安全修复：禁用 SVG（存储型 XSS 风险）；仅允许位图（上传时经 PIL 重绘校验）
_ALLOWED_IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp",
}


# ============ Pydantic 结构 ============

class FeatureItem(BaseModel):
    icon: str = "🧩"
    title: str = ""
    description: str = ""


class StatItem(BaseModel):
    label: str = ""
    value: str = ""


class CustomerItem(BaseModel):
    name: str = ""
    logo: str = ""            # 图片 URL（/static/site/xxx）或外链；空则展示名称首字占位


class HeroSlideItem(BaseModel):
    image: str = ""           # 轮播图 URL
    title: str = ""           # 单张标题（为空则使用 HeroBlock 顶层标题）
    subtitle: str = ""        # 单张副标题（为空则使用 HeroBlock 顶层副标题）
    cta_text: str = ""        # 单张按钮文案（为空则使用顶层）
    cta_url: str = ""         # 单张按钮链接（为空则使用顶层）


class HeroBlock(BaseModel):
    title: str = ""
    subtitle: str = ""
    description: str = ""
    cta_text: str = "免费体验"
    cta_url: str = "/login"
    slides: list[HeroSlideItem] = []   # 首屏轮播图（多张）


class AboutBlock(BaseModel):
    title: str = "关于 AI Factory"
    description: str = ""
    stats: list[StatItem] = []


class ContactBlock(BaseModel):
    company_name: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    map_url: str = ""         # 地图嵌入链接（可选）


class SocialBlock(BaseModel):
    hotline: str = ""         # 热线电话（点击可拨号）
    qq: str = ""              # 客服 QQ
    wechat_qr: str = ""       # 微信二维码图片 URL（点击放大）


class PlanItem(BaseModel):
    name: str = ""            # 方案名称，如 入门版 / 专业版
    tag: str = ""             # 角标，如 “推荐” “热销”（可选）
    price: str = ""           # 价格数值，如 299 / 价格面议
    period: str = ""          # 计费周期，如 /月 /年
    description: str = ""     # 方案一句话简介
    features: list[str] = []  # 功能点列表（每行一条）
    cta_text: str = "立即咨询"
    cta_url: str = "/login"
    featured: bool = False    # 是否高亮推荐方案


class PricingBlock(BaseModel):
    title: str = "套餐价格"
    subtitle: str = "按需选择，灵活扩展，让每一分投入都产生 AI 价值。"
    plans: list[PlanItem] = []


class SiteContentIn(BaseModel):
    hero: HeroBlock = HeroBlock()
    about: AboutBlock = AboutBlock()
    features: list[FeatureItem] = []
    customers: list[CustomerItem] = []
    contact: ContactBlock = ContactBlock()
    pricing: PricingBlock = PricingBlock()
    social: SocialBlock = SocialBlock()


# ============ 内部工具 ============

def _require_platform(current: dict):
    if current.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要平台管理员权限")


def _get_or_create_row(db: Session) -> SiteContent:
    row = db.query(SiteContent).filter(SiteContent.id == 1).first()
    if not row:
        row = SiteContent(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _normalize_hero(hero: dict | None) -> dict:
    """兼容旧数据：若只有 bg 字段，则自动转成单张轮播图"""
    hero = hero or {}
    if hero.get("bg") and not hero.get("slides"):
        hero["slides"] = [{"image": hero["bg"], "title": "", "subtitle": "", "cta_text": "", "cta_url": ""}]
    return hero


def _protect_hero_slides(hero: dict | None, base_hero: dict | None):
    """保险：若提交的轮播图 slides 为空，但默认内容已有轮播图，则保留默认，避免误清空。"""
    hero = dict(hero) if hero is not None else {}
    base_slides = (base_hero or {}).get("slides") or []
    if not hero.get("slides") and base_slides:
        hero["slides"] = base_slides
    return hero


def _deep_merge(base, over):
    """深合并：over 覆盖 base（相同结构的 dict 递归；list/scalar 直接以 over 覆盖）"""
    if not over:
        return base
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(out.get(k), v) if k in out else v
        return out
    return over


def _deep_diff(base, over):
    """返回 over 相对 base 的差异（仅存不同部分），用于保存某语言的翻译覆盖。
    dict 递归比较；list 只要不同就整体覆盖；scalar 不同才保留；完全相同返回 None。"""
    if isinstance(base, dict) and isinstance(over, dict):
        out = {}
        for k, v in over.items():
            if k not in base:
                out[k] = v
            else:
                d = _deep_diff(base[k], v)
                if d is not None:
                    out[k] = d
        return out or None
    if isinstance(base, list) and isinstance(over, list):
        return over if base != over else None
    return None if base == over else over


def _row_to_dict(row: SiteContent) -> dict:
    return {
        "hero": _normalize_hero(row.hero),
        "about": row.about or {},
        "features": row.features or [],
        "customers": row.customers or [],
        "contact": row.contact or {},
        "pricing": row.pricing or {},
        "social": row.social or {},
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ============ 接口 ============

@router.get("/content")
def get_site_content(lang: str = None, db: Session = Depends(get_db)):
    """公开接口：官网展示内容（无需登录）。
    传入 lang（如 en/de/fr/ja/ko）时，返回默认(zh)内容被该语言翻译覆盖深合并后的结果；
    未传或 lang=zh 时返回默认内容。未翻译的字段自动回退默认语言。"""
    row = db.query(SiteContent).filter(SiteContent.id == 1).first()
    base = _row_to_dict(row) if row else _row_to_dict(SiteContent())
    if lang and lang != "zh":
        trans = (row.translations if row and isinstance(row.translations, dict) else {}) or {}
        over = trans.get(lang)
        if over:
            base = _deep_merge(base, over)
    return base


@router.get("/content/admin")
def get_site_content_admin(db: Session = Depends(get_db),
                           current: dict = Depends(get_current_user)):
    """平台超管：获取可编辑内容（默认语言）以及各语言翻译覆盖"""
    _require_platform(current)
    row = db.query(SiteContent).filter(SiteContent.id == 1).first()
    if not row:
        return {"content": _row_to_dict(SiteContent()), "translations": {}}
    return {
        "content": _row_to_dict(row),
        "translations": (row.translations if isinstance(row.translations, dict) else {}) or {},
    }


@router.put("/content")
def update_site_content(payload: SiteContentIn,
                        db: Session = Depends(get_db),
                        current: dict = Depends(get_current_user)):
    """平台超管：全量更新官网内容（默认语言 zh）"""
    _require_platform(current)
    row = _get_or_create_row(db)
    # 保险：若提交的轮播图为空，但库里已有轮播图，则保留库里原值，避免误清空
    existing = _row_to_dict(row)
    payload_hero = _protect_hero_slides(payload.hero.model_dump(), existing.get("hero"))
    row.hero = payload_hero
    row.about = payload.about.model_dump()
    row.features = [f.model_dump() for f in payload.features]
    row.customers = [c.model_dump() for c in payload.customers]
    row.contact = payload.contact.model_dump()
    row.pricing = payload.pricing.model_dump()
    row.social = payload.social.model_dump()
    # 保存时去除旧 bg 字段，避免脏数据
    if isinstance(row.hero, dict) and "bg" in row.hero:
        del row.hero["bg"]
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "官网内容已更新", "content": _row_to_dict(row)}


@router.put("/content/{lang}")
def update_site_content_lang(lang: str,
                             payload: SiteContentIn,
                             db: Session = Depends(get_db),
                             current: dict = Depends(get_current_user)):
    """平台超管：保存某语言（en/de/fr/ja/ko）的翻译覆盖。
    lang=zh 等价于更新默认内容；其他语言仅存储与默认内容不同的字段（深 diff），
    未填写的字段在前台自动回退默认语言。"""
    _require_platform(current)
    if lang == "zh":
        return update_site_content(payload, db, current)
    row = _get_or_create_row(db)
    base = _row_to_dict(row)
    # 保险：若该语言覆盖试图把轮播图清空，但默认内容已有轮播图，则保留默认不入库
    protected_hero = _protect_hero_slides(payload.hero.model_dump(), base.get("hero"))
    full = payload.model_dump()
    full["hero"] = protected_hero
    over = _deep_diff(base, full)
    # 必须先复制原 dict，避免修改 row.translations 的引用对象，否则 SQLAlchemy 检测不到变化
    translations = dict(row.translations) if isinstance(row.translations, dict) else {}
    if over:
        translations[lang] = over
    else:
        translations.pop(lang, None)
    row.translations = translations
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"message": f"{lang} 翻译已更新", "translations": translations}


def _resize_and_save(content: bytes, ext: str, save_path: str, max_width: int = 1920) -> int:
    """位图（png/jpg/webp）等比缩放（最大宽度 max_width）并压缩保存；SVG 原样写入。
    返回最终文件字节大小。"""
    if ext == "svg":
        with open(save_path, "wb") as f:
            f.write(content)
        return len(content)

    from PIL import Image

    img = Image.open(BytesIO(content))
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA") if has_alpha else img.convert("RGB")

    # 等比缩放（仅当超过最大宽度时）
    if img.width > max_width:
        ratio = max_width / float(img.width)
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.LANCZOS)

    out = BytesIO()
    if ext in ("jpg", "jpeg"):
        img.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
    elif ext == "webp":
        img.save(out, format="WEBP", quality=85, optimize=True)
    else:  # png
        img.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    with open(save_path, "wb") as f:
        f.write(data)
    return len(data)


@router.post("/upload")
async def upload_site_image(file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            current: dict = Depends(get_current_user)):
    """平台超管：上传官网图片（客户 LOGO / 首屏背景），返回静态访问 URL。
    位图会自动等比缩放（最大宽度 CMS_MAX_IMAGE_WIDTH，默认 1920）并压缩以节省存储、加快加载。"""
    _require_platform(current)
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="仅支持 PNG / JPG / WEBP 图片（SVG 已禁用，防存储型 XSS）")

    ext = content_type.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    filename = f"{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(SITE_UPLOAD_DIR, filename)

    # 安全修复：上传大小限制（10MB），限长读取避免内存耗尽
    max_bytes = int(os.getenv("CMS_MAX_UPLOAD_MB", "10")) * 1024 * 1024
    original = await file.read(max_bytes + 1)
    if len(original) > max_bytes:
        raise HTTPException(status_code=413, detail=f"图片过大，最大 {max_bytes // (1024*1024)}MB")

    max_width = int(os.getenv("CMS_MAX_IMAGE_WIDTH", "1920"))
    try:
        final_size = _resize_and_save(original, ext, save_path, max_width)
    except Exception:
        raise HTTPException(status_code=400, detail="无效的图片文件（解码失败）")

    return {
        "url": f"/static/site/{filename}",
        "filename": filename,
        "original_bytes": len(original),
        "saved_bytes": final_size,
        "ratio": round(final_size / len(original), 3) if original else None,
    }
