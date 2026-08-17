"""
hermes/evolution/store.py —— 自进化数据存取（Phase 0/1/2 落点）

对 agent_traces / agent_skills / tool_overrides 三张表的 CRUD。
全部走 database.SessionLocal，与平台其它模块保持一致。
"""
import time
from typing import Optional

from database import SessionLocal
from models import AgentTrace, AgentSkill, ToolOverride, EvolutionCandidate


# ============ 轨迹 agent_traces ============

def save_trace(trace: dict) -> AgentTrace:
    """落一条执行轨迹。"""
    db = SessionLocal()
    try:
        rec = AgentTrace(
            company_id=trace["company_id"],
            agent_name=trace["agent_name"],
            session_id=trace.get("session_id"),
            signature=trace.get("signature"),
            task_input=trace.get("task_input", ""),
            steps_json=trace.get("steps_json", []),
            final_output=trace.get("final_output", ""),
            quality_score=trace.get("quality_score"),
            tool_count=trace.get("tool_count", 0),
            error=trace.get("error"),
            created_at=trace.get("created_at", time.time()),
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec
    finally:
        db.close()


def list_traces(company_id: int, agent_name: str = None,
                limit: int = 50, offset: int = 0) -> list:
    db = SessionLocal()
    try:
        q = db.query(AgentTrace).filter(AgentTrace.company_id == company_id)
        if agent_name:
            q = q.filter(AgentTrace.agent_name == agent_name)
        return q.order_by(AgentTrace.id.desc()).limit(limit).offset(offset).all()
    finally:
        db.close()


def count_traces_by_signature(company_id: int, agent_name: str,
                              signature: str, since_ts: float = 0) -> int:
    """统计某"任务签名"在近期的出现次数（技能提取触发用）。"""
    db = SessionLocal()
    try:
        return db.query(AgentTrace).filter(
            AgentTrace.company_id == company_id,
            AgentTrace.agent_name == agent_name,
            AgentTrace.signature == signature,
            AgentTrace.created_at >= since_ts,
        ).count()
    finally:
        db.close()


def get_recent_traces_for_signature(company_id: int, agent_name: str,
                                     signature: str, limit: int = 20) -> list:
    db = SessionLocal()
    try:
        return db.query(AgentTrace).filter(
            AgentTrace.company_id == company_id,
            AgentTrace.agent_name == agent_name,
            AgentTrace.signature == signature,
        ).order_by(AgentTrace.id.desc()).limit(limit).all()
    finally:
        db.close()


# ============ 技能 agent_skills ============

def upsert_skill(company_id: int, agent_name: str, skill_key: str,
                 title: str, trigger: str, workflow_md: str,
                 level0: str = "", soul_version: str = "") -> AgentSkill:
    """插入或更新技能（按 company+agent+skill_key 去重）。

    soul_version：技能沉淀/更新时的 Soul 版本指纹（方案2），用于检测
    "人格已变、技能可能不再匹配"的陈旧状态。
    """
    db = SessionLocal()
    try:
        rec = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.agent_name == agent_name,
            AgentSkill.skill_key == skill_key,
        ).first()
        now = time.time()
        if rec:
            rec.title = title
            rec.trigger = trigger
            rec.workflow_md = workflow_md
            rec.level0 = level0
            rec.version += 1
            rec.updated_at = now
            if soul_version:
                rec.soul_version = soul_version
        else:
            rec = AgentSkill(
                company_id=company_id, agent_name=agent_name,
                skill_key=skill_key, title=title, trigger=trigger,
                workflow_md=workflow_md, level0=level0,
                soul_version=soul_version,
                version=1, created_at=now, updated_at=now,
            )
            db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec
    finally:
        db.close()


def list_skills(company_id: int, agent_name: str = None,
                limit: int = 100, offset: int = 0,
                include_quarantined: bool = False) -> list:
    db = SessionLocal()
    try:
        q = db.query(AgentSkill).filter(AgentSkill.company_id == company_id)
        if not include_quarantined:
            # 方案4：默认排除被隔离的旧版本技能，避免注入已不匹配当前 Soul 的技能
            q = q.filter(AgentSkill.quarantined.is_(False))
        if agent_name:
            q = q.filter(AgentSkill.agent_name == agent_name)
        return q.order_by(AgentSkill.usage_count.desc()).limit(limit).offset(offset).all()
    finally:
        db.close()


def get_skill(company_id: int, skill_id: int) -> Optional[AgentSkill]:
    db = SessionLocal()
    try:
        return db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.id == skill_id).first()
    finally:
        db.close()


def get_skill_by_key(company_id: int, agent_name: str, skill_key: str,
                     include_quarantined: bool = False) -> Optional[AgentSkill]:
    db = SessionLocal()
    try:
        q = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.agent_name == agent_name,
            AgentSkill.skill_key == skill_key)
        if not include_quarantined:
            q = q.filter(AgentSkill.quarantined.is_(False))
        return q.first()
    finally:
        db.close()


def increment_skill_usage(company_id: int, agent_name: str,
                          skill_key: str) -> None:
    """命中已有技能时累加使用次数（隔离技能不计使用，避免旧技能被反复命中）。"""
    db = SessionLocal()
    try:
        rec = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.agent_name == agent_name,
            AgentSkill.skill_key == skill_key,
            AgentSkill.quarantined.is_(False),
        ).first()
        if rec:
            rec.usage_count += 1
            rec.updated_at = time.time()
            db.commit()
    finally:
        db.close()


def delete_skill(company_id: int, skill_id: int) -> bool:
    db = SessionLocal()
    try:
        rec = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.id == skill_id).first()
        if not rec:
            return False
        db.delete(rec)
        db.commit()
        return True
    finally:
        db.close()


def quarantine_stale_skills(company_id: int, agent_name: str,
                            new_soul_version: str) -> int:
    """方案4：Soul 大改时自动隔离该 Agent 的旧版本技能（比方案2的 stale 标志更硬）。

    把 soul_version != 新指纹、且尚未隔离的技能置 quarantined=True，使其不再被
    get_skill_by_key / list_skills / increment_skill_usage 匹配（即不再被注入/命中）。
    返回被隔离的技能数。
    """
    db = SessionLocal()
    try:
        now = time.time()
        recs = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.agent_name == agent_name,
            AgentSkill.quarantined.is_(False),
        ).all()
        n = 0
        for r in recs:
            if r.soul_version != new_soul_version:
                r.quarantined = True
                r.quarantined_at = now
                r.quarantine_reason = (
                    f"Soul 版本变更：技能沉淀时指纹为 {r.soul_version or '未知'}，"
                    f"当前为 {new_soul_version or '未知'}")
                n += 1
        if n:
            db.commit()
        return n
    finally:
        db.close()


def unquarantine_skill(company_id: int, skill_id: int) -> bool:
    """人工恢复被隔离的技能（解除 quarantine，重新参与匹配/注入）。"""
    db = SessionLocal()
    try:
        rec = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.id == skill_id).first()
        if not rec:
            return False
        rec.quarantined = False
        rec.quarantined_at = None
        rec.quarantine_reason = ""
        db.commit()
        return True
    finally:
        db.close()


# ============ Tool 覆盖 tool_overrides ============

def get_tool_override(company_id: int, tool_name: str) -> Optional[str]:
    """返回企业级 Tool 描述覆盖；无则返回 None（回退内置描述）。"""
    db = SessionLocal()
    try:
        rec = db.query(ToolOverride).filter(
            ToolOverride.company_id == company_id,
            ToolOverride.tool_name == tool_name).first()
        return rec.description if rec else None
    finally:
        db.close()


def set_tool_override(company_id: int, tool_name: str, description: str) -> ToolOverride:
    db = SessionLocal()
    try:
        rec = db.query(ToolOverride).filter(
            ToolOverride.company_id == company_id,
            ToolOverride.tool_name == tool_name).first()
        now = time.time()
        if rec:
            rec.description = description
            rec.updated_at = now
        else:
            rec = ToolOverride(company_id=company_id, tool_name=tool_name,
                               description=description, created_at=now, updated_at=now)
            db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec
    finally:
        db.close()


def list_tool_overrides(company_id: int) -> list:
    db = SessionLocal()
    try:
        return db.query(ToolOverride).filter(
            ToolOverride.company_id == company_id).all()
    finally:
        db.close()


def delete_tool_override(company_id: int, tool_name: str) -> bool:
    """删除企业级 Tool 描述覆盖（恢复内置默认）。"""
    db = SessionLocal()
    try:
        rec = db.query(ToolOverride).filter(
            ToolOverride.company_id == company_id,
            ToolOverride.tool_name == tool_name).first()
        if not rec:
            return False
        db.delete(rec)
        db.commit()
        return True
    finally:
        db.close()


# ============ 进化候选 evolution_candidates ============

def save_candidate(company_id: int, agent_name: str, target_type: str,
                   target_key: str, current_text: str, candidate_text: str,
                   rationale: str = "", baseline_score: float = None,
                   eval_score: float = None, guardrails: dict = None,
                   soul_version: str = "") -> EvolutionCandidate:
    """落一条待审批的进化候选（status=pending）。"""
    db = SessionLocal()
    try:
        now = time.time()
        rec = EvolutionCandidate(
            company_id=company_id, agent_name=agent_name,
            target_type=target_type, target_key=target_key,
            current_text=current_text, candidate_text=candidate_text,
            rationale=rationale, baseline_score=baseline_score,
            eval_score=eval_score, guardrails=guardrails or {},
            soul_version=soul_version,
            status="pending", created_at=now,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec
    finally:
        db.close()


def list_candidates(company_id: int, status: str = None,
                    agent_name: str = None, limit: int = 100) -> list:
    db = SessionLocal()
    try:
        q = db.query(EvolutionCandidate).filter(
            EvolutionCandidate.company_id == company_id)
        if status:
            q = q.filter(EvolutionCandidate.status == status)
        if agent_name:
            q = q.filter(EvolutionCandidate.agent_name == agent_name)
        return q.order_by(EvolutionCandidate.id.desc()).limit(limit).all()
    finally:
        db.close()


def get_candidate(company_id: int, candidate_id: int) -> Optional[EvolutionCandidate]:
    db = SessionLocal()
    try:
        return db.query(EvolutionCandidate).filter(
            EvolutionCandidate.company_id == company_id,
            EvolutionCandidate.id == candidate_id).first()
    finally:
        db.close()


def decide_candidate(company_id: int, candidate_id: int, status: str,
                     decided_by: int = None) -> Optional[EvolutionCandidate]:
    """更新候选状态（approved/rejected/applied）。"""
    db = SessionLocal()
    try:
        rec = db.query(EvolutionCandidate).filter(
            EvolutionCandidate.company_id == company_id,
            EvolutionCandidate.id == candidate_id).first()
        if not rec:
            return None
        rec.status = status
        rec.decided_at = time.time()
        rec.decided_by = decided_by
        db.commit()
        db.refresh(rec)
        return rec
    finally:
        db.close()


def update_skill_perf(company_id: int, agent_name: str, skill_key: str,
                      perf_score: float) -> None:
    """进化应用后回写技能质量分（取滑动平均）。"""
    db = SessionLocal()
    try:
        rec = db.query(AgentSkill).filter(
            AgentSkill.company_id == company_id,
            AgentSkill.agent_name == agent_name,
            AgentSkill.skill_key == skill_key,
            AgentSkill.quarantined.is_(False)).first()
        if rec:
            prev = rec.perf_score
            rec.perf_score = round(perf_score, 4) if prev is None \
                else round(prev * 0.7 + perf_score * 0.3, 4)
            rec.updated_at = time.time()
            db.commit()
    finally:
        db.close()
