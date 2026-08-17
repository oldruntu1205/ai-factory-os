"""
hermes/evolution/extractor.py —— 技能自动提取（Phase 1 落点，Phase 0 一并落地）

思路（类 Hermes GEPA 的"技能自动沉淀"）：
  1. 每次 Agent 任务落库后，按 (企业, Agent, 任务签名) 归类；
  2. 同类任务重复达到阈值（默认 5 次）且尚无技能 → 调 LLM 把工作流提炼成 SKILL.md；
  3. 已存在技能 → 仅累加 usage_count（"越用越强"的计数信号）；
  4. 无 API Key（Mock 模式）或 LLM 失败 → 退化为模板技能，保证 demo 链路可跑。

注意：本模块只"读"轨迹、"写"技能，不直接改 Agent 行为；GEPA 进化引擎（Phase 2/3）会消费这些技能。
"""
import hashlib
import logging
import time
from typing import Optional

from hermes.evolution import store

logger = logging.getLogger(__name__)

# 同类任务重复多少次后触发技能提取
SKILL_MIN_REPEAT = 5
# 只看最近 N 秒的轨迹（默认 30 天）
SKILL_LOOKBACK_SEC = 30 * 24 * 3600

# 制造域任务签名关键词（命中即归到固定签名，否则按输入哈希散列）
_SIGNATURE_KEYWORDS = [
    ("quality_defect_analysis", ["不良率", "defect", "缺陷", "裂纹", "气泡", "客诉", "质检", "质量"]),
    ("production_scheduling_analysis", ["排产", "产能", "延期", "工单", "产线", "瓶颈", "生产计划", "mes"]),
    ("quotation_analysis", ["报价", "quotation", "单价", "交期"]),
    ("lead_analysis", ["线索", "lead", "客户画像", "评分"]),
]


def classify(agent_name: str, task_input: str) -> str:
    """把一次任务归类为一个稳定签名（用于跨会话聚合）。"""
    text = (task_input or "").lower()
    for sig, kws in _SIGNATURE_KEYWORDS:
        if any(kw.lower() in text for kw in kws):
            return sig
    # 兜底：用 (agent + 输入前 40 字) 哈希，避免无关任务被误聚
    raw = f"{agent_name}:{(task_input or '')[:40]}"
    return "gen_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _generate_skill_content(agent_name: str, signature: str,
                             samples: list) -> tuple:
    """返回 (title, trigger, workflow_md, level0)。优先用 LLM，失败用模板。"""
    try:
        from ai.llm import get_llm, is_mock
        if not is_mock():
            llm = get_llm()
            sample_block = "\n---\n".join(
                f"输入: {t.task_input[:300]}\n输出: {t.final_output[:500]}"
                for t in samples[:5]
            )
            prompt = (
                f"你是制造业 AI 平台的技能提炼器。下面是一批 [{agent_name}] 员工重复执行的任务样本，"
                f"请把其通用工作流提炼成一个可复用 SKILL（遵循 agentskills.io）。\n\n"
                f"任务签名: {signature}\n\n样本:\n{sample_block}\n\n"
                "请严格按以下格式输出（中文）：\n"
                "# 技能标题\n"
                "## Trigger\n<何时自动匹配该技能>\n"
                "## Workflow\n<编号步骤：先查什么数据/调什么工具/再怎么分析>\n"
                "## Level0\n<一句话快速预览，不超过 60 字>"
            )
            out = llm.invoke(prompt).content
            return _parse_llm_skill(out, agent_name, signature)
    except Exception as e:
        logger.warning("技能提取 LLM 失败，回退模板: %s", e)

    return _template_skill(agent_name, signature, samples)


def _parse_llm_skill(text: str, agent_name: str, signature: str) -> tuple:
    """粗略解析 LLM 输出为 (title, trigger, workflow_md, level0)。"""
    title = f"{agent_name} · {signature}"
    trigger, workflow, level0 = "", text, ""
    try:
        import re
        t = re.search(r"##\s*Trigger\s*(.+?)(?=##\s*Workflow|\Z)", text, re.S)
        w = re.search(r"##\s*Workflow\s*(.+?)(?=##\s*Level0|\Z)", text, re.S)
        l0 = re.search(r"##\s*Level0\s*(.+?)(?=\Z)", text, re.S)
        h = re.search(r"#\s*(.+)", text)
        if h:
            title = h.group(1).strip()
        if t:
            trigger = t.group(1).strip()
        if w:
            workflow = w.group(1).strip()
        if l0:
            level0 = l0.group(1).strip()
    except Exception:
        pass
    return title, trigger, workflow, level0


def _template_skill(agent_name: str, signature: str, samples: list) -> tuple:
    """无 LLM 时的模板技能（保证 demo 可跑）。"""
    title = f"{agent_name} 复用技能 · {signature}"
    trigger = f"当任务属于 [{signature}] 类型时自动匹配"
    last = samples[0].final_output[:400] if samples else ""
    workflow = (
        f"# {title}\n\n"
        f"## Trigger\n{trigger}\n\n"
        f"## Workflow\n"
        f"1. 读取本次任务输入与历史轨迹\n"
        f"2. 复用过往同类项的分析框架（已有 {len(samples)} 次执行记录）\n"
        f"3. 输出结构化结论（异常/根因/建议）\n\n"
        f"## 近期样本结论节选\n{last}"
    )
    level0 = f"[{signature}] 复用既往 {len(samples)} 次执行经验"
    return title, trigger, workflow, level0


def extract_after_trace(company_id: int, agent_name: str,
                        signature: str, task_input: str,
                        final_output: str) -> Optional[dict]:
    """runtime 落库轨迹后调用：判定是否沉淀/复用技能。返回新技能记录或 None。"""
    try:
        # 1) 已有技能 → 仅累加使用次数
        existing = store.get_skill_by_key(company_id, agent_name, signature)
        if existing:
            store.increment_skill_usage(company_id, agent_name, signature)
            return None

        # 2) 同类任务是否达到触发阈值
        since = time.time() - SKILL_LOOKBACK_SEC
        count = store.count_traces_by_signature(company_id, agent_name, signature, since)
        if count < SKILL_MIN_REPEAT:
            return None

        # 3) 达标 → 提炼技能（打上当前 Soul 版本指纹，方案2）
        samples = store.get_recent_traces_for_signature(company_id, agent_name, signature)
        title, trigger, workflow, level0 = _generate_skill_content(
            agent_name, signature, samples)
        try:
            from hermes.registry.soul import soul_version
            sv = soul_version(company_id, agent_name)
        except Exception:
            sv = ""
        rec = store.upsert_skill(
            company_id=company_id, agent_name=agent_name, skill_key=signature,
            title=title, trigger=trigger, workflow_md=workflow, level0=level0,
            soul_version=sv)
        logger.info("技能自动沉淀: company=%s agent=%s skill=%s (样本=%d) soul=%s",
                    company_id, agent_name, signature, count, sv)
        return {"id": rec.id, "skill_key": signature, "title": title,
                "usage_count": rec.usage_count}
    except Exception as e:
        logger.warning("extract_after_trace 异常（不影响主流程）: %s", e)
        return None
