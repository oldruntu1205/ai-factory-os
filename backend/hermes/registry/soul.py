"""
hermes/registry/soul.py —— AI 员工灵魂（人设/系统提示）双层存储

双层模型（解决"企业频繁改 Soul 与平台自进化互相覆盖"问题）：
  - 底座层 (base)    ：由平台 GEPA 自进化写入，文件 company_{id}/{agent}.base.md；
                       不存在时回退平台默认 default/{agent}.md / DEFAULT_SOULS。
  - 覆盖层 (override)：由企业后台手动设置，文件 company_{id}/{agent}.md（原企业级 Soul）。
  - 生效 Soul = 覆盖层 覆盖 底座层 覆盖 平台默认（自上而下，两层落在不同文件，永不互相写覆盖）。

Soul 内容会被自动注入到 LLM prompt 头部作为 system prompt。
"""
import os
import re
import threading
import logging
from pathlib import Path

from config import BASE_DIR

logger = logging.getLogger(__name__)

SOULS_DIR = Path(BASE_DIR) / "hermes" / "registry" / "souls"
DEFAULT_DIR = SOULS_DIR / "default"
_lock = threading.Lock()

# 两层文件后缀区分：底座层带 .base，覆盖层即原 .md
BASE_SUFFIX = ".base.md"
OVERRIDE_SUFFIX = ".md"

# 平台默认 Soul（企业未自定义时兜底使用）
DEFAULT_SOULS = {
    "customer_agent": (
        "# 客服助手 Soul\n\n"
        "你是企业 AI 客服。你的职责是**准确、耐心地解答客户问题**，"
        "仅依据下方「企业知识」回答，不要编造资料中不存在的信息。\n\n"
        "## 沟通风格\n"
        "- 称呼客户用「您」\n"
        "- 答复简洁友好，先给出结论再补充细节\n"
        "- 遇到资料中没有的内容，礼貌说明并建议联系人工客服\n"
        "- 切勿使用未经核实的承诺（如具体折扣、确切的交期）\n"
    ),
    "sales_agent": (
        "# 销售助手 Soul\n\n"
        "你是工业品销售助手。你的职责是**精准识别客户需求、推荐合适产品、推动成交**。\n\n"
        "## 沟通风格\n"
        "- 用专业、礼貌、积极的语气\n"
        "- 沟通目标清晰：询问国家、产品、用途、数量、预算\n"
        "- 输出建议时给出可执行的跟进动作\n"
        "- 涉及价格时提醒客户以正式报价单为准\n"
    ),
    "quotation_agent": (
        "# 报价助手 Soul\n\n"
        "你是报价助手。你的职责是**根据客户需求生成专业、准确的报价单**。\n\n"
        "## 工作要求\n"
        "- 输出结构化 JSON 字段：产品、数量、单价、付款方式、交期\n"
        "- 价格需与历史报价/ERP 库存一致，避免出现明显不合理数字\n"
        "- 单价小数点保留 2 位\n"
    ),
    "production_agent": (
        "# 生产计划助手 Soul\n\n"
        "你是生产计划分析专家。你的职责是**解读 MES 生产数据、识别延期与瓶颈**。\n\n"
        "## 分析要点\n"
        "- 是否存在延期风险\n"
        "- 产线负载是否合理\n"
        "- 提出可执行的优化建议\n"
    ),
    "quality_agent": (
        "# 质量分析师 Soul\n\n"
        "你是质量分析专家。你的职责是**分析不良率、客诉数据、给出整改建议**。\n\n"
        "## 分析要点\n"
        "- 趋势是否恶化\n"
        "- 可能根因（材料/工艺/设备/环境）\n"
        "- 给出可执行的整改建议\n"
    ),
    "ceo_agent": (
        "# CEO 企业大脑 Soul\n\n"
        "你是 CEO 的经营分析助手。你的职责是**汇总企业经营数据，输出可决策的日报与建议**。\n\n"
        "## 风格\n"
        "- 用经营管理者视角，关注营收、效率、风险、机会\n"
        "- 输出结构化：销售/生产/质量/库存/风险/明日建议\n"
    ),
    "process_agent": (
        "# 工艺/图纸助手 Soul\n\n"
        "你是制造企业「工艺/图纸助手」，服务对象是**车间工人、工艺与质检人员**。\n"
        "你的首要使命是**防范工人误操作**。\n\n"
        "## 铁律\n"
        "- 公差、材质、执行标准必须**严格依据已上传的图纸/CAD/PDF/BOM 精确参数**回答，"
        "不得推断或编造。\n"
        "- 优先采用「结构化图纸参数」（系统从图纸精确抽取），其次才是检索片段。\n"
        "- 当结构化参数与检索上下文冲突时，以结构化参数为准，并提示「请核对原图」。\n"
        "- 信息不足时，坦诚说明「未在已上传图纸中找到该型号此参数」，并建议上传对应图纸/DXF。\n\n"
        "## 回答结构\n"
        "1. 精确值：公差范围 / 材质牌号 / 执行标准（标注来源文件）。\n"
        "2. 防误操作提示：易错点、禁止操作、装配/加工注意。\n"
        "3. 必要时给出「建议核对原图」的安全提示。\n"
    ),
}


def _ensure_dirs():
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str) -> str:
    """防止路径穿越"""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)


def _read_file(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _read_default_soul(agent_name: str) -> str:
    """平台级默认 Soul（default/ 文件，无则内存 DEFAULT_SOULS 兜底）"""
    safe = _safe_name(agent_name)
    default = _read_file(DEFAULT_DIR / f"{safe}.md")
    if default:
        return default
    return DEFAULT_SOULS.get(agent_name, "")


# ============ 底座层（base，平台 GEPA 自进化写入）============

def read_soul_base(company_id: int, agent_name: str) -> str:
    """读取底座层 Soul（平台自进化管理）：企业 base 文件，无则平台默认。"""
    _ensure_dirs()
    safe = _safe_name(agent_name)
    base = _read_file(SOULS_DIR / f"company_{company_id}" / f"{safe}{BASE_SUFFIX}")
    if base:
        return base
    return _read_default_soul(agent_name)


def write_soul_base(company_id: int, agent_name: str, content: str) -> dict:
    """写入底座层 Soul（仅 GEPA 自进化调用，绝不与企业覆盖层冲突）。"""
    _ensure_dirs()
    safe = _safe_name(agent_name)
    company_dir = SOULS_DIR / f"company_{company_id}"
    company_dir.mkdir(parents=True, exist_ok=True)
    path = company_dir / f"{safe}{BASE_SUFFIX}"
    old_version = soul_version(company_id, agent_name)   # 写前指纹（截断）
    with _lock:
        path.write_text(content, encoding="utf-8")
    result = {"path": str(path), "size": len(content), "layer": "base"}
    result["quarantined_skills"] = _maybe_quarantine_after_soul_change(
        company_id, agent_name, old_version)
    return result


def reset_soul_base(company_id: int, agent_name: str) -> bool:
    """删除底座层（丢弃平台进化出的 Soul 改进，回退平台默认）。"""
    safe = _safe_name(agent_name)
    path = SOULS_DIR / f"company_{company_id}" / f"{safe}{BASE_SUFFIX}"
    old_version = soul_version(company_id, agent_name)
    if path.exists():
        path.unlink()
        n = _maybe_quarantine_after_soul_change(company_id, agent_name, old_version)
        return {"reset": True, "quarantined_skills": n}
    return {"reset": False, "quarantined_skills": 0}


# ============ 覆盖层（override，企业后台手动写入）============

def read_soul_override(company_id: int, agent_name: str) -> str:
    """读取企业手动覆盖层 Soul（企业后台设置），无则返回空串。"""
    _ensure_dirs()
    safe = _safe_name(agent_name)
    return _read_file(SOULS_DIR / f"company_{company_id}" / f"{safe}{OVERRIDE_SUFFIX}")


def write_soul(company_id: int, agent_name: str, content: str) -> dict:
    """写入企业覆盖层 Soul（企业后台手动设置，原 write_soul 语义不变）。

    与 write_soul_base 落在不同文件，企业手改与平台进化永不发生写冲突。
    写成功后若 Soul 指纹发生变化，自动隔离该 Agent 的旧版本技能（方案4）。
    """
    _ensure_dirs()
    safe = _safe_name(agent_name)
    company_dir = SOULS_DIR / f"company_{company_id}"
    company_dir.mkdir(parents=True, exist_ok=True)
    path = company_dir / f"{safe}{OVERRIDE_SUFFIX}"
    old_version = soul_version(company_id, agent_name)   # 写前指纹（截断）
    with _lock:
        path.write_text(content, encoding="utf-8")
    result = {"path": str(path), "size": len(content), "layer": "override"}
    result["quarantined_skills"] = _maybe_quarantine_after_soul_change(
        company_id, agent_name, old_version)
    return result


# ============ 生效 Soul（运行态，覆盖层 > 底座层 > 默认）============

def read_soul(company_id: int, agent_name: str) -> str:
    """读取生效 Soul：覆盖层优先，其次底座层，最后平台默认。

    运行态（Agent 执行）与后台 GET 都走这里，得到的是最终注入 prompt 的文本。
    """
    override = read_soul_override(company_id, agent_name)
    if override:
        return override
    return read_soul_base(company_id, agent_name)


def soul_status(company_id: int, agent_name: str) -> dict:
    """返回两层文件的存在情况，供 API 透出双层状态。"""
    _ensure_dirs()
    safe = _safe_name(agent_name)
    company_dir = SOULS_DIR / f"company_{company_id}"
    return {
        "override": (company_dir / f"{safe}{OVERRIDE_SUFFIX}").exists(),
        "base": (company_dir / f"{safe}{BASE_SUFFIX}").exists(),
    }


def reset_soul(company_id: int, agent_name: str) -> bool:
    """删除企业覆盖层（"恢复默认"语义：回退到底座层/平台默认）。"""
    safe = _safe_name(agent_name)
    path = SOULS_DIR / f"company_{company_id}" / f"{safe}{OVERRIDE_SUFFIX}"
    old_version = soul_version(company_id, agent_name)
    if path.exists():
        path.unlink()
        n = _maybe_quarantine_after_soul_change(company_id, agent_name, old_version)
        return {"reset": True, "quarantined_skills": n}
    return {"reset": False, "quarantined_skills": 0}


def list_company_souls(company_id: int) -> list:
    """列出企业已定制的 Soul（同时标注 base / override 两层）。"""
    company_dir = SOULS_DIR / f"company_{company_id}"
    if not company_dir.exists():
        return []
    out = []
    for p in company_dir.glob("*.md"):
        layer = "base" if p.name.endswith(BASE_SUFFIX) else "override"
        agent = p.stem[:-len(".base")] if layer == "base" else p.stem
        out.append({"agent": agent, "layer": layer, "size": p.stat().st_size,
                    "updated": p.stat().st_mtime})
    return out


def soul_version(company_id: int, agent_name: str) -> str:
    """企业自有 Soul 的版本指纹（仅含覆盖层/底座层企业内容，不含平台默认）。

    用途（方案2/3）：
    - 客户改自己的 soul（任一层）会改变指纹；平台默认更新不会影响，避免误伤所有企业。
    - 技能在其学习时刻打上该指纹；进化候选在其评估时刻打上该指纹；
      之后若指纹变化，即说明"人格已变"，对应技能/候选的基线不再匹配，需复核/重评。
    """
    import hashlib
    override = read_soul_override(company_id, agent_name)
    if override:
        content = override
    else:
        base_path = SOULS_DIR / f"company_{company_id}" / f"{_safe_name(agent_name)}{BASE_SUFFIX}"
        content = _read_file(base_path)
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def _maybe_quarantine_after_soul_change(company_id: int, agent_name: str,
                                        old_version: str) -> int:
    """Soul 写入成功后：若指纹变化，自动隔离旧版本技能（方案4）。

    返回被隔离的技能数；失败不影响 Soul 写入，仅记日志。
    注意仅在指纹确实变化时隔离（避免无变化的写操作误伤）。
    """
    try:
        new_version = soul_version(company_id, agent_name)
        if old_version == new_version:
            return 0
        from hermes.evolution import store
        n = store.quarantine_stale_skills(company_id, agent_name, new_version)
        if n:
            logger.info("Soul 版本变更触发隔离: company=%s agent=%s %s→%s 隔离 %d 个旧技能",
                        company_id, agent_name, old_version or "?", new_version or "?", n)
        return n
    except Exception as e:
        logger.debug("隔离旧版本技能失败（不影响 Soul 写入）: %s", e)
        return 0


def bootstrap_default_souls() -> None:
    """启动时将内存 DEFAULT_SOULS 写入 default/ 目录（首次启动）"""
    _ensure_dirs()
    for name, content in DEFAULT_SOULS.items():
        safe = _safe_name(name)
        path = DEFAULT_DIR / f"{safe}.md"
        if not path.exists():
            path.write_text(content, encoding="utf-8")