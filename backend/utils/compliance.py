"""
utils/compliance.py —— 出口合规助手（出口型制造业 AI 应用 第4项）

提供：
- check_lead_compliance: 对客户（国家 + 公司名/联系人）做出口合规快速筛查：
  制裁国家/地区、实体关键词、认证要求提示、原产地提示
- 数据说明：内置名单为**示例规则**（教学/演示用），生产环境必须接入官方数据源
  （OFAC SDN / EU 制裁名单 / 商务部两用物项清单）并定期更新。

合规判断是高风险事项：本工具只做"快速筛查与提示"，正式交易前必须由人工/法务核验。
"""
import logging

logger = logging.getLogger(__name__)

# 示例制裁/高风险国家/地区（生产需接官方名单）
_SANCTION_COUNTRIES = {
    "IR", "KP", "SY", "CU", "BY", "MM", "RU", "SD", "VE",
}

# 示例敏感实体关键词（生产需接 OFAC SDN / EU 名单）
_SENSITIVE_KEYWORDS = [
    "defense", "military", "nuclear", "weapon", "missile",
    "sanctioned", "embargo", "terror", "dual-use",
]

# 需特殊认证提示的品类关键词 → 认证
_CERT_REQUIREMENTS = [
    (["电子", "电气", "电器", "electronic", "electrical"], "CE / RoHS（欧盟电子电气）"),
    (["医疗", "医疗器械", "medical"], "CE-MDR / FDA（医疗）"),
    (["食品", "食品接触", "food"], "FDA / LFGB / 食品接触认证"),
    (["玩具", "toy"], "EN71 / CPSIA（玩具）"),
    (["玻璃", "glass", "实验室", "laboratory"], "ISO 9001 / 客户指定（玻璃器皿一般无强制认证，按合同）"),
]


def check_lead_compliance(company_name: str = "", country: str = "",
                          contact: str = "") -> dict:
    """对客户做出口合规快速筛查，返回 {status, reasons, cert_hint}"""
    country = (country or "").upper()
    name = (company_name or "") + " " + (contact or "")
    name_l = name.lower()
    reasons = []

    # 1) 国家/地区筛查
    if country in _SANCTION_COUNTRIES:
        reasons.append(f"目的国 {country} 在示例制裁/高风险名单中（需人工核验后决定是否继续）")

    # 2) 实体/关键词筛查
    for kw in _SENSITIVE_KEYWORDS:
        if kw in name_l:
            reasons.append(f"客户名称/联系人含敏感关键词「{kw}」（需人工核验）")
            break

    # 3) 认证要求提示
    cert_hint = ""
    for kws, cert in _CERT_REQUIREMENTS:
        if any(k in name_l for k in kws):
            cert_hint = cert
            break

    status = "flagged" if reasons else "clean"
    return {
        "status": status,
        "reasons": reasons,
        "cert_hint": cert_hint,
        "source": "rule-example",  # 标注：示例规则，生产需接官方名单
        "note": "示例规则筛查，正式交易前需人工/法务核验",
    }
