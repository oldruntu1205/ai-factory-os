"""
hermes/audit —— AI 员工工具调用审计与回放

对外接口：
    from hermes.audit.service import record_tool_call, query_tool_calls, replay_session

设计约束：
- 只落库，不抛异常（审计失败绝不能阻断业务调用）
- 参数入库前脱敏（token / password / secret / authorization）
- 与 utils.audit.log_action 联动，让"安全审计"页也能看到 AI 员工动作
"""
