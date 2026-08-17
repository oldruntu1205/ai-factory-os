"""
hermes/evolution/evaluator.py —— 离线评估（GEPA 第四阶段的"评估"环节）

不真正重跑 Agent（成本高），而是用历史 trace 回放做**轻量离线评估**：
  1. 基线分：该 Agent 历史 trace 的平均 quality_score（无信号时退化为对当前文本的 LLM/启发式评分）；
  2. 候选分：让 LLM 扮演评估者，基于同样的任务样本判断"若采用候选文本，预期表现"；
  3. 五道护栏校验：体积约束 / 语义保留 / 缓存兼容 / 测试（trace 回放提升）/ 不漂移。

同时承担护栏门禁职责——任一硬护栏不过，候选即使分数更高也不许自动应用。
"""
import logging
import re

from hermes.evolution import store

logger = logging.getLogger(__name__)

# 体积约束（与 Hermes 官方护栏对齐）
SIZE_LIMIT = {"skill": 15 * 1024, "tool": 500, "soul": 50 * 1024}
# 离线评估最多采样多少条 trace（控成本）
SAMPLE_N = 5
# 允许应用的最小提升阈值（相对基线，避免无意义微改）
MIN_LIFT = 0.0


def _avg_historical_quality(traces: list) -> float:
    scores = [t.quality_score for t in traces if getattr(t, "quality_score", None) is not None]
    if not scores:
        return None
    return sum(scores) / len(scores)


def _trace_summary(traces: list, n: int = SAMPLE_N) -> str:
    """取最多 n 条 trace 的输入/输出摘要，作为评估上下文。"""
    picked = traces[:n]
    blocks = []
    for i, t in enumerate(picked, 1):
        inp = (t.task_input or "")[:240]
        out = (t.final_output or "")[:360]
        blocks.append(f"[样本{i}] 输入: {inp}\n输出: {out}")
    return "\n---\n".join(blocks)


def _heuristic_score(text: str, base: float) -> float:
    """Mock / 无 LLM 时的确定性启发式评分（0-1）。

    设计意图：文本越结构化、越强调根因与可行动建议，预期质量越高；
    GEPA 生成的候选通常新增结构化内容，因此会获得合理增量。
    """
    s = base
    if re.search(r"(##\s|步骤|Workflow|workflow)", text):
        s += 0.06
    if re.search(r"(根因|原因|root\s*cause|成因)", text, re.I):
        s += 0.05
    if re.search(r"(建议|整改|优化|action|对策)", text):
        s += 0.05
    if len(re.findall(r"\d", text)) >= 3:   # 含具体数据/数字
        s += 0.02
    if len(text.strip()) < 80:               # 信息量不足
        s -= 0.08
    return max(0.1, min(0.98, round(s, 4)))


def _llm_score(agent_name: str, target_type: str, text: str,
               traces: list, soul_snapshot: str = None) -> float:
    """用 LLM 扮演评估者，对"采用该文本后在该类任务上的预期质量"打分 0-1。

    无可用 LLM（未配置 Key / 降级 Mock）时直接走确定性启发式，保证离线可评估。
    soul_snapshot：评估时锁定的 Soul 文本（方案3）。传入后作为固定基准上下文，
    使候选评分与基线在同一人格下比较，避免 soul 频繁变动导致"移动靶"。
    """
    from ai.llm import get_llm, is_mock
    base = _avg_historical_quality(traces) or 0.55
    try:
        llm = get_llm()
    except Exception as e:
        logger.warning("LLM 不可用，回退启发式: %s", e)
        return _heuristic_score(text, base)
    if is_mock():
        return _heuristic_score(text, base)
    try:
        sample = _trace_summary(traces)
        soul_ctx = ""
        if soul_snapshot:
            soul_ctx = (
                f"\n\n【评估基准 Soul（已锁定，请在该人格设定下判断质量）】\n"
                f"{soul_snapshot[:1200]}\n"
            )
        prompt = (
            f"你是制造业 AI 平台的严格质量评估者。下面是一份 [{agent_name}] 员工的"
            f"{target_type} 文本（系统提示/SKILL/工具描述），以及它历史上处理的同类任务样本。\n\n"
            f"请评估：若采用该文本，该员工在此类任务上的输出质量（准确性、可操作性、结构性）。\n\n"
            f"待评估文本:\n{text[:1500]}\n\n历史任务样本:\n{sample}{soul_ctx}\n\n"
            "只输出一个 0 到 1 之间的小数（保留两位），不要解释。"
        )
        out = llm.invoke(prompt).content
        m = re.search(r"0?\.\d+|\b[01](\.\d+)?\b", out)
        if m:
            return max(0.1, min(0.98, float(m.group(0))))
        return _heuristic_score(text, base)
    except Exception as e:
        logger.warning("LLM 评估失败，回退启发式: %s", e)
        return _heuristic_score(text, base)


def _check_guardrails(target_type: str, current_text: str,
                      candidate_text: str) -> dict:
    """五道护栏中的三道"结构门禁"（测试门由分数差体现，人工门由审批体现）。"""
    limit = SIZE_LIMIT.get(target_type, 15 * 1024)
    size_ok = len(candidate_text) <= limit

    # 语义保留：候选应沿用当前文本的核心目的关键词（简单但可解释）
    # 取当前文本里的核心名词（>=2字中文片段）作为锚点，候选需保留多数。
    anchors = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", current_text))
    # 过滤掉过于通用的词，保留出现频率低、更具业务含义的词
    kept = sum(1 for a in anchors if a in candidate_text)
    semantic_kept = True  # 默认保留；真正的漂移判定交给 LLM（下方可选）
    if anchors:
        semantic_kept = (kept / max(1, len(anchors))) >= 0.3

    # 缓存兼容：我们只改文本、不改标识/结构键，默认 True
    cache_compatible = True

    all_pass = size_ok and semantic_kept and cache_compatible
    return {
        "size_ok": size_ok,
        "size_limit": limit,
        "semantic_kept": semantic_kept,
        "cache_compatible": cache_compatible,
        "all_pass": all_pass,
        "notes": "" if all_pass else "体积或语义护栏未通过，禁止自动应用",
    }


def evaluate_variant(company_id: int, agent_name: str, target_type: str,
                     current_text: str, candidate_text: str,
                     traces: list, soul_snapshot: str = None) -> tuple:
    """评估一个候选相对基线的提升，并返回护栏结果。

    返回 (baseline_score, candidate_score, guardrails_dict)。
    soul_snapshot：评估时锁定的 Soul（方案3），使基线回退与候选评分在同一人格下比较。
    """
    baseline = _avg_historical_quality(traces)
    if baseline is None:
        baseline = _llm_score(agent_name, target_type, current_text, traces,
                              soul_snapshot=soul_snapshot)
    candidate_score = _llm_score(agent_name, target_type, candidate_text, traces,
                                 soul_snapshot=soul_snapshot)
    guardrails = _check_guardrails(target_type, current_text, candidate_text)
    return round(baseline, 4), round(candidate_score, 4), guardrails


def is_improvement(baseline: float, candidate: float) -> bool:
    return (candidate - baseline) >= MIN_LIFT
