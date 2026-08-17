"""
hermes/evolution/service.py —— 进化编排（人工兜底入口）

把 GEPA 四阶段与护栏串起来，并提供 Human-in-the-loop 的审批/应用：
  run_evolution   触发一次进化，产出 pending 候选（不自动生效）
  list_candidates 查看待审批/全部候选
  approve_candidate 审批通过 → apply 写回 Soul/Tool/Skill（这是"人工门"）
  reject_candidate   拒绝候选
  curate             触发技能管家剪枝建议

设计原则（与 Hermes 一致）：所有进化改动都经人工审查，从不自动提交到运行态。
"""
import logging
import time

from hermes.evolution import store, gepa, curator

logger = logging.getLogger(__name__)

VALID_TARGETS = ("soul", "tool", "skill", "tool_code")


def run_evolution(company_id: int, agent_name: str, target_type: str,
                  target_key: str = None, n_variants: int = 3) -> dict:
    if target_type not in VALID_TARGETS:
        return {"ok": False, "reason": f"target_type 必须是 {VALID_TARGETS}"}
    engine = gepa.GEPAEngine()
    return engine.run(company_id, agent_name, target_type, target_key, n_variants)


def list_candidates(company_id: int, status: str = None,
                    agent_name: str = None) -> list:
    rows = store.list_candidates(company_id, status=status, agent_name=agent_name)
    # 方案3：对比候选评估时的 Soul 版本与当前版本，标记基线是否已漂移
    try:
        from hermes.registry.soul import soul_version
        cur_versions = {}
        def _cur(ag):
            if ag not in cur_versions:
                cur_versions[ag] = soul_version(company_id, ag)
            return cur_versions[ag]
    except Exception:
        _cur = lambda ag: None  # noqa
    return [{
        "id": r.id, "agent_name": r.agent_name, "target_type": r.target_type,
        "target_key": r.target_key, "baseline_score": r.baseline_score,
        "eval_score": r.eval_score, "lift": (round(r.eval_score - r.baseline_score, 4)
                                             if r.eval_score is not None and r.baseline_score is not None else None),
        "status": r.status, "rationale": r.rationale,
        "guardrails_pass": (r.guardrails or {}).get("all_pass"),
        # 审批 UI 需要展示进化前后内容（截断，避免列表过载）
        "current_text": (r.current_text or "")[:800],
        "candidate_text": (r.candidate_text or "")[:1200],
        "soul_version": r.soul_version,
        "soul_changed": (bool(r.soul_version) and _cur(r.agent_name) is not None
                         and r.soul_version != _cur(r.agent_name)),
        "created_at": r.created_at,
    } for r in rows]


def _apply(company_id: int, cand) -> dict:
    """把候选写回运行态（Soul 文件 / tool_override / skill 表）。"""
    tt, tk = cand.target_type, cand.target_key
    if tt == "soul":
        from hermes.registry.soul import write_soul_base
        # 进化只写「底座层」，与企业覆盖层落在不同文件，互不覆盖
        res = write_soul_base(company_id, tk or cand.agent_name, cand.candidate_text)
        return {"applied_to": "soul_base", "path": res.get("path")}
    if tt == "tool":
        rec = store.set_tool_override(company_id, tk, cand.candidate_text)
        return {"applied_to": "tool_override", "tool_name": rec.tool_name}
    if tt == "skill":
        existing = store.get_skill_by_key(company_id, cand.agent_name, tk)
        try:
            from hermes.registry.soul import soul_version
            sv = soul_version(company_id, cand.agent_name)
        except Exception:
            sv = ""
        store.upsert_skill(
            company_id=company_id, agent_name=cand.agent_name, skill_key=tk,
            title=existing.title if existing else tk,
            trigger=existing.trigger if existing else "",
            workflow_md=cand.candidate_text,
            level0=existing.level0 if existing else "",
            soul_version=sv)
        if cand.eval_score is not None:
            store.update_skill_perf(company_id, cand.agent_name, tk, cand.eval_score)
        return {"applied_to": "skill", "skill_key": tk, "soul_version": sv}
    if tt == "tool_code":
        # 代码进化：绝不热替换运行代码，只产提案文件，由人工合并到 tools.py 后重启生效
        from pathlib import Path
        from config import BASE_DIR
        patch_dir = Path(BASE_DIR) / "hermes" / "evolution" / "tool_patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        path = patch_dir / f"{tk}.patch.md"
        report = (cand.guardrails or {}).get("test_report", {})
        content = (
            f"# Tool 代码进化提案: `{tk}`\n\n"
            f"> 由 GEPA 自动生成，隔离测试"
            f"{'通过' if (cand.guardrails or {}).get('all_pass') else '未通过'}。"
            f"请人工 review 后合并到 `hermes/tool/tools.py`，重启生效。\n\n"
            f"## 原实现\n\n```python\n{cand.current_text}\n```\n\n"
            f"## 进化候选\n\n```python\n{cand.candidate_text}\n```\n\n"
            f"## 测试报告\n\n```json\n{report}\n```\n\n"
            f"## 改进理由\n\n{cand.rationale}\n"
        )
        path.write_text(content, encoding="utf-8")
        return {"applied_to": "tool_code_patch", "patch_file": str(path),
                "note": "已生成代码提案文件，需人工合并到 tools.py 后重启生效（默认不热替换运行代码）"}
    raise ValueError(f"未知 target_type: {tt}")


def approve_candidate(company_id: int, candidate_id: int,
                      operator_id: int = None) -> dict:
    cand = store.get_candidate(company_id, candidate_id)
    if not cand:
        return {"ok": False, "reason": "候选不存在"}
    if cand.status != "pending":
        return {"ok": False, "reason": f"候选状态为 {cand.status}，不可重复审批"}
    if not (cand.guardrails or {}).get("all_pass"):
        return {"ok": False, "reason": "护栏未通过，禁止应用", "guardrails": cand.guardrails}
    applied = _apply(company_id, cand)
    store.decide_candidate(company_id, candidate_id, "applied", decided_by=operator_id)
    logger.info("进化候选已审批应用: company=%s id=%s type=%s key=%s",
                company_id, candidate_id, cand.target_type, cand.target_key)
    return {"ok": True, "candidate_id": candidate_id, "applied": applied,
            "note": "已写回运行态，下次该员工加载 Soul/Tool/Skill 时生效"}


def reject_candidate(company_id: int, candidate_id: int,
                     operator_id: int = None) -> dict:
    cand = store.get_candidate(company_id, candidate_id)
    if not cand:
        return {"ok": False, "reason": "候选不存在"}
    if cand.status != "pending":
        return {"ok": False, "reason": f"候选状态为 {cand.status}，不可重复操作"}
    store.decide_candidate(company_id, candidate_id, "rejected", decided_by=operator_id)
    return {"ok": True, "candidate_id": candidate_id, "status": "rejected"}


def curate(company_id: int, agent_name: str = None, dry_run: bool = True) -> dict:
    """触发技能管家：dry_run=True 只给建议，False 执行物理剪枝。"""
    return curator.auto_prune_low_value(company_id, agent_name=agent_name, dry_run=dry_run)


# ---------- Phase 5: 全自动持续进化循环 ----------

def auto_evolve_cycle(company_id: int, agent_name: str = None,
                      operator_id: int = None, lookback_days: int = 30,
                      target_types: tuple = ("soul", "skill")) -> dict:
    """定期扫描近期有轨迹的 Agent，自动跑 GEPA 生成 pending 候选（仍走人工审批）。

    设计（与 Hermes 一致）：循环是"全自动"的，但产出的每个候选都是 pending，
    绝不自动 apply——必须人工 approve 才写回运行态。
    """
    since = time.time() - lookback_days * 86400
    traces = store.list_traces(company_id, agent_name=agent_name, limit=500)
    agents: dict = {}
    for t in traces:
        agents[t.agent_name] = agents.get(t.agent_name, 0) + 1

    created = []
    agent_soul = {}
    for ag, cnt in agents.items():
        if cnt < 3:           # 轨迹太少不进化（避免噪声）
            continue
        # 方案3：记录本轮各 Agent 的 Soul 版本，供前端/调度器判断是否基线漂移
        try:
            from hermes.registry.soul import soul_version
            agent_soul[ag] = soul_version(company_id, ag)
        except Exception:
            agent_soul[ag] = None
        for tt in target_types:
            if tt == "skill":
                skills = store.list_skills(company_id, agent_name=ag, limit=50)
                for sk in skills:
                    r = run_evolution(company_id, ag, "skill", target_key=sk.skill_key)
                    if r.get("ok"):
                        created.append({"agent": ag, "type": "skill",
                                        "key": sk.skill_key,
                                        "candidate_id": r.get("candidate_id")})
                continue
            tk = ag if tt == "soul" else None
            r = run_evolution(company_id, ag, tt, target_key=tk)
            if r.get("ok"):
                created.append({"agent": ag, "type": tt,
                                "candidate_id": r.get("candidate_id")})
    # 检测"技能陈旧"：本次扫描到的 Agent 中，存在 soul 版本与技能打点时不一致的技能
    stale_skills = 0
    try:
        for ag, sv in agent_soul.items():
            if not sv:
                continue
            for sk in store.list_skills(company_id, agent_name=ag, limit=200):
                if sk.soul_version and sk.soul_version != sv:
                    stale_skills += 1
    except Exception:
        pass
    return {"agents_scanned": len(agents),
            "candidates_created": created,
            "agent_soul_versions": agent_soul,
            "stale_skills": stale_skills,
            "note": "候选均为 pending，需人工 approve 后才生效；"
                    "soul 版本变动可能导致既有技能/候选基线漂移，请复核 stale 项"}
