"""可信登记表加载与 fail-closed 准入判定（P0 Mock）。

契约依据：`packages/contracts/schemas/common-context.json` 的 `x-admission-policy`。
必须的拒绝语义（契约原文）：
    unknown_tenant / unknown_tool / unknown_data_class / sensitive_nonlocal_model /
    missing_registry → reject

本模块只做**登记表查询**，不信任任何请求自填字段。登记表缺失或损坏时一律拒绝
（fail-closed），不允许退化成"放行"。
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

# 仓库根：services/_kernel/registry.py → 上溯两级
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = pathlib.Path(__file__).resolve().parent / "registry.mock.json"

DATA_CLASSES = ("C0", "C1", "C2", "C3", "C4")
RISK_CLASSES = ("R0", "R1", "R2", "R3")


class RegistryUnavailable(RuntimeError):
    """登记表不可用。契约要求此时拒绝，而不是放行。"""


class Registry:
    """只读登记表视图。任何缺失都返回 None，由调用方决定拒绝码。"""

    def __init__(self, document: dict[str, Any], source: str) -> None:
        self._doc = document
        self.source = source

    # ---- 加载 -------------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Registry":
        target = pathlib.Path(path) if path else DEFAULT_REGISTRY
        if not target.is_absolute():
            target = (REPO_ROOT / target).resolve()
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise RegistryUnavailable(f"登记表不可读: {target} ({exc})") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistryUnavailable(f"登记表不是合法 JSON: {target} ({exc})") from exc
        if not isinstance(document, dict):
            raise RegistryUnavailable(f"登记表根对象必须是 object: {target}")
        return cls(document, str(target))

    # ---- 基础访问 ---------------------------------------------------------

    @property
    def document(self) -> dict[str, Any]:
        return self._doc

    @property
    def registry_version(self) -> str | None:
        value = self._doc.get("registry_version")
        return value if isinstance(value, str) else None

    @property
    def environment(self) -> str:
        return str(self._doc.get("environment") or "unknown")

    def _section(self, name: str) -> dict[str, Any]:
        value = self._doc.get(name)
        return value if isinstance(value, dict) else {}

    # ---- 租户 -------------------------------------------------------------

    def tenant(self, tenant_id: str | None) -> dict[str, Any] | None:
        if not tenant_id:
            return None
        return self._section("tenants").get(tenant_id)

    def employee_ids(self, tenant_id: str) -> list[str]:
        """本租户的员工 id 列表。

        ## 为什么需要这个（2026-10-05）

        `employees` 原先是一串**裸 id**（`["employee-planning"]`），与
        `packages/contracts/schemas/p3-employee.json` 契约里的**结构化记录**
        不一致——这是同一个概念的**第三份真相**（契约一份、登记表一份、这里一份），
        审计时被我漏掉了。

        现在 `employees` 按契约存**记录**（含 `executing_actor_ref` / `positions` /
        `limits` / `config_authority`），所以任何"只想知道有哪些员工"的地方
        （如 `tenant_allows` 的成员校验、员工页）都应当走这里，而不是自己
        假设形状。**那条假设正是当初漏掉的地方。**
        """
        record = self.tenant(tenant_id) or {}
        entries = record.get("employees")
        if not isinstance(entries, list):
            return []
        ids: list[str] = []
        for entry in entries:
            if isinstance(entry, str):
                # 兼容裸 id 形态：历史上登记表就是这么写的，
                # 而不是"另一种真相"。读法统一在这里，别处不再分支。
                ids.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("employee_id"), str):
                ids.append(entry["employee_id"])
        return ids

    def tenant_allows(self, tenant_id: str, *, factory_id, actor_id, employee_id) -> bool:
        """核实 tenant / factory / actor / employee 的归属链，全部一致才放行。"""
        record = self.tenant(tenant_id)
        if record is None:
            return False
        if factory_id is not None and factory_id not in record.get("factories", []):
            return False
        if actor_id is not None and actor_id not in record.get("actors", []):
            return False
        if employee_id is not None and employee_id not in self.employee_ids(tenant_id):
            return False
        return True

    def tenant_max_data_class(self, tenant_id: str) -> str | None:
        record = self.tenant(tenant_id)
        if record is None:
            return None
        value = record.get("max_data_class")
        return value if value in DATA_CLASSES else None

    def tenant_delegated_scopes(self, tenant_id: str) -> list[str]:
        record = self.tenant(tenant_id) or {}
        scopes = record.get("delegated_scopes")
        return list(scopes) if isinstance(scopes, list) else []

    # ---- 工具 -------------------------------------------------------------

    def tool(self, tool_id: str | None) -> dict[str, Any] | None:
        if not tool_id:
            return None
        return self._section("tools").get(tool_id)

    # ---- 数据等级 ---------------------------------------------------------

    def known_data_class(self, data_class: str | None) -> bool:
        declared = self._doc.get("data_classes")
        known = declared if isinstance(declared, list) else list(DATA_CLASSES)
        return data_class in known

    # ---- 模型后端 ---------------------------------------------------------

    def model_backend(self, backend: str | None) -> dict[str, Any] | None:
        if not backend:
            return None
        return self._section("model_backends").get(backend)

    def model(self, model_id: str | None) -> dict[str, Any] | None:
        if not model_id:
            return None
        return self._section("models").get(model_id)

    # ---- 策略 -------------------------------------------------------------

    def policy(self, policy_version: str | None) -> dict[str, Any] | None:
        if not policy_version:
            return None
        return self._section("policies").get(policy_version)

    def default_policy_version(self) -> str | None:
        policies = self._section("policies")
        return next(iter(policies), None)

    def policy_reason_codes(self, policy_version: str | None) -> dict[str, str]:
        """策略判定可用的原因码目录（`reason_code` 是契约的 `CatalogValue`）。

        **只从策略自己的目录取，不认识就别猜**：返回空 dict 时调用方应当
        fail-closed（拒绝），而不是回退到某个"默认原因码"——
        一个编出来的原因码会让审计看到一句不存在于目录里的判定依据。
        """
        policy = self.policy(policy_version) or {}
        codes = policy.get("reason_codes")
        if not isinstance(codes, dict):
            return {}
        return {key: value for key, value in codes.items() if isinstance(value, str) and value}

    # ---- 决策有限目录 -----------------------------------------------------

    def decision_catalog(self) -> dict[str, Any]:
        return self._section("decision_catalog")

    def decision_adapter(self) -> dict[str, Any]:
        """Jev 适配方式的**冻结点**（上游 §13「Jev 适配方式需 P0 冻结」）。

        返回登记表里的 `decision_adapter` 节点；缺失时返回空 dict，
        由调用方 fail-closed 拒绝（**不退回默认实现**）。
        """
        return self._section("decision_adapter")

    def decision_facts(self) -> dict[str, Any]:
        """交给决策适配器的**只读事实**（上游 §9：decision 允许依赖
        「security 模型接口、批准的规则版本」）。

        **只含登记的元数据，不含任何租户私密数据**——工具目录、策略规则、
        分类规则、模型后端矩阵。需要真实依据的适配器（如 rules）据此判定；
        不需要的（如 mock）忽略它。

        为什么要显式收口在这里：适配器若自己去翻登记表全文，
        就会顺手拿到租户名单等它不该看的东西——**能拿到就会有人用**。
        """
        from . import classification as classification_module  # noqa: F401 - 保持依赖显式

        policies = self._section("policies")
        return {
            "tools": self._section("tools"),
            "policies": policies,
            "policy": next(iter(policies.values()), {}),
            # 分类规则走**已有的**读取方法，不自己翻 `p1` 节点——
            # 形状的读法只应有一处
            "classification": {
                "rule_version": self.classification_rule_version(),
                "derived_origin_rules": self.derived_origin_rules(),
            },
            "model_backends": self._section("model_backends"),
            "data_classes": self._section("data_classes"),
            "catalog": self._section("decision_catalog"),
        }

    # ---- 部署映射 ---------------------------------------------------------

    def deployment(self) -> dict[str, Any]:
        return self._section("runtime_deployment")

    def service_deployment(self, service_id: str) -> dict[str, Any] | None:
        for entry in self.deployment().get("services", []):
            if entry.get("service_id") == service_id:
                return entry
        return None

    def dependency_probe(self, dependency_id: str) -> str | None:
        record = self._section("dependency_probes").get(dependency_id)
        if not isinstance(record, dict):
            return None
        probe = record.get("probe")
        return probe if isinstance(probe, str) else None

    def service_identity(self, service_id: str) -> str | None:
        entry = self.service_deployment(service_id)
        if entry is None:
            return None
        value = entry.get("identity_ref")
        return value if isinstance(value, str) else None

    # ---- P1：分类 / 用途 / 身份 / Vault / 知识 ACL -------------------------

    def _p1(self) -> dict[str, Any]:
        return self._section("p1")

    def classification_rule_version(self) -> str | None:
        record = self._p1().get("classification")
        if not isinstance(record, dict):
            return None
        value = record.get("rule_version")
        return value if isinstance(value, str) else None

    def derived_origin_rules(self) -> dict[str, str]:
        record = self._p1().get("classification")
        if not isinstance(record, dict):
            return {}
        rules = record.get("derived_origin_rules")
        if not isinstance(rules, dict):
            return {}
        return {key: value for key, value in rules.items() if value in DATA_CLASSES}

    def key_material_refs(self) -> list[str]:
        record = self._p1().get("classification")
        if not isinstance(record, dict):
            return []
        refs = record.get("key_material_refs")
        return list(refs) if isinstance(refs, list) else []

    def transformation_target_class(self, rule_ref: str) -> str | None:
        record = self._p1().get("classification")
        if not isinstance(record, dict):
            return None
        rules = record.get("transformation_rules")
        if not isinstance(rules, dict):
            return None
        value = rules.get(rule_ref)
        return value if value in DATA_CLASSES else None

    def purpose_registered(self, purpose: str) -> bool:
        return purpose in self.purpose_scopes(purpose) or purpose in self._p1().get("purposes", {})

    def purpose_scopes(self, purpose: str) -> list[str]:
        record = self._p1().get("purposes", {}).get(purpose)
        if not isinstance(record, dict):
            return []
        scopes = record.get("scopes")
        return list(scopes) if isinstance(scopes, list) else []

    def service_account_registered(self, identity_ref: str) -> bool:
        return identity_ref in self._p1().get("service_accounts", {})

    def service_account_tenants(self, identity_ref: str | None) -> list[str]:
        if not identity_ref:
            return []
        record = self._p1().get("service_accounts", {}).get(identity_ref)
        if not isinstance(record, dict):
            return []
        tenants = record.get("tenant_ids")
        return list(tenants) if isinstance(tenants, list) else []

    def identity_tenant(self, identity_ref: str | None, claimed: str | None) -> str | None:
        """可信身份对应的租户。

        P1.5 Mock 的取法：认证头给出**服务身份**，服务身份声明自己可代表的租户集合；
        如果请求额外声明了具体租户（`X-V3-Tenant`），必须落在该集合内才成立——
        否则"请求自称的租户"就是不可信输入，直接判为不一致。

        生产做法是 SSO 令牌里带 tenant 声明并可验签，待冻结。
        """
        tenants = self.service_account_tenants(identity_ref)
        if not tenants:
            return None
        if claimed:
            return claimed if claimed in tenants else "__not_authorized__"
        return tenants[0] if len(tenants) == 1 else None

    def service_account_allows(self, identity_ref: str | None, *, tenant_id: str, purpose: str) -> bool:
        if not identity_ref:
            return False
        record = self._p1().get("service_accounts", {}).get(identity_ref)
        if not isinstance(record, dict):
            return False
        if tenant_id not in record.get("tenant_ids", []):
            return False
        return purpose in record.get("purposes", [])

    def consumer_allowed(self, consumer_ref: str, *, tenant_id: str, purpose: str) -> bool:
        record = self._p1().get("consumers", {}).get(consumer_ref)
        if not isinstance(record, dict):
            return False
        if tenant_id not in record.get("tenant_ids", []):
            return False
        return purpose in record.get("purposes", [])

    def vault_version(self) -> str | None:
        record = self._p1().get("vault")
        if not isinstance(record, dict):
            return None
        value = record.get("vault_version")
        return value if isinstance(value, str) else None

    def vault_max_ttl_seconds(self) -> int | None:
        record = self._p1().get("vault")
        if not isinstance(record, dict):
            return None
        value = record.get("max_ttl_seconds")
        return value if isinstance(value, int) and value > 0 else None

    def c4_task_allowlist(self, tenant_id: str) -> list[str]:
        record = self._p1().get("c4", {}).get("task_allowlists", {})
        tasks = record.get(tenant_id) if isinstance(record, dict) else None
        return list(tasks) if isinstance(tasks, list) else []

    def c4_approval_exists(self, tenant_id: str, task_id: str) -> bool:
        record = self._p1().get("c4", {}).get("approvals", {})
        tasks = record.get(tenant_id) if isinstance(record, dict) else None
        return isinstance(tasks, list) and task_id in tasks

    def knowledge_acl_version(self) -> str | None:
        record = self._p1().get("knowledge")
        if not isinstance(record, dict):
            return None
        value = record.get("acl_version")
        return value if isinstance(value, str) else None

    def knowledge_documents(self) -> list[dict[str, Any]]:
        record = self._p1().get("knowledge")
        if not isinstance(record, dict):
            return []
        documents = record.get("documents")
        return list(documents) if isinstance(documents, list) else []
