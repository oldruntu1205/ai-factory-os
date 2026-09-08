"""
hermes/registry/agent_registry.py —— Agent 注册中心（第5章）

功能：Agent 注册 / 发现 / 版本管理 / 生命周期管理。
让平台能动态加载任意行业的 AI 员工（销售/客服/报价/生产/质量...）。

多租户安全模型（2026 修复）：内存 key = (company_id, name)。
- company_id=0 为平台级（内置 Agent / 商城包级 Agent），所有租户共享可见可执行；
- company_id>0 为企业私有自定义 Agent，仅本企业可见可执行；
- 同名不同属主互不覆盖（DB 由 (company_id, name) 复合唯一兜底）。
"""
import logging
from typing import Dict, List, Optional

from hermes.registry.models import AgentManifest

logger = logging.getLogger(__name__)


class AgentRegistry:
    """内存版注册中心。生产可扩展为 Redis/PostgreSQL 存储 + 分布式同步。

    自定义 Agent（agent_manifests 表）在启动时 load_custom() 加载进内存，
    保证重启不丢；内置 Agent 由 main.py 启动注册。
    """

    def __init__(self):
        # 安全修复（跨租户同名覆盖）：key = (company_id, name)，见模块 docstring
        self._agents: Dict[tuple, AgentManifest] = {}

    @staticmethod
    def _key(company_id, name):
        return (int(company_id or 0), name)

    # ---------- 持久化自定义 Agent ----------

    def load_custom(self) -> int:
        """从 agent_manifests 表加载自定义注册的 Agent（启动时调用，重启不丢）"""
        from database import SessionLocal
        from models import AgentManifestRecord
        db = SessionLocal()
        n = 0
        try:
            rows = db.query(AgentManifestRecord).all()
            for row in rows:
                manifest = AgentManifest(
                    name=row.name, version=row.version or "1.0",
                    industry=row.industry or "general",
                    label=row.label or row.name,
                    description=row.description or "",
                    tools=list(row.tools or []),
                    permissions=list(row.permissions or []),
                    system_prompt=row.system_prompt or "",
                    status=row.status or "registered",
                    company_id=row.company_id or 0,
                )
                manifest.handler = self._make_custom_handler(manifest)
                self._agents[self._key(manifest.company_id, manifest.name)] = manifest
                n += 1
        finally:
            db.close()
        if n:
            logger.info(f"Registry: 从持久化加载 {n} 个自定义 Agent")
        return n

    @staticmethod
    def _make_custom_handler(manifest: AgentManifest):
        """自定义 Agent 的统一处理器（prompt 驱动编排）"""
        from hermes.registry.prompt_agent import prompt_agent_handler

        def handler(message: str, runtime_ctx: dict):
            # 把 manifest 注入上下文供 prompt 编排读取（工具白名单/系统提示词）
            runtime_ctx["manifest"] = manifest
            return prompt_agent_handler(message, runtime_ctx)

        return handler

    def persist(self, manifest: AgentManifest) -> None:
        """持久化自定义 Agent 到 agent_manifests 表（同属主幂等 upsert）。

        安全修复：查重条件含 company_id —— 不同企业的同名 Agent 互不可达，
        杜绝『企业 B 同名注册覆盖企业 A manifest』的跨租户篡改。
        """
        from database import SessionLocal
        from models import AgentManifestRecord
        db = SessionLocal()
        try:
            cid = int(getattr(manifest, "company_id", 0) or 0)
            row = db.query(AgentManifestRecord).filter(
                AgentManifestRecord.name == manifest.name,
                AgentManifestRecord.company_id == cid).first()
            if not row:
                row = AgentManifestRecord(name=manifest.name, company_id=cid)
                db.add(row)
            row.version = manifest.version
            row.industry = manifest.industry
            row.label = manifest.label
            row.description = manifest.description
            row.tools = list(manifest.tools)
            row.permissions = list(manifest.permissions)
            row.system_prompt = manifest.system_prompt
            row.status = manifest.status
            row.company_id = cid
            from datetime import datetime
            row.updated_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()

    # ---------- 注册 / 注销 ----------

    def register(self, manifest: AgentManifest) -> AgentManifest:
        """注册 Agent（同属主同名新版本覆盖旧版本，实现版本管理）"""
        self._agents[self._key(manifest.company_id, manifest.name)] = manifest
        logger.info(f"Registry: agent [{manifest.name}] (company={manifest.company_id}) "
                    f"v{manifest.version} 注册成功, tools={manifest.tools}")
        return manifest

    def unregister(self, name: str, company_id: int = 0) -> bool:
        """注销指定属主的 Agent（默认平台级）"""
        return self._agents.pop(self._key(company_id, name), None) is not None

    # ---------- 发现 ----------

    def get_agent(self, name: str, company_id: int = 0) -> Optional[AgentManifest]:
        """查询指定属主的 Agent（平台级传 0；企业私有传企业 id）。

        多 worker 一致性（uvicorn --workers N 各进程独立内存）：
        - 平台内置 Agent（company_id=0 且为内置名）：DB 无行，走内存常驻路径；
        - 企业私有（company_id>0）与持久化平台级：以 DB 为真源——每次查询同步
          DB 行到本进程内存，保证『其他 worker 上的注册更新/注销』即时可见，
          不会因本进程内存命中旧 manifest 而永久 stale；
        - 平台级非内置（商城包级 Agent 仅内存注册、未持久化）：DB 无行时保留内存。
        """
        cid = int(company_id or 0)
        key = self._key(cid, name)
        if cid == 0 and name in self._BUILTIN_NAMES:
            # 平台内置：常驻内存，agent_manifests 无其行，直接返回
            return self._agents.get(key)
        # DB 真源：有行 → 装载覆盖本进程内存并返回
        agent = self._load_one_from_db(name, cid)
        if agent is not None:
            return agent
        if cid > 0:
            # 企业私有：DB 无行 = 已在其他进程注销 → 清除本进程残留
            self._agents.pop(key, None)
            return None
        # 平台级非内置（如商城内存注册未持久化）：DB 无行时保留内存注册
        return self._agents.get(key)

    def resolve_agent(self, name: str, company_id: int) -> Optional[AgentManifest]:
        """解析实际生效的 Agent：企业私有（company_id>0）优先，未命中回退平台级（0）。

        内置 / 商城包级 Agent 均为平台级（company_id=0），所有租户共用一份，
        因此该回退使各企业仍可执行内置 Agent，且企业同名私有 Agent 互不干扰。
        """
        cid = int(company_id or 0)
        agent = self.get_agent(name, cid)
        if agent is None and cid != 0:
            agent = self.get_agent(name, 0)
        return agent

    def _load_one_from_db(self, name: str, company_id: int = 0) -> Optional[AgentManifest]:
        try:
            from database import SessionLocal
            from models import AgentManifestRecord
            db = SessionLocal()
            try:
                cid = int(company_id or 0)
                row = db.query(AgentManifestRecord).filter(
                    AgentManifestRecord.name == name,
                    AgentManifestRecord.company_id == cid).first()
                if not row:
                    return None
                manifest = AgentManifest(
                    name=row.name, version=row.version or "1.0",
                    industry=row.industry or "general",
                    label=row.label or row.name,
                    description=row.description or "",
                    tools=list(row.tools or []),
                    permissions=list(row.permissions or []),
                    system_prompt=row.system_prompt or "",
                    status=row.status or "registered",
                    company_id=row.company_id or 0,
                )
                manifest.handler = self._make_custom_handler(manifest)
                self._agents[self._key(manifest.company_id, manifest.name)] = manifest
                return manifest
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Registry: 从 DB 加载 Agent [{name}] (company={company_id}) 失败: {e}")
            return None

    def list_agents(self, industry: Optional[str] = None,
                    status: Optional[str] = None,
                    company_id: Optional[int] = None) -> List[AgentManifest]:
        """列出 Agent。

        company_id=None → 全量（内部/运维用）；
        company_id>=0 → 可见集合 = 平台级(0) + 本企业私有（company_id 相同）。
        """
        # 合并持久化的自定义 Agent（多 worker 下各 worker 内存可能不全）
        try:
            from database import SessionLocal
            from models import AgentManifestRecord
            db = SessionLocal()
            try:
                rows = db.query(AgentManifestRecord).all()
                db_keys = {self._key(row.company_id or 0, row.name) for row in rows}
                for row in rows:
                    # 多 worker 一致性：以 DB 行为准总是覆盖本进程内存（注册更新即时可见）
                    key = self._key(row.company_id or 0, row.name)
                    manifest = AgentManifest(
                        name=row.name, version=row.version or "1.0",
                        industry=row.industry or "general",
                        label=row.label or row.name,
                        description=row.description or "",
                        tools=list(row.tools or []),
                        permissions=list(row.permissions or []),
                        system_prompt=row.system_prompt or "",
                        status=row.status or "registered",
                        company_id=row.company_id or 0,
                    )
                    manifest.handler = self._make_custom_handler(manifest)
                    self._agents[key] = manifest
                # 同步删除：内存中已注销（DB 无记录）的自定义 Agent，避免跨 worker 残留。
                # 仅平台级内置 Agent 名（company_id=0 且内置）常驻不随 DB 清理。
                for key in [k for k in self._agents if k not in db_keys]:
                    cid, name = key
                    if cid == 0 and name in self._BUILTIN_NAMES:
                        continue
                    self._agents.pop(key, None)
            finally:
                db.close()
        except Exception:
            pass  # 表不存在/未初始化时忽略

        result = []
        for key, a in self._agents.items():
            if industry and a.industry != industry:
                continue
            if status and a.status != status:
                continue
            if company_id is not None:
                cid = int(company_id)
                if a.company_id not in (0, cid):
                    continue  # 只暴露平台级 + 本企业私有
            result.append(a)
        return result

    # 平台内置 Agent 名（main.py 启动注册；company_id=0 常驻，不随 DB 同步删除）
    _BUILTIN_NAMES = frozenset({
        "customer_agent", "sales_agent", "quotation_agent",
        "production_agent", "quality_agent", "ceo_agent", "process_agent",
    })

    @staticmethod
    def _is_custom(name: str) -> bool:
        """（兼容保留）内置 Agent 名不属于自定义"""
        return name not in {
            "customer_agent", "sales_agent", "quotation_agent",
            "production_agent", "quality_agent", "ceo_agent", "process_agent",
        }

    def discover(self, industry: str) -> List[AgentManifest]:
        """按行业发现可用 Agent（平台级 + 企业私有全量）"""
        return [a for a in self._agents.values()
                if a.industry == industry and a.status != "disabled"]

    # ---------- 生命周期 ----------

    def activate(self, name: str, company_id: int = 0) -> bool:
        a = self._agents.get(self._key(company_id, name))
        if a:
            a.status = "registered"
            return True
        return False

    def deactivate(self, name: str, company_id: int = 0) -> bool:
        a = self._agents.get(self._key(company_id, name))
        if a:
            a.status = "disabled"
            return True
        return False

    # ---------- 执行 ----------

    def invoke_agent(self, name: str, message: str, runtime_ctx: dict) -> str:
        """调用 Agent 实现（handler 或默认工作流）——按运行企业解析属主"""
        manifest = self.resolve_agent(name, runtime_ctx.get("company_id", 0))
        if not manifest:
            raise ValueError(f"Agent [{name}] 未注册")
        if manifest.status == "disabled":
            raise ValueError(f"Agent [{name}] 已停用")

        if manifest.handler:
            return manifest.handler(message, runtime_ctx)

        # 无 handler 时，退回通用 LangGraph 工作流
        from agents.service import execute_agent
        return execute_agent(message, runtime_ctx["company_id"])
