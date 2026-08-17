"""
channels/email/sequence.py —— AI 邮件跟进序列（V2.1 邮件自动化，升级版）

核心能力：
- 统一跟进节奏 SEQUENCE_INTERVALS = [0, 3, 7, 14]（首封 / D+3 / D+7 / D+14）
- build_sequence(language, variant): 中/英开发信，支持 A/B 文案变体（subject 维度）
- send_follow_up: 发送第 N 步跟进，自动带报价 PDF；注入打开追踪像素 + 退订链接
- run_due_follow_ups: 定时入口（Celery beat 或手动触发），自动推进到期线索；
  自动跳过 paused / unsubscribed / replied / won / converted 状态

英文格式：Subject / Dear... / 正文段落 / Call-to-action / Best regards 签名块
"""
import json
import logging
import os
import re
import html as _html
from datetime import datetime, timedelta

from channels.email.generator import create_email
from channels.email.smtp import send_email
from ai.llm import get_llm
from config import APP_PUBLIC_URL

logger = logging.getLogger(__name__)

# 跟进节奏（天数）：首封 / D+3 / D+7 / D+14
SEQUENCE_INTERVALS = [0, 3, 7, 14]
MAX_STEP = len(SEQUENCE_INTERVALS) - 1  # 3 -> 第4封

# 自动跳过这些状态的线索（不再发信）
_STOP_STATUSES = ("paused", "unsubscribed", "replied", "won", "converted")

# ========== 中文模板（降级）==========
_ZH_TEMPLATES = {
    "email1": (
        "尊敬的[{company}]：\n\n"
        "您好！我是[公司名称]的外贸销售代表[您的姓名]。\n"
        "我们专注于高品质[{product}]的研发与生产，产品远销欧美、东南亚，"
        "已为多家实验室与化工企业提供稳定供货。\n\n"
        "随函附上产品目录与认证资料（ISO 9001、CE、SGS），如贵司有采购需求，"
        "可提供免费样品、报价单及技术支持。\n\n"
        "期待与您建立长期合作关系！\n\n"
        "祝 商祺！\n\n"
        "[您的姓名]\n[公司名称] | 联系电话 | 邮箱地址"
    ),
    "email2": (
        "尊敬的[{company}]：\n\n"
        "您好！距离上一封邮件已过去 3 天，不知您是否已查阅。\n"
        "为了更好地服务于您，我们整理了 [{product}] 的差异化卖点：\n"
        "1) 原料：选用高纯度硼硅酸盐玻璃，热膨胀系数低\n"
        "2) 工艺：自动化生产线，关键尺寸误差 < 0.05mm\n"
        "3) 认证：ISO 9001 / CE / SGS 体系完备\n\n"
        "若您有任何疑问，欢迎随时来电或邮件咨询，期待您的反馈！\n\n"
        "[您的姓名]\n[公司名称]"
    ),
    "email3": (
        "尊敬的[{company}]：\n\n"
        "您好！这是关于 [{product}] 的最后一封跟进邮件。\n\n"
        "【限时优惠】即日起至本月底，凡下单即可享 5% 折扣，并免费赠送配套耐热手套一套。\n"
        "【库存提醒】由于近期订单激增，最受欢迎的规格库存仅剩少量，下一批补货需等 3-4 周。\n\n"
        "如果您有采购计划，请尽快回复此邮件或致电，我们为您预留库存并锁定优惠价格。"
        "若暂时不需要，也请告知，以便我们不再打扰。\n\n"
        "期待您的尽快回复，以免错失良机！\n\n"
        "祝 工作顺利！\n"
        "[您的姓名]\n[公司名称] | 联系电话 | [邮箱地址]"
    ),
}

# ========== 英文模板（外贸标准格式）==========
_EN_TEMPLATES = {
    "email1": (
        "Subject: Business Cooperation on Premium {product} | From [Your Company]\n\n"
        "Dear Sir/Madam,\n\n"
        "Greetings from [Your Company]! We are a leading manufacturer of high-quality {product}, "
        "with over 10 years of experience serving laboratories and industrial clients across Europe, "
        "Southeast Asia, and the Americas.\n\n"
        "We are pleased to introduce our {product} line, which features:\n"
        "  - Material: High-purity borosilicate glass, low thermal expansion\n"
        "  - Certifications: ISO 9001, CE, SGS compliant\n"
        "  - MOQ: 1,000 pieces | Lead time: 30 days | Payment: T/T 30% deposit\n\n"
        "For your reference, I have attached our product catalog and certification documents. "
        "We would be glad to provide free samples, a formal quotation, and full technical support "
        "upon your request.\n\n"
        "May I ask if this is of interest to your team? I'd welcome the opportunity to discuss "
        "your specific requirements at your convenience.\n\n"
        "Looking forward to your kind reply.\n\n"
        "Best regards,\n"
        "[Your Name]\n"
        "[Your Company] | Tel: +86-xxx-xxxx-xxxx\n"
        "Email: sales@yourcompany.com | Web: www.yourcompany.com"
    ),
    "email2": (
        "Subject: Re: Quick Follow-up | Custom {product} Solutions for {company}\n\n"
        "Dear Sir/Madam,\n\n"
        "I hope this message finds you well. I am writing to follow up on my previous email "
        "sent 3 days ago regarding our {product} for {company}.\n\n"
        "Just to give you a quick recap, here are the key benefits our customers appreciate most:\n"
        "  1. Consistent quality — every batch goes through 3-stage QC inspection\n"
        "  2. Flexible customization — OEM/ODM service available upon request\n"
        "  3. Competitive pricing — direct factory supply, no middlemen\n\n"
        "If you have any questions or would like to see a detailed quotation, please do not "
        "hesitate to reach out. I am happy to arrange a brief call or send additional samples.\n\n"
        "Thank you for your time, and I look forward to hearing from you.\n\n"
        "Best regards,\n"
        "[Your Name]\n"
        "[Your Company] | Tel: +86-xxx-xxxx-xxxx\n"
        "Email: sales@yourcompany.com"
    ),
    "email3": (
        "Subject: Final Notice | Limited Stock of {product} | Reply by [Specific Date]\n\n"
        "Dear Sir/Madam,\n\n"
        "I am reaching out one last time regarding our {product}, as we are approaching "
        "the end of this quarter's production cycle.\n\n"
        "📌 Limited-Time Offer: Place an order by [Specific Date] to enjoy a 5% discount and "
        "receive a complimentary set of heat-resistant gloves.\n\n"
        "⚠️ Stock Alert: Due to a recent surge in orders, our most popular specifications "
        "(e.g., 250mL, 500mL, and 1000mL) are nearly out of stock. The next production batch "
        "will not be available for 3-4 weeks.\n\n"
        "If you have a purchasing plan, kindly reply to this email or call us so we can reserve "
        "stock and lock in the discount. If now is not the right time, please let us know so we "
        "will not follow up further.\n\n"
        "We hope to have the opportunity to serve you. Thank you for your kind consideration.\n\n"
        "Best regards,\n"
        "[Your Name]\n"
        "[Your Company] | Tel: +86-xxx-xxxx-xxxx\n"
        "Email: sales@yourcompany.com | Web: www.yourcompany.com"
    ),
}


def subject_for(step: int, language: str, product: str, company: str, variant: str = "A") -> str:
    """A/B 变体主题：A=标准合作主题，B=利益/紧迫感主题"""
    if language == "en":
        if variant == "B":
            return f"Quick question about {product} for {company}"
        return f"Business Cooperation on Premium {product} | From [Your Company]"
    # 中文
    if variant == "B":
        return f"关于 {product} 的限时合作机会（{company}）"
    return f"Business Cooperation - {product}"


def build_sequence(company: str, product: str, extra: str = "",
                   language: str = "zh", variant: str = "A") -> dict:
    """生成跟进序列（4 封邮件），language: zh / en / de / es / ar ...（多语种）；variant: A / B"""
    from utils.i18n import LANGUAGE_NAMES
    language = (language or "en").lower()
    templates = _ZH_TEMPLATES if language == "zh" else _EN_TEMPLATES

    try:
        llm = get_llm()
        vhint = ("Variant B (A/B test): use a short, curiosity-driven subject under 50 chars, "
                 "lead with a bold claim, max 70 words per email.") if variant == "B" else \
                ("Variant A: professional, relationship-focused, standard structure.")
        if language == "zh":
            prompt = f"""你是一名外贸销售邮件写作专家（中文）。
为 {company} 的 {product} 生成 4 封邮件（中文）：首封/ D+3 / D+7 / D+14。
{vhint}
额外信息：{extra}
输出 JSON：{{"email1":"...","email2":"...","email3":"...","email4":"..."}}"""
        else:
            lang_name = LANGUAGE_NAMES.get(language, "English")
            prompt = f"""You are a professional international trade (B2B) sales email copywriter.
Write a 4-email follow-up sequence in {lang_name} to {company} about our {product}.
Email steps: Day0 introduction, Day3 follow-up, Day7, Day14 final notice.
{vhint}
Write the email body in {lang_name} (subject line may keep English keywords).
CRITICAL format per email:
  Subject: <subject line>
  Dear Sir/Madam,
  <2-3 short paragraphs, value-driven, max 80 words>
  <Clear Call-to-Action>
  Best regards, [Your Name] / [Your Company]
Extra context: {extra}
Return ONLY valid JSON: {{"email1":"...","email2":"...","email3":"...","email4":"..."}}"""
        result = llm.invoke(prompt)
        m = re.search(r"\{.*\}", result.content, re.S)
        if m:
            seq = json.loads(m.group())
            # 补齐缺失步骤（修复：回退模板必须先 format，否则发出含 {company}/{product} 占位符的字面量）
            for i in range(1, 5):
                if f"email{i}" not in seq:
                    seq[f"email{i}"] = templates.get(
                        f"email{min(i,3)}", "").format(company=company, product=product)
            return seq
    except Exception as e:
        logger.debug(f"LLM 跟进序列失败（{language}/{variant}），用模板: {e}")

    seq = {key: tmpl.format(company=company, product=product)
           for key, tmpl in templates.items()}
    # 模板只到 email3，补第4封（与 email3 同内容，提示最终）——同样需 format
    seq.setdefault("email4", templates["email3"].format(company=company, product=product))
    return seq


def _inject_tracking(body: str, track_id: str, language: str) -> tuple:
    """返回 (最终body, 是否html)。注入打开追踪像素 + 退订链接"""
    if not track_id:
        return body, False
    pixel = f"{APP_PUBLIC_URL}/leads/track/{track_id}.png"
    unsub = f"{APP_PUBLIC_URL}/leads/unsubscribe/{track_id}"
    if language == "en":
        footer = (f"\n\n----------------------------------------\n"
                  f"<img src=\"{pixel}\" width=\"1\" height=\"1\" alt=\"\" />\n"
                  f"If you'd prefer not to receive these emails, "
                  f"<a href=\"{unsub}\">unsubscribe here</a>.")
    else:
        footer = (f"\n\n----------------------------------------\n"
                  f"<img src=\"{pixel}\" width=\"1\" height=\"1\" alt=\"\" />\n"
                  f"如不想再收到此类邮件，请<a href=\"{unsub}\">点击退订</a>。")
    html_body = (_html.escape(body).replace("\n", "<br/>") +
                 "<br/>" + footer.replace("\n", ""))
    return html_body, True


def send_follow_up(lead_id: int, company_id: int, step: int = 1,
                   attachments: list = None, language: str = "zh",
                   variant: str = "A", track_id: str = "") -> dict:
    """发送第 N 步跟进邮件（step: 1~4），自动带报价 PDF 附件 + 追踪像素"""
    from database import SessionLocal
    from models import Lead, Quotation
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id,
                                     Lead.company_id == company_id).first()
        if not lead:
            return {"error": "线索不存在"}
        company_name = lead.company_name or "我方"
        product = (lead.enriched or {}).get("products")
        if isinstance(product, list) and product:
            product = product[0]
        product = product or lead.industry or "industrial product"

        if step < 1 or step > MAX_STEP + 1:
            return {"error": f"step 须为 1~{MAX_STEP + 1}"}
        seq = build_sequence(company_name, product, "", language=language, variant=variant)
        content = seq.get(f"email{step}", seq.get("email1", ""))

        subject = subject_for(step, language, product, company_name, variant)

        # 报价 PDF 附件（仅首封）——安全修复：只附与线索企业名匹配的报价单（此前取企业最新报价，会跨客户泄露 PDF）
        atts = list(attachments or [])
        if step == 1 and lead.company_name:
            q = db.query(Quotation).filter(
                Quotation.company_id == company_id,
                Quotation.customer_name == lead.company_name,
            ).order_by(Quotation.id.desc()).first()
            if q and getattr(q, "pdf_path", None):
                atts.append((q.pdf_path, f"quotation_{q.quotation_no}.pdf"))

        # 英文模板正文含 Subject 行，剥离（已作为主题）
        body = content
        if language == "en" and body.startswith("Subject:"):
            body = "\n".join(body.split("\n")[1:]).lstrip()

        tid = track_id or lead.track_id or ""
        final_body, is_html = _inject_tracking(body, tid, language)

        # 修复：空邮箱判断前置（此前先调 send_email 再判断，真实 SMTP 下空收件人直接报错）
        if not lead.email:
            result = {"status": "simulated", "note": "无邮箱，生成内容待人工触达",
                      "subject": subject}
        else:
            result = send_email(lead.email, final_body, subject=subject,
                                html=is_html, company_id=company_id, attachments=atts)

        # 记录跟进进度
        lead.follow_up_step = step
        lead.follow_up_at = datetime.utcnow()
        lead.variant = variant
        if lead.status == "discovered":
            lead.status = "contacted"
        lead.last_error = result.get("error", "") if result.get("status") == "error" else ""
        db.commit()
        return {**result, "step": step, "subject": subject,
                "language": language, "variant": variant}
    finally:
        db.close()


def run_due_follow_ups(company_id: int = None) -> dict:
    """扫描线索，对到期的发下一封跟进。

    company_id 为空时扫描全部企业（供 Celery 定时任务使用）；
    由企业 API 手动触发时传 company_id，实现租户隔离（修复跨企业发信副作用）。
    """
    from database import SessionLocal
    from models import Lead
    db = SessionLocal()
    sent = []
    try:
        query = db.query(Lead)
        if company_id is not None:
            query = query.filter(Lead.company_id == company_id)
        else:
            query = query.filter(Lead.company_id.isnot(None))
        now = datetime.utcnow()
        # 单轮发送上限（防历史积压批量爆发；可配置 EMAIL_MAX_PER_RUN）
        max_per_run = int(os.getenv("EMAIL_MAX_PER_RUN", "200"))
        # 性能优化：分页扫描（不全表加载），每批处理完释放
        batch_size = int(os.getenv("EMAIL_SCAN_BATCH", "500"))
        page = 0
        while len(sent) < max_per_run:
            leads = query.order_by(Lead.id)\
                .offset(page * batch_size).limit(batch_size).all()
            if not leads:
                break
            page += 1
            for lead in leads:
                if len(sent) >= max_per_run:
                    break
                if lead.status in _STOP_STATUSES:
                    continue
                step = getattr(lead, "follow_up_step", 0) or 0
                # 修复：允许第 4 封（step=3 -> email4）；step=4 表示序列已完成
                if step >= len(SEQUENCE_INTERVALS):
                    continue
                # 修复：到期时间以序列起点（created_at）锚定 INTERVALS（D0/D3/D7/D14）。
                # 下标用 SEQUENCE_INTERVALS[step]（step=1→3、2→7、3→14）；
                # 此前 [step+1] 错位且 step=3 时越界（IndexError），导致第 4 封永不发送。
                start = lead.created_at or now
                if step == 0:
                    due = timedelta(0)  # 首封立即
                else:
                    due = timedelta(days=SEQUENCE_INTERVALS[step])
                if now - start >= due:
                    try:
                        # 多语种：按线索国家路由沟通语言（替代硬编码 en）
                        from utils.trade import COUNTRY_LANGUAGES
                        lang = COUNTRY_LANGUAGES.get((lead.country or "").upper(), "en")
                        r = send_follow_up(lead.id, lead.company_id, step=step + 1,
                                           language=lang, variant=lead.variant or "A",
                                           track_id=lead.track_id or "")
                        sent.append({"lead": lead.id, "email": lead.email, **r})
                    except Exception as e:
                        logger.error(f"跟进失败 lead={lead.id}: {e}")
        return {"sent": sent, "count": len(sent)}
    finally:
        db.close()
