"""
hermes/evolution/curator.py —— 技能管家（The Curator，GEPA Phase 5 维护层）

对 agent_skills 做剪枝与评分，为后续进化提供干净的素材：
  - 标记"低价值"技能（使用次数低且质量分低），建议人工删除（复用 DELETE /skills/{id}）
  - 按 usage * perf 排序，给出"值得优先沉淀为进化目标"的清单
不自动物理删除（尊重 Human-in-the-loop）。
"""
import logging
import time

from hermes.evolution import store

logger = logging.getLogger(__name__)

# 低价值阈值
LOW_USAGE = 2          # 复用次数低于此
LOW_PERF = 0.4         # 质量分低于此


def suggest_prune(company_id: int, agent_name: str = None) -> dict:
    """返回建议剪枝的技能清单与保留清单。"""
    skills = store.list_skills(company_id, agent_name=agent_name, limit=200)
    to_prune, keep = [], []
    for s in skills:
        perf = s.perf_score if s.perf_score is not None else 0.5
        if s.usage_count <= LOW_USAGE and perf < LOW_PERF:
            to_prune.append({
                "id": s.id, "agent_name": s.agent_name,
                "skill_key": s.skill_key, "title": s.title,
                "usage_count": s.usage_count, "perf_score": perf,
            })
        else:
            keep.append({
                "id": s.id, "agent_name": s.agent_name,
                "skill_key": s.skill_key, "title": s.title,
                "usage_count": s.usage_count, "perf_score": perf,
                "value": round(s.usage_count * perf, 3),
            })
    keep.sort(key=lambda x: x["value"], reverse=True)
    return {
        "prune_count": len(to_prune),
        "keep_count": len(keep),
        "suggest_prune": to_prune,
        "top_keep": keep[:10],
    }


def auto_prune_low_value(company_id: int, agent_name: str = None,
                         dry_run: bool = True) -> dict:
    """真正执行剪枝（dry_run=True 只统计不删）。"""
    sug = suggest_prune(company_id, agent_name=agent_name)
    if dry_run:
        return {"dry_run": True, "would_delete": [x["id"] for x in sug["suggest_prune"]]}
    deleted = 0
    for x in sug["suggest_prune"]:
        if store.delete_skill(company_id, x["id"]):
            deleted += 1
    return {"deleted": deleted}
