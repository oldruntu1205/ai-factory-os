"""
api/evolution.py —— Hermes 自进化 API（Phase 0/2/3：轨迹 + 技能 + Tool 覆盖 + 进化引擎）

路由（挂在 /hermes/evolution 下）：
- GET  /traces              执行轨迹列表
- GET  /skills              已沉淀技能列表
- GET  /skills/{id}         技能详情
- DELETE /skills/{id}       删除技能（Curator 人工入口）
- POST /extract/run         手动回放轨迹触发技能提取（demo 用）
- GET  /tool-overrides      Tool 描述企业级覆盖列表
- PUT  /tool-override       设置 Tool 描述覆盖（GEPA Phase 2 落点）
- DELETE /tool-override/{name}  删除覆盖（恢复内置默认）
- POST /run                 触发一次 GEPA 进化，产出 pending 候选（不自动生效）
- GET  /candidates          查看进化候选（?status=pending）
- POST /candidates/{id}/approve  审批通过 → 写回运行态（人工门）
- POST /candidates/{id}/reject   拒绝候选
- POST /curate              技能管家：剪枝建议（dry_run）/执行
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from auth.deps import get_current_user, require_company
from hermes.evolution import store, extractor, service

router = APIRouter(prefix="/hermes/evolution", tags=["Hermes 自进化"])


def _require_admin(current: dict) -> None:
    """进化写操作（审批/删除/触发）仅允许企业管理员/平台超管（staff 无权改 AI 治理）"""
    if current["role"] not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _require_platform(current: dict) -> None:
    """平台级运维（全平台周期自进化）仅允许平台超管"""
    if current["role"] != "platform_admin":
        raise HTTPException(status_code=403, detail="仅平台超管可执行该操作")


# ============ 请求模型 ============

class ToolOverrideIn(BaseModel):
    tool_name: str
    description: str


class RunEvolutionIn(BaseModel):
    agent_name: str
    target_type: str                       # soul | tool | skill
    target_key: str = None                 # soul->agent_name; tool->tool_name; skill->skill_key
    n_variants: int = 3


# ============ 轨迹 ============

@router.get("/traces")
def get_traces(company_id: int, agent_name: str = None,
               limit: int = 50, current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    rows = store.list_traces(company_id, agent_name=agent_name, limit=min(limit, 200))
    return {
        "count": len(rows),
        "traces": [
            {
                "id": r.id,
                "agent_name": r.agent_name,
                "session_id": r.session_id,
                "signature": r.signature,
                "task_input": (r.task_input or "")[:200],
                "tool_count": r.tool_count,
                "quality_score": r.quality_score,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


# ============ 技能 ============

@router.get("/skills")
def get_skills(company_id: int, agent_name: str = None,
               limit: int = 100, stale_only: bool = False,
               include_quarantined: bool = False,
               quarantined_only: bool = False,
               current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    rows = store.list_skills(company_id, agent_name=agent_name,
                             limit=min(limit, 200),
                             include_quarantined=include_quarantined)
    # 方案2：以各 Agent 当前 Soul 版本为基准，标记技能是否陈旧（人格已变，技能可能不匹配）
    from hermes.registry.soul import soul_version
    cur_versions: dict = {}
    def _cur(ag):
        if ag not in cur_versions:
            cur_versions[ag] = soul_version(company_id, ag)
        return cur_versions[ag]
    out = []
    for r in rows:
        stale = bool(r.soul_version) and r.soul_version != _cur(r.agent_name)
        if stale_only and not stale:
            continue
        if quarantined_only and not r.quarantined:
            continue
        out.append({
            "id": r.id,
            "agent_name": r.agent_name,
            "skill_key": r.skill_key,
            "title": r.title,
            "trigger": r.trigger,
            "level0": r.level0,
            "usage_count": r.usage_count,
            "perf_score": r.perf_score,
            "version": r.version,
            "soul_version": r.soul_version,
            "stale": stale,
            "quarantined": r.quarantined,
            "quarantined_at": r.quarantined_at,
            "quarantine_reason": r.quarantine_reason,
            "updated_at": r.updated_at,
        })
    return {"count": len(out), "skills": out,
            "note": "stale=true 表示技能学习时的 Soul 版本与当前不一致；"
                    "quarantined=true 表示已因 Soul 大改被自动隔离（不再注入），可 POST /skills/{id}/restore 恢复"}


@router.get("/skills/{skill_id}")
def skill_detail(company_id: int, skill_id: int,
                 current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    r = store.get_skill(company_id, skill_id)
    if not r:
        raise HTTPException(status_code=404, detail="技能不存在")
    return {
        "id": r.id, "agent_name": r.agent_name, "skill_key": r.skill_key,
        "title": r.title, "trigger": r.trigger, "workflow_md": r.workflow_md,
        "level0": r.level0, "usage_count": r.usage_count,
        "perf_score": r.perf_score, "version": r.version,
        "soul_version": r.soul_version,
        "quarantined": r.quarantined,
        "quarantined_at": r.quarantined_at,
        "quarantine_reason": r.quarantine_reason,
    }


@router.delete("/skills/{skill_id}")
def delete_skill(company_id: int, skill_id: int,
                 current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    _require_admin(current)
    ok = store.delete_skill(company_id, skill_id)
    return {"deleted": ok}


@router.post("/skills/{skill_id}/restore")
def restore_skill(company_id: int, skill_id: int,
                  current: dict = Depends(get_current_user)):
    """人工恢复被隔离的技能（方案4：解除 quarantine，重新参与匹配/注入）。"""
    require_company(company_id, current)
    _require_admin(current)
    ok = store.unquarantine_skill(company_id, skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail="技能不存在")
    return {"restored": ok}


@router.post("/extract/run")
def run_extraction(company_id: int, agent_name: str = None,
                   current: dict = Depends(get_current_user)):
    """手动回放近期轨迹触发技能提取（自动提取也在每次 /hermes/execute 后运行）。"""
    require_company(company_id, current)
    _require_admin(current)
    traces = store.list_traces(company_id, agent_name=agent_name, limit=500)
    created = 0
    for t in traces:
        sig = t.signature or extractor.classify(t.agent_name, t.task_input or "")
        res = extractor.extract_after_trace(
            company_id, t.agent_name, sig, t.task_input or "", t.final_output or "")
        if res:
            created += 1
    return {"replayed": len(traces), "skills_created_or_updated": created}


# ============ Tool 描述覆盖 ============

@router.get("/tool-overrides")
def get_overrides(company_id: int, current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    rows = store.list_tool_overrides(company_id)
    return {"count": len(rows),
            "overrides": [{"tool_name": r.tool_name, "description": r.description}
                          for r in rows]}


@router.put("/tool-override")
def put_override(data: ToolOverrideIn, company_id: int,
                 current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    _require_admin(current)
    if len(data.description) > 500:
        raise HTTPException(status_code=400, detail="Tool 描述不超过 500 字符（GEPA 体积护栏）")
    rec = store.set_tool_override(company_id, data.tool_name, data.description)
    return {"tool_name": rec.tool_name, "description": rec.description,
            "updated_at": rec.updated_at}


@router.delete("/tool-override/{tool_name}")
def del_override(company_id: int, tool_name: str,
                 current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    _require_admin(current)
    ok = store.delete_tool_override(company_id, tool_name)
    return {"deleted": ok}


# ============ 进化引擎 GEPA（Phase 2/3）============

@router.post("/run")
def run_evolution(data: RunEvolutionIn, company_id: int,
                  current: dict = Depends(get_current_user)):
    """触发一次 GEPA 进化：read→mutate→evaluate→select，产出 pending 候选。"""
    require_company(company_id, current)
    _require_admin(current)
    return service.run_evolution(
        company_id, data.agent_name, data.target_type,
        target_key=data.target_key, n_variants=data.n_variants)


@router.get("/candidates")
def get_candidates(company_id: int, status: str = None, agent_name: str = None,
                   current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    return {"count": len(service.list_candidates(company_id, status=status, agent_name=agent_name)),
            "candidates": service.list_candidates(company_id, status=status, agent_name=agent_name)}


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(company_id: int, candidate_id: int,
                      current: dict = Depends(get_current_user)):
    """人工审批通过：写回运行态（Soul 文件 / Tool 覆盖 / 技能），这是 Human-in-the-loop 门。"""
    require_company(company_id, current)
    _require_admin(current)
    operator_id = current.get("user_id") if isinstance(current, dict) else None
    return service.approve_candidate(company_id, candidate_id, operator_id=operator_id)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(company_id: int, candidate_id: int,
                     current: dict = Depends(get_current_user)):
    require_company(company_id, current)
    _require_admin(current)
    operator_id = current.get("user_id") if isinstance(current, dict) else None
    return service.reject_candidate(company_id, candidate_id, operator_id=operator_id)


@router.post("/curate")
def curate(company_id: int, dry_run: bool = True, agent_name: str = None,
           current: dict = Depends(get_current_user)):
    """技能管家：dry_run=True 给剪枝建议；dry_run=False 执行物理剪枝。"""
    require_company(company_id, current)
    _require_admin(current)
    return service.curate(company_id, agent_name=agent_name, dry_run=dry_run)


@router.post("/auto-run")
def auto_run(company_id: int, agent_name: str = None, lookback_days: int = 30,
             current: dict = Depends(get_current_user)):
    """Phase 5 全自动持续进化循环：扫描近期有轨迹的 Agent，自动跑 GEPA 生成 pending 候选。

    注意：循环全自动，但每个候选仍为 pending，必须人工 approve 才写回运行态。
    """
    require_company(company_id, current)
    _require_admin(current)
    operator_id = current.get("user_id") if isinstance(current, dict) else None
    return service.auto_evolve_cycle(
        company_id, agent_name=agent_name, operator_id=operator_id,
        lookback_days=lookback_days)


# ============ 周期自进化调度器（Phase 5 落地）============

@router.get("/scheduler/status")
def get_scheduler_status(current: dict = Depends(get_current_user)):
    """查看后台周期自进化调度器状态（间隔/上次运行/累计候选）。"""
    from hermes.evolution.scheduler import scheduler_status
    return scheduler_status()


@router.post("/scheduler/trigger")
def trigger_scheduler(current: dict = Depends(get_current_user)):
    """手动触发一轮「全企业周期自进化」（仅平台超管——修复：此前任何企业管理员可触发，
    跨租户写 candidates 表 + 平台级 LLM 费用 DoS；仅产出 pending 候选）。"""
    _require_platform(current)
    from hermes.evolution.scheduler import get_scheduler
    return get_scheduler().run_once()
