"""
hermes/evolution —— Hermes 自进化子系统

提供：
- trace     执行轨迹采集（ContextVar 传递，无需改 Agent 签名）
- store     四张表 CRUD（agent_traces / agent_skills / tool_overrides / evolution_candidates）
- extractor 技能自动沉淀（重复任务 → SKILL.md）
- evaluator 离线回放评估 + 五道护栏校验
- gepa      GEPA 进化引擎（read→mutate→evaluate→select）
- curator   技能管家（剪枝/评分）
- service   编排 + 人工审批/应用（Human-in-the-loop）
"""
from hermes.evolution import store, trace, extractor, evaluator, gepa, curator, service

__all__ = ["store", "trace", "extractor", "evaluator", "gepa", "curator", "service"]
