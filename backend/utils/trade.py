"""
utils/trade.py —— 外贸工具（出口型制造业 AI 应用 第1项：外贸报价智能体升级）

提供：
- get_fx_rate: 汇率换算（frankfurter.app 免费 ECB 汇率 API + 内存缓存 + 演示回退）
- classify_hs_code: HS Code 归类（内置制造业常见品类规则表 + 关键词匹配）
- estimate_freight: 海运费估算（按目的地区域 × 体积/重量规则）
- estimate_tariff: 关税估算（按 HS 品类 × 目的国规则，演示税率需按实际核验）
- COUNTRY_LANGUAGES: 国家 → 沟通语言映射（供多语种全链路使用）

说明：运费/关税为「估算辅助」，正式报价前必须人工核验（HS Code 以海关确认为准）。
"""
import logging
import time
import threading

logger = logging.getLogger(__name__)

# ---------- 汇率 ----------
_fx_cache: dict = {}
_fx_lock = threading.Lock()
_FX_TTL = 3600  # 1 小时缓存

# 演示回退汇率（真实汇率获取失败时使用；source="demo" 明确标注）
_DEMO_FX = {
    "USD": 1.0, "EUR": 0.92, "CNY": 7.25, "JPY": 157.0,
    "GBP": 0.79, "AUD": 1.52, "CAD": 1.37, "HKD": 7.82,
}


def get_fx_rate(from_currency: str = "USD", to_currency: str = "USD",
                use_cache: bool = True) -> dict:
    """获取汇率（1 from_currency = ? to_currency），返回 {rate, source, updated_at}"""
    from_currency = (from_currency or "USD").upper()
    to_currency = (to_currency or "USD").upper()
    if from_currency == to_currency:
        return {"rate": 1.0, "source": "identity", "updated_at": time.time()}
    key = f"{from_currency}:{to_currency}"

    with _fx_lock:
        cached = _fx_cache.get(key)
        if use_cache and cached and time.time() - cached["ts"] < _FX_TTL:
            return {"rate": cached["rate"], "source": cached["source"], "updated_at": cached["ts"]}

    # 真实汇率（frankfurter.app，ECB 数据，免费无 key）
    rate, source = None, "demo"
    try:
        import requests
        resp = requests.get(
            f"https://api.frankfurter.app/latest",
            params={"from": from_currency, "to": to_currency},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "rates" in data and to_currency in data["rates"]:
                rate = float(data["rates"][to_currency])
                source = "frankfurter(ECB)"
    except Exception as e:
        logger.debug("汇率 API 失败（%s），使用演示汇率", e)

    if rate is None:
        # 演示回退：经 USD 换算（USD 基准表）
        f = _DEMO_FX.get(from_currency)
        t = _DEMO_FX.get(to_currency)
        if f and t:
            rate = t / f
        else:
            rate = 1.0

    with _fx_lock:
        _fx_cache[key] = {"rate": rate, "source": source, "ts": time.time()}
    return {"rate": rate, "source": source, "updated_at": time.time()}


# ---------- HS Code 归类（制造业常见品类规则表） ----------
# 规则：关键词 → (HS Code 前 6 位, 描述)。命中后可由人工核验确认。
_HS_RULES = [
    (["玻璃烧杯", "烧杯", "量筒", "容量瓶", "试管", "培养皿", "玻璃器皿", "实验玻璃"],
     "7017", "Laboratory, hygienic or pharmaceutical glassware"),
    (["实验室仪器", "实验仪器", "测试仪器", "检测仪器"],
     "9027", "Instruments for physical or chemical analysis"),
    (["泵"], "8413", "Pumps for liquids"),
    (["阀门", "球阀", "闸阀"], "8481", "Taps, cocks, valves and similar appliances"),
    (["电机", "电动机"], "8501", "Electric motors and generators"),
    (["轴承", "滚珠轴承"], "8482", "Ball or roller bearings"),
    (["五金", "紧固件", "螺丝", "螺母", "螺栓"], "7318", "Screws, bolts, nuts and similar articles of iron/steel"),
    (["塑料制品", "塑料件", "注塑"], "3926", "Other articles of plastics"),
    (["橡胶件", "橡胶制品", "密封圈"], "4016", "Other articles of vulcanised rubber"),
    (["铝件", "铝合金", "铝型材"], "7606", "Aluminium plates, sheets and strip"),
    (["不锈钢", "不锈钢件"], "7219", "Flat-rolled products of stainless steel"),
    (["包装", "纸箱", "包装盒"], "4819", "Cartons, boxes, cases of paper/corrugated paper"),
    (["灯具", "照明", "LED灯"], "9405", "Luminaires and lighting fittings"),
    (["电线", "电缆"], "8544", "Insulated wire, cable and other conductors"),
    (["电子产品", "电路板", "PCB"], "8534", "Printed circuits"),
    (["传感器"], "9031", "Measuring or checking instruments"),
]


def classify_hs_code(product_desc: str) -> dict:
    """按产品描述做 HS Code 归类（关键词规则），返回 {hs_code, description, source}"""
    desc = (product_desc or "").lower()
    for keywords, code, label in _HS_RULES:
        if any(k.lower() in desc for k in keywords):
            return {"hs_code": code, "description": label, "source": "rule"}
    return {"hs_code": "", "description": "需人工核验归类", "source": "unclassified"}


# ---------- 海运费估算（演示规则：按目的地区域 × 体积） ----------
# 每立方米基准运费（USD），实际按航线/船司/季节浮动
_FREIGHT_RATE_PER_CBM = {
    "EU": 320.0, "NA": 380.0, "ME": 260.0, "SEA": 180.0,
    "SA": 420.0, "AF": 360.0, "OC": 450.0, "ASIA": 150.0,
}

_REGION_BY_COUNTRY = {
    # 欧洲
    "DE": "EU", "FR": "EU", "GB": "EU", "IT": "EU", "ES": "EU",
    "NL": "EU", "PL": "EU", "BE": "EU", "SE": "EU", "CH": "EU",
    # 北美
    "US": "NA", "CA": "NA", "MX": "NA",
    # 中东
    "AE": "ME", "SA": "ME", "IL": "ME", "TR": "ME",
    # 东南亚
    "SG": "SEA", "MY": "SEA", "TH": "SEA", "VN": "SEA", "ID": "SEA", "PH": "SEA",
    # 南美
    "BR": "SA", "AR": "SA", "CL": "SA", "PE": "SA",
    # 非洲
    "ZA": "AF", "EG": "AF", "NG": "AF", "KE": "AF",
    # 大洋洲
    "AU": "OC", "NZ": "OC",
}


def estimate_freight(volume_cbm: float = 1.0, weight_kg: float = 0.0,
                     destination_country: str = "US") -> dict:
    """海运费估算（USD）：按体积计费为主（<12CBM 走拼箱），返回 {freight_usd, basis, source}"""
    region = _REGION_BY_COUNTRY.get((destination_country or "").upper(), "ASIA")
    rate = _FREIGHT_RATE_PER_CBM.get(region, 180.0)
    vol = max(volume_cbm or 0, (weight_kg or 0) / 500.0)  # 体积重按 1CBM=500kg 折算
    freight = round(vol * rate, 2)
    return {"freight_usd": freight, "basis": f"{region}@{rate:.0f}/CBM", "source": "rule-estimate"}


# ---------- 关税估算（演示规则：按 HS 前 2 位品类 × 目的国，需人工核验） ----------
# 默认关税率（%）；未命中用 0，需核验
_DEFAULT_TARIFF = 0.0
_TARIFF_BY_HS2_AND_REGION = {
    "70": {"EU": 6.0, "US": 5.0, "ME": 8.0, "ASIA": 6.0},   # 玻璃制品
    "90": {"EU": 4.0, "US": 3.5, "ME": 5.0, "ASIA": 5.0},  # 仪器
    "84": {"EU": 3.0, "US": 2.5, "ME": 5.0, "ASIA": 5.0},  # 机械
    "85": {"EU": 3.0, "US": 2.5, "ME": 5.0, "ASIA": 5.0},  # 电气
    "73": {"EU": 2.7, "US": 2.5, "ME": 5.0, "ASIA": 6.0},  # 钢铁制品
    "39": {"EU": 6.5, "US": 5.3, "ME": 8.0, "ASIA": 8.0},  # 塑料
    "94": {"EU": 3.0, "US": 2.9, "ME": 5.0, "ASIA": 6.0},  # 灯具家具
}


def estimate_tariff(hs_code: str = "", destination_country: str = "US") -> dict:
    """关税估算（%）：按 HS 前 2 位 × 目的地区域规则，返回 {tariff_pct, basis, source}"""
    region = _REGION_BY_COUNTRY.get((destination_country or "").upper(), "ASIA")
    hs2 = (hs_code or "")[:2]
    pct = _DEFAULT_TARIFF
    if hs2:
        pct = _TARIFF_BY_HS2_AND_REGION.get(hs2, {}).get(region, _DEFAULT_TARIFF)
    return {"tariff_pct": pct, "basis": f"HS{hs2 or '?'}→{region}", "source": "rule-estimate"}


# ---------- 原材料价格（第5项：汇率/原材料风险助手） ----------
# 演示价格数据（生产可接 LME / 行业资讯 API）；trend_pct 为近期变动方向
_COMMODITY_PRICES = {
    "glass(borosilicate)": {"price": 3.2, "unit": "USD/kg", "trend_pct": 1.2},
    "copper": {"price": 9.8, "unit": "USD/kg", "trend_pct": -0.5},
    "aluminum": {"price": 2.6, "unit": "USD/kg", "trend_pct": 0.8},
    "plastic(PP)": {"price": 1.1, "unit": "USD/kg", "trend_pct": -0.3},
}


def get_commodity_prices() -> dict:
    """原材料价格快照（演示数据，生产接真实行情源）"""
    return {k: {**v, "source": "demo"} for k, v in _COMMODITY_PRICES.items()}


# ---------- 国家 → 沟通语言映射（多语种全链路 第2项） ----------
COUNTRY_LANGUAGES = {
    # 欧洲
    "DE": "de", "AT": "de", "CH": "de", "FR": "fr", "BE": "fr",
    "ES": "es", "IT": "it", "PT": "pt", "NL": "nl", "PL": "pl",
    "GB": "en", "IE": "en", "SE": "en", "DK": "en", "NO": "en",
    "RU": "ru", "UA": "uk",
    # 中东
    "AE": "ar", "SA": "ar", "QA": "ar", "KW": "ar", "IL": "he", "TR": "tr",
    # 亚洲
    "JP": "ja", "KR": "ko", "CN": "zh", "TW": "zh", "HK": "zh",
    "VN": "vi", "TH": "th", "ID": "id", "MY": "ms", "PH": "en",
    "IN": "en", "SG": "en",
    # 美洲
    "US": "en", "CA": "en", "MX": "es", "BR": "pt", "AR": "es",
    "CL": "es", "PE": "es", "CO": "es",
    # 非洲/大洋洲
    "ZA": "en", "EG": "ar", "AU": "en", "NZ": "en",
}
