"""
hermes/runtime.py —— Hermes Runtime 主入口（第4章 4.2）

编排核心子系统：Agent Registry / Tool Center / Memory System / Permission System。
Agent 在 Hermes 上运行，通过 Tool 访问企业系统，全程受权限与记忆约束。
"""
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# 运行中会话默认有效期（秒）：超过即视为过期，防止 _running 无限增长
SESSION_TTL_SECONDS = 3600


class HermesRuntime:
    """
    Hermes Agent Runtime —— AI 员工运行环境。

    职责：
    - 加载 / 执行 / 卸载 Agent（生命周期管理）
    - 路由 Tool 调用并做权限校验
    - 管理三层记忆（短期/工作/长期）
    - 派发异步任务到消息队列
    """

    def __init__(self, agent_registry=None, tool_center=None,
                 memory_system=None, permission_system=None, queue=None):
        self.agent_registry = agent_registry
        self.tool_center = tool_center
        self.memory_system = memory_system
        self.permission_system = permission_system
        self.queue = queue
        # 运行中的 Agent 会话
        self._running = {}

    # ---------- Agent 生命周期 ----------

    def start_agent(self, agent_name: str, tenant_id: int,
                    company_id: int, session_id: Optional[str] = None) -> dict:
        """启动一个 AI 员工会话（创建运行上下文）"""
        agent = self.agent_registry.get_agent(agent_name)
        if not agent:
            raise ValueError(f"Agent [{agent_name}] 未注册")

        # 权限预检：该租户是否有权使用此 Agent
        if self.permission_system:
            self.permission_system.assert_agent_allowed(tenant_id, agent_name)

        # 惰性清理过期会话，防止 _running 无限增长
        self._evict_stale_sessions()

        session_id = session_id or uuid.uuid4().hex
        ctx = {
            "session_id": session_id,
            "agent_name": agent_name,
            "tenant_id": tenant_id,
            "company_id": company_id,
            "started_at": time.time(),
            "memory": {},
        }
        self._running[session_id] = ctx

        # 恢复工作记忆
        if self.memory_system:
            ctx["memory"] = self.memory_system.load_working_memory(
                agent_name, company_id) or {}

        logger.info(f"Hermes: agent [{agent_name}] started, session={session_id}")
        return ctx

    def execute(self, agent_name: str, message: str, tenant_id: int,
                company_id: int, session_id: Optional[str] = None) -> dict:
        """
        执行一次 Agent 任务：
        1. 启动会话
        2. 注入记忆（短期+工作+长期）
        3. 调用 Agent 实现
        4. 写入记忆
        5. 采集执行轨迹（Phase 0 自进化地基）+ 触发技能沉淀
        """
        ctx = self.start_agent(agent_name, tenant_id, company_id, session_id)
        session_id = ctx["session_id"]

        # 自进化：开启轨迹记录（ContextVar 在调用链内传递）
        from hermes.evolution.trace import TraceRecorder, set_recorder, clear_recorder
        from hermes.evolution import extractor as _extractor
        rec = TraceRecorder(company_id, agent_name, session_id, str(message))
        set_recorder(rec)

        # 组装运行时上下文（记忆 + 工具访问器 + 权限）
        runtime_context = {
            "session_id": session_id,
            "agent_name": agent_name,
            "tenant_id": tenant_id,
            "company_id": company_id,
            "memory": ctx["memory"],
            "tools": self.tool_center if self.tool_center else None,
            "permissions": self.permission_system,
        }

        try:
            result = self.agent_registry.invoke_agent(
                agent_name, message, runtime_context)
            rec.set_output(str(result))
        except Exception as e:
            rec.set_error(str(e))
            logger.error(f"Hermes: agent [{agent_name}] failed: {e}")
            clear_recorder()
            raise

        # 写回工作记忆（短期由 LLM 层写 Redis）
        if self.memory_system:
            self.memory_system.save_working_memory(
                agent_name, company_id, runtime_context["memory"])
            self.memory_system.save_short_term_memory(
                session_id, {"user": message, "agent": str(result)[:2000]})

        # 自进化：落轨迹 + 触发技能沉淀
        try:
            rec.signature = _extractor.classify(agent_name, str(message))
            from hermes.evolution import store as _store
            _store.save_trace(rec.to_dict())
            _extractor.extract_after_trace(
                company_id, agent_name, rec.signature, str(message), str(result))
        except Exception as e:
            logger.warning("轨迹采集/技能提取异常（不影响主流程）: %s", e)
        finally:
            clear_recorder()

        return {
            "session_id": session_id,
            "agent_name": agent_name,
            "result": result,
        }

    def stop_agent(self, session_id: str) -> None:
        """结束 Agent 会话"""
        self._running.pop(session_id, None)

    def _evict_stale_sessions(self) -> None:
        """清理超过 SESSION_TTL_SECONDS 的过期会话（惰性：仅在新会话启动时触发）"""
        now = time.time()
        expired = [sid for sid, ctx in self._running.items()
                   if now - ctx.get("started_at", 0) > SESSION_TTL_SECONDS]
        for sid in expired:
            self._running.pop(sid, None)
        if expired:
            logger.info("Hermes: 清理 %d 个过期会话", len(expired))

    def assert_session_owner(self, session_id: str, company_id: int) -> dict:
        """校验会话存在且归属于指定企业，返回会话上下文（防跨租户借用会话）"""
        ctx = self._running.get(session_id)
        if not ctx:
            raise ValueError("会话不存在或已结束")
        if int(ctx.get("company_id", -1)) != int(company_id):
            raise ValueError("会话不属于当前企业")
        return ctx

    # ---------- 工具调度 ----------

    def call_tool(self, session_id: str, tool_name: str,
                  params: dict, actor: str = "agent") -> dict:
        """Agent 调用 Tool 的统一入口（Tool Router）"""
        ctx = self._running.get(session_id)
        if not ctx:
            raise ValueError("会话不存在或已结束")

        if self.permission_system:
            self.permission_system.assert_tool_allowed(
                ctx["tenant_id"], ctx["agent_name"], tool_name)

        if not self.tool_center:
            raise ValueError("Tool Center 未配置")
        return self.tool_center.execute(tool_name, params, company_id=ctx["company_id"])

    # ---------- 异步任务 ----------

    def dispatch(self, task_name: str, payload: dict) -> dict:
        """将任务派发到消息队列（异步执行）"""
        if not self.queue:
            return {"status": "queue_not_configured"}
        return self.queue.enqueue(task_name, payload)


# 全局单例（应用启动时装配）
runtime: Optional[HermesRuntime] = None


def get_runtime() -> HermesRuntime:
    if runtime is None:
        raise RuntimeError("Hermes Runtime 尚未初始化（请先在 app 启动时调用 init_hermes()）")
    return runtime


def init_hermes() -> HermesRuntime:
    """装配 Hermes Runtime（lazy import 避免循环依赖）"""
    global runtime
    from hermes.registry.agent_registry import AgentRegistry
    from hermes.tool.center import ToolCenter
    from hermes.memory.memory_system import MemorySystem
    from hermes.permissions.rbac import RBACSystem
    from hermes.queue.task_queue import TaskQueue

    runtime = HermesRuntime(
        agent_registry=AgentRegistry(),
        tool_center=ToolCenter(),
        memory_system=MemorySystem(),
        permission_system=RBACSystem(),
        queue=TaskQueue(),
    )
    logger.info("Hermes Agent Runtime 初始化完成")
    return runtime
