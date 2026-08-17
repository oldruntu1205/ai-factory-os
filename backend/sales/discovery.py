"""
sales/discovery.py —— 客户发现 Agent（V2.1 第四章，升级版）

支持：
- 关键词 / 国家(多) / 行业(多) 筛选
- 可插拔数据源：MockProvider（演示，丰富可配置） / SearchProvider（真实搜索 API 占位）
- 发现后写入 leads 表并 enrichment（员工规模 / 进口史 / 主营产品 / 社媒占位）
- 自动调用 score_lead 完成评分（LLM 语义评分，失败回退规则）
"""
import random
import uuid

from ai.llm import get_llm
from models import Lead
from sales.scoring import score_lead
from database import SessionLocal


# ========== 真实数据源占位（接入真实搜索/海关/B2B API 时实现）==========
class SearchProvider:
    """
    真实获客数据源（占位）

    接入建议（按效果/成本排序）：
    1. Google Custom Search / Bing Web Search：关键词 + 国家搜公司官网
    2. ImportGenius / Panjiva / 海关数据：按 HS 编码查进口商名单
    3. Hunter.io / Apollo / ZoomInfo：按域名补全决策人邮箱
    4. 官站抓取 + LLM 提炼：行业 / 规模 / 主营产品 / 社媒

    返回结构须与 MockProvider.generate 一致：list[dict]，每条含
    company_name / website / country / email / industry / enriched。
    """
    def __init__(self, api_key: str = "", engine: str = "google"):
        self.api_key = api_key
        self.engine = engine

    def search(self, keywords, countries, industries, limit):
        raise NotImplementedError("请在 SearchProvider.search 接入真实搜索 API")
        # 伪代码示例：
        # raw = google_search(q=keywords, gl=country_code, num=limit, key=self.api_key)
        # for item in raw:
        #     yield {"company_name": ..., "website": ..., "country": ..., ...}


# ========== 演示数据源（可配置、可重复）==========
class MockProvider:
    """基于关键词 / 国家 / 行业生成多样化、可重复的拟真企业（演示用）"""
    # 国家 -> (域名后缀, 公司名后缀池)
    _REGIONS = {
        "Germany": ("de", ["GmbH", "AG", "Chemie", "Labor"]),
        "United Kingdom": ("co.uk", ["Ltd", "Scientific", "Laboratories"]),
        "United States": ("com", ["Inc", "Labs", "Bio-Tech", "Corp"]),
        "France": ("fr", ["SAS", "Laboratoire", "Industries"]),
        "Sweden": ("se", ["AB", "Pharma", "Research"]),
        "Japan": ("co.jp", ["K.K.", "Analytical", "Instruments"]),
        "Italy": ("it", ["S.r.l.", "Strumenti", "Scientifica"]),
        "Australia": ("com.au", ["Pty Ltd", "Scientific", "Lab"]),
        "India": ("in", ["Pvt Ltd", "Laboratories", "Optics"]),
        "Brazil": ("com.br", ["Ltda", "Laboratório", "Científica"]),
        "Canada": ("ca", ["Inc", "Scientific", "Lab"]),
        "Netherlands": ("nl", ["B.V.", "Analytical", "Instruments"]),
    }
    _COUNTRY_POOL = list(_REGIONS.keys())

    _INDUSTRIES = [
        "实验室化学品", "科研仪器", "生物技术", "制药", "分析仪器",
        "玻璃制品", "医疗器械", "高校科研", "食品检测", "环境监测",
    ]

    _BRANDS = [
        "Alpha", "Meridian", "Vertex", "Nordic", "Pacific", "Helix",
        "Aurora", "Cobalt", "Summit", "Vantage", "Orion", "Quanta",
        "Azure", "Borealis", "Crest", "Delta",
    ]

    _PRODUCT_POOL = [
        "硼硅玻璃器皿", "石英管", "反应釜", "量筒", "培养皿",
        "冷凝管", "移液器", "滴定管", "色谱柱", "离心管",
    ]

    def __init__(self, seed: int = 0):
        self._rng = random.Random(seed)

    def generate(self, keywords, countries, industries, limit):
        countries = countries or self._COUNTRY_POOL
        industries = industries or self._INDUSTRIES
        core = (keywords or "Prime").strip().split()[0].title()
        out = []
        for _ in range(limit):
            country = self._rng.choice(countries)
            region = self._REGIONS.get(country, ("com", ["Inc", "Labs"]))
            tld, suffix_pool = region[0], region[1]
            brand = self._rng.choice(self._BRANDS)
            suffix = self._rng.choice(suffix_pool)
            company_name = f"{brand} {core} {suffix}".strip()
            website = f"{brand.lower()}{self._rng.randint(10, 99)}.{tld}"
            industry = self._rng.choice(industries)
            # 约 25% 缺邮箱 —— 测试"无邮箱人工触达"路径
            has_email = self._rng.random() > 0.25
            email = f"purchase@{website}" if has_email else ""
            employee_count = self._rng.choice([12, 35, 80, 150, 320, 600])
            has_import = country not in ("China", "印度")
            products = self._rng.sample(self._PRODUCT_POOL, 3)
            out.append({
                "company_name": company_name,
                "website": website,
                "country": country,
                "email": email,
                "industry": industry,
                "enriched": {
                    "employee_count": employee_count,
                    "has_import": bool(has_import),
                    "products": products,
                    "social": "",
                },
            })
        return out


def analyze_company(company_info: str) -> str:
    """LLM 分析目标客户价值（V2.1 第四章，可选）"""
    llm = get_llm()
    prompt = f"""
你是B2B工业销售专家。
分析目标客户：
{company_info}
判断：1) 是否适合采购 2) 采购规模 3) 推荐销售策略。
输出JSON。
"""
    try:
        return llm.invoke(prompt).content
    except Exception as e:
        return f"分析失败: {e}"


def discover_leads(company_id: int, keywords: str = "laboratory glassware",
                   countries=None, industries=None, limit: int = 20,
                   provider=None) -> list:
    """
    客户发现 + 自动评分（升级版）。
    - countries / industries 为列表，空表示不限
    - provider 缺省使用 MockProvider（按关键词做种子，结果可重复）
    """
    db = SessionLocal()
    created = []
    try:
        seed = abs(hash(keywords or "seed")) % 100000
        prov = provider or MockProvider(seed=seed)
        samples = prov.generate(keywords, countries, industries, limit)
        # 性能修复：批量 add + 末尾统一 commit（此前逐条 commit，量大时慢且部分失败留半批）
        pending = []
        for s in samples:
            lead = Lead(
                company_name=s["company_name"],
                website=s["website"],
                country=s["country"],
                email=s["email"],
                industry=s["industry"],
                status="discovered",
                followup_day=1,
                follow_up_step=0,
                source="discovery",
                enriched=s.get("enriched", {}),
                track_id=uuid.uuid4().hex[:16],
                company_id=company_id,
            )
            score_lead(lead)  # LLM 评分（失败回退规则）
            db.add(lead)
            pending.append(lead)
        db.commit()
        for lead in pending:
            db.refresh(lead)
            created.append({
                "id": lead.id, "company_name": lead.company_name,
                "country": lead.country, "score": lead.score,
                "grade": lead.grade, "email": lead.email,
                "industry": lead.industry,
            })
        return created
    finally:
        db.close()
