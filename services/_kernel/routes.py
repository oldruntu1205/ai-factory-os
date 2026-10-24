"""契约 §10.1 十五条业务接口的 P0 Mock 实现。

一个刻意的设计取舍：本文件按 **owner** 组织 handler，service.py 只注册属于本
service 的那些路由；因此"请求打到不属于本服务的路径"天然 404，不需要额外鉴权。

所有 handler 遵守的硬约束（契约原文）：
- 202 只表示持久化受理，**不代表业务成功**；业务成功只由权威执行回执驱动。
- 未知 tenant / 工具 / 等级拒绝；C3/C4 只允许本地模型后端。
- 同幂等键同 payload 返回同一 command_id，异 payload 必须 409。
- 时间只输出 UTC ISO8601（Z 结尾），版本用离散整数。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from . import classification as classification_module
from . import edge
from . import egress_audit
from . import errors
from . import events
from . import governance
from . import journal
from . import knowledge
from . import projection_store
from . import transactions
from . import vault
from .http import Route
from .registry import Registry

# Jev Decision Fabric 的适配层（上游 §3「均需实现同一契约」/ §13「适配方式需 P0 冻结」）。
# 决策逻辑**不再写在本模块里**：内核只做编排，实现见 packages/decision/。
from packages.decision import DECISION_KINDS, DecisionError
from packages.decision import resolve_adapter as resolve_decision_adapter

# --------------------------------------------------------------------------
# Mock 存储。原则：**一个事实只有一个真相源**。
#
#  - journal（跨进程、追加式）：Task / Run / Command / Approval / 回执 / 审计 / 回答投影。
#    理由：这些状态会被**多个运行单元**读（例如 manufacturing-core 要确认任务存在、
#    control-plane 要读回答投影），进程内内存必然产生"两个互相矛盾的真相源"。
#  - 进程内索引（_*_INDEX）：只是 journal 的**读缓存**，可随时丢弃重建，不构成独立事实。
#
# 真实存储（每 owner 独立 schema + role + RLS）属 P2/P3；本阶段先把"单一真相源"
# 这条语义立起来，避免把 P1 的绕过（Task 另写一份投影 journal）固化成架构。
# --------------------------------------------------------------------------
_STORE_LOCK = threading.Lock()
_TASK_INDEX: dict[tuple[str, str], dict[str, Any]] = {}          # (tenant, task_id) -> 最新任务
_APPROVAL_INDEX: dict[tuple[str, str], dict[str, Any]] = {}      # (tenant, approval_id) -> 决策
_RUN_INDEX: dict[tuple[str, str], dict[str, Any]] = {}           # (tenant, run_id) -> 最新 run
_COMMAND_INDEX: dict[tuple[str, str], dict[str, Any]] = {}       # (tenant, command_id) -> 最新命令
_CALIBRATION: dict[str, str] = {}                                # mock：存在即可用于置信度


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _find(store_name: str, index: dict, tenant_id: str, record_id: str, id_field: str) -> dict[str, Any] | None:
    """通用读：**journal 是唯一真相源**，读时以它为准。

    为什么不能"内存优先、未命中才回落 journal"：本进程的内存索引在**其它进程**
    更新了同一事实后会变陈旧（实测：测试进程把命令改成 UNKNOWN，transaction-service
    的内存里仍是旧状态，于是守卫读到旧值而放行）。内存只用于单次请求内的临时加速，
    任何跨进程可见的事实都必须每次从 journal 读。
    """
    key = (tenant_id, record_id)
    latest = journal.read_latest(store_name, lambda item: (item["tenant_id"], item[id_field]))
    record = latest.get(key)
    if record is not None:
        index[key] = record
        return record
    return index.get(key)


def _persist(store_name: str, index: dict, record: dict[str, Any], id_field: str) -> None:
    index[(record["tenant_id"], record[id_field])] = record
    journal.append(store_name, record)


def find_task(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    return _find("tasks", _TASK_INDEX, tenant_id, task_id, "task_id")


def persist_task(record: dict[str, Any]) -> None:
    _persist("tasks", _TASK_INDEX, record, "task_id")


def find_task_by_key(tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
    latest = journal.read_latest("tasks", lambda item: (item["tenant_id"], item["task_id"]))
    for record in latest.values():
        if record["tenant_id"] == tenant_id and record["idempotency_key"] == idempotency_key:
            return record
    return None


def find_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    return _find("runs", _RUN_INDEX, tenant_id, run_id, "run_id")


def persist_run(record: dict[str, Any]) -> None:
    _persist("runs", _RUN_INDEX, record, "run_id")


def find_approval(tenant_id: str, approval_id: str) -> dict[str, Any] | None:
    return _find("approvals", _APPROVAL_INDEX, tenant_id, approval_id, "approval_id")


def persist_approval(record: dict[str, Any]) -> None:
    _persist("approvals", _APPROVAL_INDEX, record, "approval_id")


def find_command(tenant_id: str, command_id: str) -> dict[str, Any] | None:
    return _find("commands", _COMMAND_INDEX, tenant_id, command_id, "command_id")


def persist_command(record: dict[str, Any]) -> None:
    _persist("commands", _COMMAND_INDEX, record, "command_id")


def find_command_by_key(tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
    latest = journal.read_latest("commands", lambda item: (item["tenant_id"], item["command_id"]))
    for record in latest.values():
        if record["tenant_id"] == tenant_id and record["idempotency_key"] == idempotency_key:
            return record
    return None


def _canonical_hash(value: Any) -> str:
    """可信服务的 canonical 序列化：键排序 + 紧凑分隔符 + UTF-8。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _echo(context) -> dict[str, Any]:
    """响应里回显公共 Context（契约要求每个业务对象显式持有 tenant 等字段）。"""
    return dict(context.as_dict())


def _port(registry: Registry, service_id: str) -> int | None:
    entry = registry.service_deployment(service_id)
    if entry is None:
        return None
    port = entry.get("port")
    return port if isinstance(port, int) else None


def _call(registry: Registry, service_id: str, method: str, path: str, *, body=None, headers=None):
    """同步调用兄弟服务。网络/超时失败 → 503，不伪造成功。"""
    port = _port(registry, service_id)
    if port is None:
        raise errors.V3Error(
            errors.HTTP_UNAVAILABLE, "DEPENDENCY_NOT_DEPLOYED", "依赖服务未登记部署映射",
            details={"reason_ref": service_id},
        )
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer svc-mock")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"code": "UPSTREAM_ERROR", "message": "上游返回非 JSON"}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise errors.V3Error(
            errors.HTTP_UNAVAILABLE, "DEPENDENCY_UNAVAILABLE", "依赖服务不可用",
            details={"reason_ref": type(exc).__name__},
        ) from exc


# ==========================================================================
# control-plane
# ==========================================================================

def _handler_task_create(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    idempotency_key = body["idempotency_key"]
    input_schema = body.get("input_schema") or {}
    if not isinstance(input_schema, dict):
        raise errors.reject_bad_request("input_schema", "input_schema 必须是 object")

    fingerprint = _canonical_hash({"input_ref": body["input_ref"], "data_class": context.data_class})
    with _STORE_LOCK:
        existing = find_task_by_key(context.tenant_id, idempotency_key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT,
                    errors.IDEMPOTENCY_CONFLICT,
                    "同幂等键携带了不同 payload",
                )
            return 202, _task_view(existing, context)
        task_id = _rid("task")
        record = {
            "tenant_id": context.tenant_id,
            "factory_id": context.factory_id,
            "task_id": task_id,
            "trace_id": context.trace_id,
            "status": "ACCEPTED",
            "fingerprint": fingerprint,
            "idempotency_key": idempotency_key,
            "updated_at": _utc_now(),
            "command_refs": [],
            "result_refs": [],
        }
        # 单一真相源：任务状态只写 journal 一次；其它运行单元（如 manufacturing-core
        # 的只读链路）通过同一个 journal 读，不再另写一份"任务投影"。
        persist_task(record)
    # 受理即返回 202；编排推进由后续阶段的 runtime 触发，不在这里假装完成。
    return 202, _task_view(record, context)


def _commands_of_task(tenant_id: str, task_id: str) -> list[dict[str, Any]]:
    """按任务列出它的命令（**读 journal，不另记一份索引**）。

    ## 为什么这是"读"而不是"写"

    任务归 control-plane，命令归 transaction-service（上游 §13 的 owner 表）。
    任务侧原先有一个 `command_refs` 字段，但**全仓没有任何写入路径**
    （见 `tests/e2e/test_chain_gap_task_command_link.py` 的缺口登记）。

    修复方式有两条，这里选了第二条：

    | 方案 | 问题 |
    |---|---|
    | ① 提命令时往**任务记录**里追加 `command_refs` | **跨 owner 写**：transaction-service 去改 control-plane 的表，违反 §13「严禁跨 owner 直接写表」 |
    | ② **在组装任务视图时读出来**（本实现） | 无跨 owner 写；且天然不会漂移——**没有第二份真相可漂移** |

    第 ② 条还有个附带好处：命令的 `task_id` **本来就在契约里是必填**
    （`command.json` 的 required 含 `task_id`），所以这不是新增事实，
    而是**把已经存在的事实暴露出来**。

    `task_id` 为空的命令（非任务场景产生的命令）不会被归到任何任务——
    那是对的：它确实不属于任何一个任务。
    """
    latest = journal.read_latest("commands", lambda item: (item["tenant_id"], item["command_id"]))
    matched = [
        record
        for record in latest.values()
        if record.get("tenant_id") == tenant_id and record.get("task_id") == task_id
    ]
    # 按创建时间排序，让"第几步"稳定可复现（同一任务下多次重试时尤其重要）
    matched.sort(key=lambda record: (str(record.get("updated_at") or ""), record["command_id"]))
    return matched


def _task_view(record: dict[str, Any], context) -> dict[str, Any]:
    view = _echo(context)
    # 任务侧的 `command_refs` **不再读取任务记录里的那个字段**（它永远是空的），
    # 而是按任务从命令账本里读出来——见 `_commands_of_task` 的说明。
    links = _commands_of_task(context.tenant_id, record["task_id"])
    view.update(
        {
            "task_id": record["task_id"],
            "status": record["status"],
            "status_url": f"/api/v1/tasks/{record['task_id']}",
            "status_source": "control-plane",
            "updated_at": record["updated_at"],
            "command_refs": [link["command_id"] for link in links],
            "result_refs": list(record["result_refs"]),
        }
    )
    return view


def _handler_task_get(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    task_id = request.path_params["task_id"]
    record = find_task(context.tenant_id, task_id)
    if record is None:
        # 不区分"不存在"与"跨租户"，避免泄露对象存在性。
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TASK_NOT_FOUND", "任务不存在或不可见")
    return 200, _task_view(record, context)


def _p3_binding_hash(binding: dict[str, Any]) -> str:
    """批准绑定的**单一**哈希算法。

    必须只有一个实现：绑定建立（control-plane 的 bindings 接口）与决策
    （P0 的 decisions 接口）都拿它算冲突判据。曾经两边各算一份、
    参与字段不同，结果"刚建完绑定立刻做决策"被判成 `APPROVAL_BINDING_CHANGED`——
    同一个绑定算出两个哈希正是这类缺陷的典型症状。

    参与字段：对象、payload 哈希、工具与版本、策略版本、动作范围、期望版本。
    `valid_until` **不参与**：延长有效期是同一绑定的正当更新，不是"换绑"。
    """
    return _canonical_hash(
        {
            "object_id": binding["object_id"],
            "payload_hash": binding["payload_hash"],
            "tool_id": binding["tool_id"],
            "tool_version": binding["tool_version"],
            "action_scope": sorted(binding.get("action_scope") or []),
            "policy_version": binding["policy_version"],
            "expected_version": binding["expected_version"],
        }
    )


def _p3_binding_from_decision(context, body: dict[str, Any]) -> dict[str, Any]:
    """从 P0 批准决策请求构造**契约形状**的绑定（p3-approval.json#/$defs/Binding）。

    为什么要在这里就写成契约形状：P3 的执行前复核要按绑定逐项比对，
    如果批准事实里存的是另一套结构，就会出现"同一个批准、两套解释"，
    也就是上游明令禁止的第二真相源。P0 与 P3 因此共用**同一条**批准事实。
    """
    return {
        "context": context.as_dict(),
        "object_id": body["object_id"],
        "expected_version": body["expected_version"],
        "tool_id": body["tool_id"],
        "tool_version": body["tool_version"],
        "command_type": body.get("command_type") or body["tool_id"],
        "action_scope": list(body["action_scope"]),
        "payload_ref": body.get("payload_ref") or body["object_id"],
        "payload_hash": body["payload_hash"],
        "payload_schema": body.get("payload_schema")
        or {"schema_ref": "task.json", "schema_version": "1.0.0", "schema_hash": "sha256:mock"},
        "normalization_ref": body.get("normalization_ref") or f"norm-{body['payload_hash'][:16]}",
        "policy_version": body["policy_version"],
    }


def _handler_approval_decision(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    approval_id = request.path_params["approval_id"]
    if body["approval_id"] != approval_id:
        raise errors.reject_bad_request("approval_id", "路径 approval_id 与 body 不一致")
    if not isinstance(body["approved"], bool):
        raise errors.reject_bad_request("approved", "approved 必须是布尔值")

    # 工具登记（x-registry=tools）
    if request.registry.tool(body["tool_id"]) is None:
        raise errors.reject_unknown_tool(body["tool_id"])

    binding = _p3_binding_from_decision(context, body)

    # ── 职责分离：**在批准写入时就拦**，不等执行前复核（上游 §7 / P3 SPEC）──
    #
    # P3 SPEC 的复核点表明确列出「批准决定写入」这一行必须校验
    # 「当前批准权限、**职责分离**、绑定、对象/策略、有效期、批准记录 CAS」，
    # 失败即**拒绝**。
    #
    # 2026-10-06 修：此前只在 `revalidate_before_execution`（执行前复核）里判
    # `duties_separated`，于是**发起人可以自己批准自己** —— 批准事实照样成立，
    # 只是等到执行时才被拒。后果不是"多一层保护"，而是：
    #   ① 审计台账里留下一条**本就不该存在的批准事实**（事实追加不可变，删不掉）；
    #   ② 界面会显示"已批准"，而执行时被拒 —— 正是本项目一贯要防的
    #      「显示成功而实际不行」。
    #
    # ## 判据取谁：**已存在的待批准记录里的 binding.context.actor_id**
    #
    # 我第一版用**本次请求**构造出来的 `binding["context"]["actor_id"]` 去比，
    # 结果是审批人跟自己比 ⇒ **永远相等** ⇒ 15 条既有用例全红。原因很具体：
    # `_p3_binding_from_decision` 用**请求自己的 context** 构造绑定，
    # 所以在"批准"这个动作里，那个字段就是**审批人**，不是发起人。
    #
    # 正确的"发起人"在**建立待批准时**写入的绑定里——它来自 Tool Gateway 的
    # 规范化结果（SPEC 原文：「**binding.context.actor_id 是受委托执行人**」）。
    #
    # 兼容性：P0 的决策接口允许"**直接决定**"（不经 bindings 接口，记录尚不存在）。
    # 那种路径没有可比的发起人，因此**不拦**——它本来就没有"发起"这一步。
    # 一旦记录存在（正常流程：先 bindings 再 decisions），就必须分离。
    with _STORE_LOCK:
        existing = find_approval(context.tenant_id, approval_id)
        stored_requester = (
            ((existing or {}).get("binding") or {}).get("context", {}).get("actor_id")
        )
        approver_ref = body["actor_id"]
        if stored_requester and approver_ref and stored_requester == approver_ref:
            raise errors.V3Error(
                errors.HTTP_FORBIDDEN,
                "DUTIES_NOT_SEPARATED",
                "审批人不得是发起人（上游 §7 职责分离；P3 SPEC 要求批准写入时即拒绝）",
                details={"denied_field": "approver_ref"},
            )

        # 冲突判据仍在**绑定**上：绑定变了就是"批准已绑其他 payload"，需重新审批。
        binding_hash = _p3_binding_hash(binding)

        if existing is not None and existing.get("binding_hash") not in (None, binding_hash):
            raise errors.V3Error(
                errors.HTTP_CONFLICT,
                "APPROVAL_BINDING_CHANGED",
                "批准已绑定其他 payload/版本，需重新审批",
            )
        if existing is not None and existing.get("approved") is True:
            # 契约 `append_only_fact` + `identical_decision_replay: same_fact`：
            # 已经成立的决定**不可改判**——重复提交"同一决定"返回**同一事实**（幂等），
            # 提交"相反决定"返回 409。不能靠 record_version+1 覆盖历史决定。
            if body["approved"] is True:
                record = existing
            else:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT, "APPROVAL_ALREADY_GRANTED",
                    "批准已成立，不能改判为否决（事实追加不可变）",
                    details={"object_ref": "approval_id"},
                )
        elif existing is not None and existing.get("approved") is False:
            if body["approved"] is False:
                record = existing
            else:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT, "APPROVAL_ALREADY_DENIED",
                    "批准已被否决，不能改判为通过（事实追加不可变）",
                    details={"object_ref": "approval_id"},
                )
        elif existing is not None:
            record = {
                **existing,
                "approved": body["approved"],
                "approver_ref": body["actor_id"],
                # 决策时必须补上这两项：早先的"绑定"记录里它们是 None，
                # 直接沿用会把 null 泄漏进响应与事实（实测到的契约违规）。
                "decision_ref": existing.get("decision_ref") or f"approval-decision-{approval_id}",
                "decided_at": _utc_now(),
                "valid_until": body["valid_until"],
                "binding": existing.get("binding") or binding,
                "binding_hash": existing.get("binding_hash") or binding_hash,
                "record_version": int(existing.get("record_version", 0)) + 1,
                "updated_at": _utc_now(),
            }
        else:
            record = {
                "tenant_id": context.tenant_id,
                "factory_id": context.factory_id,
                "approval_id": approval_id,
                "trace_id": context.trace_id,
                "approved": body["approved"],
                "approver_ref": body["actor_id"],
                "binding": binding,
                "binding_hash": binding_hash,
                "policy_evaluation_ref": body.get("policy_evaluation_ref")
                or f"policy-eval-{approval_id}",
                "decision_ref": f"approval-decision-{approval_id}",
                "decided_at": _utc_now(),
                "valid_from": _utc_now(),
                "valid_until": body["valid_until"],
                "record_version": 1,
                "updated_at": _utc_now(),
            }
        persist_approval(record)

    view = _echo(context)
    view.update(
        {
            "approval_id": approval_id,
            "approved": record["approved"],
            "record_version": record["record_version"],
            "decision_ref": record["decision_ref"],
        }
    )
    return 200, view


def _handler_policy_evaluate(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    tool = request.registry.tool(body["tool_id"])
    if tool is None:
        raise errors.reject_unknown_tool(body["tool_id"])
    if tool.get("tool_version") != body["tool_version"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "TOOL_VERSION_CONFLICT", "工具版本与登记不一致",
            details={"reason_ref": "tool_version"},
        )

    policy_version = request.registry.default_policy_version()
    policy = request.registry.policy(policy_version)
    if policy is None:
        raise errors.reject_registry_unavailable("policy-registry")

    risk_class = tool.get("risk_class")
    rule = policy.get("risk_rules", {}).get(risk_class)
    if not isinstance(rule, dict):
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "RISK_CLASS_UNKNOWN", "工具风险等级未在策略中登记",
            details={"reason_ref": "unknown_risk_class"},
        )

    # 职责分离：批准权不等于执行权；这里只判定是否**需要**批准。
    delegated = set(body.get("delegated_scopes") or [])
    required = set(tool.get("required_scopes") or [])
    missing = sorted(required - delegated)
    allowed = rule.get("auto_execute") is True and not missing
    approval_required = rule.get("requires_approval") is True or bool(missing)

    # 原因码必须**取自策略自己的有限目录**（契约：`reason_code` 是 CatalogValue）。
    #
    # 2026-10-05 修：原先写的是 `"reason-demo" if allowed else "reason-demo"` ——
    # 两个分支同一个值，等于**没有区分**，却长得像区分了。而 `reason-demo`
    # 属于 `decision_catalog`，是**另一个决策类型**的目录：策略判定借用它，
    # 会让审计分不清一条原因码出自哪个判定，也会让一边扩目录时意外放宽另一边。
    #
    # 目录缺失 ⇒ fail-closed 拒绝（不给"默认原因码"）：编一个原因码出来，
    # 审计会看到一句目录里并不存在的判定依据。
    reason_codes = request.registry.policy_reason_codes(policy_version)
    if not reason_codes:
        raise errors.reject_registry_unavailable("policy-reason-codes")
    if allowed:
        reason_code = reason_codes.get("allowed")
    elif approval_required:
        reason_code = reason_codes.get("approval_required")
    else:
        reason_code = reason_codes.get("denied")
    if not reason_code:
        raise errors.reject_registry_unavailable("policy-reason-codes")

    view = _echo(context)
    view.update(
        {
            "allowed": bool(allowed),
            "approval_required": bool(approval_required),
            "risk_class": risk_class,
            "policy_version": policy_version,
            "evaluation_ref": _rid("policy-eval"),
            "approval_ref": None,
            "reason_code": reason_code,
            "valid_until": body["deadline_at"],
        }
    )
    if allowed and approval_required:
        # 契约 allOf 要求 allowed 且 approval_required 时 approval_ref 必须存在。
        view["approval_ref"] = f"approval-{body['object_id']}"
    return 200, view


# ==========================================================================
# decision-service
# ==========================================================================

def decision_adapter_for(registry: Registry):
    """按登记表解析 Jev 适配器。

    独立成函数（而不是写在处理器里）是为了让**测试能直接验解析的 fail-closed 行为**：
    未配置、配置为空、配置了未登记的实现 id —— 三种都必须拒绝，
    而不是退回某个默认实现。
    """
    return resolve_decision_adapter(registry.decision_adapter())


def _handler_decision(request) -> tuple[int, dict[str, Any]]:
    """Jev Decision Fabric 的**接口**（上游 §3 / §10.1）。

    ## 内核在这里的职责（2026-10-05 起）

    内核**不做决策**，只做四件事：校验目录与身份、解析适配器、调用它、组装回执。

    抽取前，决策逻辑（取目录首项、`confidence` 硬编码 0.71）**写在本函数里**，
    于是换任何一个真实 Jev 都得改内核——而上游 §3 明写
    「真实 Jev 产品、兼容实现或本地决策模型**均需实现同一契约**」，
    §13 要求「Jev 适配方式需 P0 冻结」。适配方式已冻结在登记表
    `decision_adapter`，实现见 `packages/decision/`。
    """
    context = request.context
    body = request.body
    catalog = request.registry.decision_catalog()
    if not catalog:
        raise errors.reject_registry_unavailable("decision-catalog")
    if catalog.get("catalog_version") != body["catalog_version"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "CATALOG_VERSION_CONFLICT", "决策目录版本不匹配",
            details={"reason_ref": "catalog_version"},
        )

    # 决策类型：省缺按 Route（加字段是兼容升级，P0 之前的调用方只做任务路由）。
    decision_kind = body.get("decision_kind") or "Route"
    if decision_kind not in DECISION_KINDS:
        raise errors.V3Error(
            errors.HTTP_UNPROCESSABLE, "DECISION_KIND_UNKNOWN",
            f"未知决策类型 {decision_kind!r}",
            details={"object_ref": "decision_kind"},
        )

    # 适配器由登记表冻结；**未登记即拒绝**（不退回 mock）
    try:
        adapter = decision_adapter_for(request.registry)
    except DecisionError as exc:
        raise errors.V3Error(
            errors.HTTP_UNAVAILABLE, exc.code, exc.message, details=exc.details
        ) from exc

    calibration_version = _CALIBRATION.get(body.get("input_ref", ""))
    try:
        outcome = adapter.decide(
            kind=decision_kind,
            request=body,
            catalog=catalog,
            calibration_version=calibration_version,
            # **只读事实**：工具目录/策略规则/分类规则/后端矩阵。
            # 不含租户私密数据——适配器需要真实依据时用它，不需要就忽略。
            facts=request.registry.decision_facts(),
        )
    except DecisionError as exc:
        # 未实现的类型 → 422（请求本身可懂但当前不可满足）；
        # 其余（目录不完整等）→ 503（依赖不可用）。
        status = (
            errors.HTTP_UNPROCESSABLE
            if exc.code in ("DECISION_KIND_NOT_IMPLEMENTED", "DECISION_KIND_UNKNOWN")
            else errors.HTTP_UNAVAILABLE
        )
        raise errors.V3Error(status, exc.code, exc.message, details=exc.details) from exc

    view = _echo(context)
    view.update({"decision_id": _rid("decision"), **outcome.as_view()})
    return 200, view


# ==========================================================================
# security-gateway
# ==========================================================================

def _handler_model_invocation(request) -> tuple[int, dict[str, Any]]:
    started_at = time.perf_counter()
    context = request.context
    body = request.body
    data_class = context.data_class
    backend = body["model_backend"]

    backend_record = request.registry.model_backend(backend)
    if backend_record is None:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "MODEL_BACKEND_NOT_REGISTERED", "模型后端未登记",
            details={"denied_field": "model_backend"},
        )

    # C3/C4 只允许本地（契约 allOf + x-admission-policy）。
    if data_class in ("C3", "C4") and backend != "local":
        raise errors.reject_sensitive_nonlocal()

    allowed_classes = set(backend_record.get("allowed_data_classes") or [])
    if data_class not in allowed_classes:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "DATA_CLASS_NOT_ALLOWED_FOR_BACKEND", "该等级不允许发往此模型后端",
            details={"denied_field": "model_backend"},
        )

    model = request.registry.model(body["model_id"])
    if model is None:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "MODEL_NOT_REGISTERED", "模型未登记", details={"object_ref": body["model_id"]}
        )
    # 客户端自填的 backend 不能证明实际后端；必须与登记元数据一致。
    if model.get("model_backend") != backend:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "MODEL_BACKEND_MISMATCH", "自填后端与登记模型元数据不一致",
            details={"denied_field": "model_backend"},
        )
    if model.get("model_version") != body["model_version"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "MODEL_VERSION_CONFLICT", "模型版本与登记不一致",
            details={"reason_ref": "model_version"},
        )

    # 本地故障 → 排队/人工，**禁止**自动降级到公网。
    if not backend_record.get("reachable", False):
        raise errors.V3Error(
            errors.HTTP_UNAVAILABLE, "MODEL_BACKEND_UNAVAILABLE", "模型后端不可用，按排队或人工处理",
            retryable=False,
            details={"denied_field": "model_backend"},
        )

    view = _echo(context)
    view.update(
        {
            "invocation_id": _rid("invocation"),
            "model_version": model["model_version"],
            "output_ref": f"model-output-{uuid.uuid4().hex[:12]}",
            "output_schema": body["output_schema"],
        }
    )
    # 指标事实：记下**受理**与**治理判定路径耗时**。
    #
    # 刻意不记"模型生成耗时"——本端点并不调用模型（只做准入判定）。
    # 把一个数字记成生成耗时，会让运维按错误的容量模型做决策。
    journal.append(
        "model-invocations",
        {
            "tenant_id": context.tenant_id,
            "trace_id": context.trace_id,
            "invocation_id": view["invocation_id"],
            "model_id": body["model_id"],
            "model_version": model["model_version"],
            "model_backend": backend,
            "output_ref": view["output_ref"],
            "decision_seconds": round(time.perf_counter() - started_at, 6),
            "invocation_performed": False,
            "note": "未发生真实模型推理；decision_seconds 是治理判定路径耗时",
            "recorded_at": _utc_now(),
        },
    )
    return 200, view


# ==========================================================================
# manufacturing-core
# ==========================================================================

def _handler_context_query(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    if not body["object_refs"]:
        raise errors.reject_bad_request("object_refs", "object_refs 不能为空")

    now = _utc_now()
    snapshots = [
        {
            "source_system": "pmc-mock",
            "source_id": object_ref,
            "source_version": "1",
            "occurred_at": now,
            "received_at": now,
            "data_class": context.data_class,
            "tenant_id": context.tenant_id,
            "quality_status": "quality-demo",
            "snapshot_ref": f"snapshot-{object_ref}",
            "valid_until": context.deadline_at,
        }
        for object_ref in body["object_refs"]
    ]
    view = _echo(context)
    view["snapshots"] = snapshots
    return 200, view


# ==========================================================================
# tool-gateway
# ==========================================================================

def _handler_tool_request(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    tool = request.registry.tool(body["tool_id"])
    if tool is None:
        raise errors.reject_unknown_tool(body["tool_id"])
    if tool.get("tool_version") != body["tool_version"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "TOOL_VERSION_CONFLICT", "工具版本与登记不一致",
            details={"reason_ref": "tool_version"},
        )

    if tool.get("read_write") == "read":
        view = _echo(context)
        view.update(
            {
                "tool_id": tool["tool_id"],
                "tool_version": tool["tool_version"],
                "read_write": "read",
                "output_ref": f"read-output-{uuid.uuid4().hex[:12]}",
                "command": None,
            }
        )
        return 200, view

    # 写路径：先算出可信 payload（工具网关必须先规范化），再请 control-plane 做权威 Policy 判定。
    normalized_payload = {"schedule_version": body["expected_version"] + 1}
    # 契约 §10.2：canonical payload_hash 由可信服务计算，绑定工具版本、对象、
    # tenant/actor、approval 与动作。transaction-service 是落库点，因此由它复算；
    # 这里把"策略判定所依据的"哈希一并传下去，让执行侧能核对两处口径一致。
    policy_payload_hash = _canonical_hash(
        {"input_ref": body["input_ref"], "schema": body["input_schema"], "payload": normalized_payload}
    )
    status, policy = _call(
        request.registry,
        "control-plane",
        "POST",
        "/internal/v1/policy/evaluate",
        body={
            **_echo(context),
            "tool_id": tool["tool_id"],
            "tool_version": tool["tool_version"],
            "action": body["input_ref"],
            "object_id": body["input_ref"],
            "payload_hash": policy_payload_hash,
            "expected_version": body["expected_version"],
            "payload_version": "1.0.0",
            "normalized_payload_ref": body["input_ref"],
            "delegated_scopes": request.registry.tenant_delegated_scopes(context.tenant_id),
        },
    )
    if status != 200:
        raise errors.V3Error(
            status, policy.get("code", "POLICY_UNAVAILABLE"), "策略判定未通过",
            details={"reason_ref": "control-plane"},
        )

    status, command = _call(
        request.registry,
        "transaction-service",
        "POST",
        "/internal/v1/commands",
        body={
            **_echo(context),
            "command_type": "pmc.dispatch.publish",
            "tool_id": tool["tool_id"],
            "tool_version": tool["tool_version"],
            "object_id": body["input_ref"],
            "payload_version": "1.0.0",
            "payload": normalized_payload,
            "idempotency_key": body["idempotency_key"],
            "expected_version": body["expected_version"],
            "policy_version": policy["policy_version"],
            "policy_evaluation_ref": policy["evaluation_ref"],
            "approval_ref": policy.get("approval_ref"),
            "policy_payload_hash": policy_payload_hash,
        },
    )
    if status not in (200, 202):
        raise errors.V3Error(
            status, command.get("code", "COMMAND_REJECTED"), "命令受理失败",
            details={"reason_ref": "transaction-service"},
        )

    view = _echo(context)
    view.update(
        {
            "tool_id": tool["tool_id"],
            "tool_version": tool["tool_version"],
            "read_write": "write",
            "output_ref": None,
            "command": command,
        }
    )
    return 202, view


def _handler_tool_execution(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    # 契约：仅 transaction-service 的受限身份可调用。
    caller = request.headers.get("x-v3-caller")
    if caller != "transaction-service":
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "CALLER_NOT_ALLOWED", "执行入口只接受受限服务身份",
            details={"object_ref": "tool-executions"},
        )

    command = body["command"]
    if command.get("tenant_id") != context.tenant_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "NESTED_TENANT_MISMATCH", "命令租户与外层不一致",
            details={"denied_field": "tenant_id"},
        )
    # 执行时复核授权：AUTHORIZED 不是永续许可。
    policy_version = command.get("policy_version")
    if request.registry.policy(policy_version) is None:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "POLICY_VERSION_STALE", "策略版本已失效，需重新判定",
            details={"reason_ref": "policy_version"},
        )
    if not body.get("execution_permit_ref"):
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "EXECUTION_PERMIT_REQUIRED", "缺少执行许可凭证",
            details={"object_ref": "execution_permit_ref"},
        )

    # Mock adapter：权威源回执。同幂等键返回同一回执，不重复产生副作用。
    command_id = body["command_id"]
    receipt_ref = _ensure_receipt(context.tenant_id, command_id, command)
    view = _echo(context)
    view.update(
        {
            "command_id": command_id,
            "status": "SUCCEEDED",
            "receipt_refs": [receipt_ref],
            "source_system": "pmc-mock",
            "updated_at": _utc_now(),
        }
    )
    return 200, view





def _ensure_receipt(tenant_id: str, command_id: str, command: dict[str, Any]) -> str:
    """Mock 权威源（adapter）。

    幂等：同一 (tenant, command_id) 只产生一个回执——回执写进 journal，跨进程可见，
    由 transaction-service 的对账线程回流到命令状态。这里**不直接改命令状态**，
    因为"命令成功"只能由 Transaction 依据权威回执判定。
    """
    receipt_ref = f"pmc-receipt-{command_id}"
    existing = [
        item
        for item in journal.read_all("receipts")
        if item.get("tenant_id") == tenant_id and item.get("command_id") == command_id
    ]
    if existing:
        return existing[0]["receipt_ref"]
    journal.append(
        "receipts",
        {
            "tenant_id": tenant_id,
            "command_id": command_id,
            "receipt_ref": receipt_ref,
            "source_system": "pmc-mock",
            "tool_id": command.get("tool_id"),
            "object_id": command.get("object_id"),
            "recorded_at": _utc_now(),
        },
    )
    return receipt_ref


# ==========================================================================
# transaction-service
# ==========================================================================

def _handler_command_create(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body

    # 契约 `command.json` 声明了 22 个必填字段。**必须先显式校验**：
    # 此前直接取 body["command_type"] 等，缺字段抛 KeyError → 被兜底成
    # 503 INTERNAL_ERROR。把"客户端请求不合法"报成 5xx，
    # 会让调用方以为服务坏了、也让排障的人去查错方向。
    #
    # 注意这里只列**调用方必须自带**的字段：
    # `payload_version` / `payload_hash` / `approval_ref` / `policy_payload_hash`
    # 在编排链路里由上游产出或允许缺省（`body.get`），把它们算作必填
    # 会误伤正常路径——实测把 P0/P1.5 的建命令用例打红了一片。
    missing = [
        field
        for field in (
            "command_type", "tool_id", "tool_version", "object_id",
            "payload", "idempotency_key", "expected_version", "policy_version",
        )
        if body.get(field) is None
    ]
    if missing:
        raise errors.reject_bad_request(
            missing[0], f"缺少必填字段（契约 command.json）：{', '.join(missing)}"
        )

    tool = request.registry.tool(body["tool_id"])
    if tool is None:
        raise errors.reject_unknown_tool(body["tool_id"])

    # 可信服务重算 canonical hash，不采信调用方提供的值。
    trusted_hash = _canonical_hash(
        {
            "command_type": body["command_type"],
            "tool_id": body["tool_id"],
            "tool_version": body["tool_version"],
            "object_id": body["object_id"],
            "payload": body["payload"],
            "payload_version": body["payload_version"],
            "tenant_id": context.tenant_id,
            "actor_id": context.actor_id,
            "approval_ref": body.get("approval_ref"),
            "policy_version": body["policy_version"],
        }
    )
    supplied = body.get("policy_payload_hash")
    if supplied:
        # 契约：策略判定所依据的 payload 与落库命令必须是同一份内容。
        policy_hash = _canonical_hash(
            {
                "input_ref": body["object_id"],
                "schema": {
                    "schema_ref": "command.json",
                    "schema_version": "1.0.0",
                    "schema_hash": "sha256:mock",
                },
                "payload": body["payload"],
            }
        )
        if supplied != policy_hash:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "POLICY_PAYLOAD_MISMATCH", "评估 payload 与落库 payload 不一致",
                details={"object_ref": "policy_payload_hash"},
            )

    if request.registry.policy(body["policy_version"]) is None:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "POLICY_VERSION_STALE", "策略版本未登记",
            details={"reason_ref": "policy_version"},
        )

    with _STORE_LOCK:
        existing = find_command_by_key(context.tenant_id, body["idempotency_key"])
        if existing is not None:
            if existing["payload_hash"] != trusted_hash:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT,
                    errors.IDEMPOTENCY_CONFLICT,
                    "同幂等键携带了不同 payload",
                )
            return 202, _command_view(existing, context)

        command_id = _rid("command")
        record = {
            "command_id": command_id,
            "tenant_id": context.tenant_id,
            "idempotency_key": body["idempotency_key"],
            "payload_hash": trusted_hash,
            "status": "ACCEPTED",
            "approved": body.get("approval_ref") is not None,
            "tool_id": body["tool_id"],
            "object_id": body["object_id"],
            "payload_version": body["payload_version"],
            "expected_version": body["expected_version"],
            "policy_version": body["policy_version"],
            "command": {**{key: body[key] for key in body}, "payload_hash": trusted_hash},
            "source_system": None,
            "receipt_refs": [],
            "reconciliation_ref": None,
            "updated_at": _utc_now(),
        }

    # 需要批准但还没批准 → 停在 ACCEPTED，等批准事件；不假装能执行。
    tool_risk = tool.get("risk_class")
    if tool_risk in ("R2", "R3") and not record["approved"]:
        persist_command(record)
        return 202, _command_view(record, context)

    record["status"] = "AUTHORIZED"
    record["updated_at"] = _utc_now()
    persist_command(record)
    return 202, _command_view(record, context)


def _command_view(record: dict[str, Any], context) -> dict[str, Any]:
    """命令的**外部视图**（`GET /internal/v1/commands/{id}`）。

    形状由 `packages/contracts/schemas/command-status.json` 规定，
    且该契约是 **`additionalProperties: false`**——所以这里**只能有**契约列出的字段，
    多想返回一个都不行（实测被 `test_responses_conform_to_contract_schemas` 当场抓住）。

    ## 这个函数曾经会 500 —— 根因是"命令记录有两种形状"

    联调实测（2026-10-05）：真实跑通一条 P3 交易链路后调这个接口，
    直接 `KeyError: 'source_system'`，连接被掐断（`RemoteDisconnected`）。

    原因是记录由**两条不同的路径**创建，字段集不一样：

    | 来源 | 有 | 没有 |
    | --- | --- | --- |
    | P0/P1.15 命令 | `source_system`、`receipt_refs`、`command` | `binding`、`authority_receipt`、`lease` |
    | P3 受控交易 | `binding`、`authority_receipt`、`lease`、`object_phase` | `source_system`、`receipt_refs` |

    硬取其中一种形状的键，碰到另一种就崩。修法是全部用 `.get()`。

    **P3 的权威事实（回执、过账状态）不在这里暴露**——那是
    `p3-command.json#/$defs/View` 的职责，由受控交易路径的视图接口返回。
    把两者混进同一个响应会同时违反两份契约（本函数曾经这么错过一次）。
    """
    view = _echo(context)
    view.update(
        {
            "command_id": record["command_id"],
            "status": record["status"],
            "status_source": "transaction-service",
            "updated_at": record["updated_at"],
            "source_system": record.get("source_system"),
            "receipt_refs": list(record.get("receipt_refs") or []),
            "reconciliation_ref": record.get("reconciliation_ref"),
        }
    )
    return view


def _handler_command_get(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    command_id = request.path_params["command_id"]
    record = find_command(context.tenant_id, command_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "COMMAND_NOT_FOUND", "命令不存在或不可见")
    return 200, _command_view(record, context)


# ---- 供对账/测试使用（不暴露为 HTTP 接口：契约接口目录是封闭的）----------

def advance_command(tenant_id: str, command_id: str, *, status: str) -> dict[str, Any] | None:
    record = find_command(tenant_id, command_id)
    if record is None:
        return None
    record["status"] = status
    record["updated_at"] = _utc_now()
    persist_command(record)
    return record


def mark_receipt(tenant_id: str, command_id: str, receipt_ref: str, source_system: str) -> None:
    record = find_command(tenant_id, command_id)
    if record is None:
        return
    record["status"] = "SUCCEEDED"
    record["receipt_refs"] = [receipt_ref]
    record["source_system"] = source_system
    record["updated_at"] = _utc_now()
    persist_command(record)


# ==========================================================================
# hermes-runtime
# ==========================================================================

def _run_view(record: dict[str, Any], context) -> dict[str, Any]:
    view = _echo(context)
    view.update(
        {
            "run_id": record["run_id"],
            "status": record["status"],
            "status_source": "hermes-runtime",
            "updated_at": record["updated_at"],
            "checkpoint": record["checkpoint"],
            # ⚠️ **run 侧的 `command_refs` 仍然是空的**——与任务侧不同，这里不是"漏了写"：
            # 命令记录只有 `task_id`，**没有 `run_id`**（见 `command.json` 的 required），
            # 所以"某个 run 产生了哪些命令"这件事**当前无法从任何账本推导**。
            # 要让 run 也能下钻，需要一条契约决定：命令是否登记 run_id。
            # 那是**另一条链路**，不在本次"任务↔命令"范围内——故如实留空，不假装。
            "command_refs": list(record["command_refs"]),
        }
    )
    return view


def _handler_run_create(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    run_id = _rid("run")
    record = {
        "tenant_id": context.tenant_id,
        "factory_id": context.factory_id,
        "task_id": context.task_id,
        "trace_id": context.trace_id,
        "run_id": run_id,
        "status": "RUNNING",
        "updated_at": _utc_now(),
        "checkpoint": None,
        "command_refs": [],
        "runtime_version": body["runtime_version"],
    }
    # 单一真相源：Run 状态同样只写 journal（hermes-runtime 拥有），
    # 其它 owner 需要时通过 journal/契约读，不复制一份状态。
    persist_run(record)
    return 202, _run_view(record, context)


def _handler_run_resume(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    run_id = request.path_params["run_id"]
    record = find_run(context.tenant_id, run_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "RUN_NOT_FOUND", "运行不存在或不可见")
    checkpoint = record["checkpoint"]
    current_version = checkpoint["version"] if checkpoint else 0
    if body["expected_checkpoint_version"] != current_version:
        # CAS 失败 → 409；不覆盖更新，不重放已成功步骤。
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "CHECKPOINT_VERSION_CONFLICT", "检查点版本冲突",
            details={"expected_version": body["expected_checkpoint_version"], "actual_version": current_version},
        )
    record["checkpoint"] = {
        **_echo(context),
        "run_id": run_id,
        "version": current_version + 1,
        "program_position": "step-demo",
        "context_refs": [body["checkpoint_ref"]],
        "pending_command_ids": [],
        "completed_tool_receipt_refs": [],
        "model_version": None,
        "policy_version": request.registry.default_policy_version(),
    }
    record["updated_at"] = _utc_now()
    persist_run(record)
    return 200, _run_view(record, context)


# ==========================================================================
# edge
# ==========================================================================

def _handler_edge_decision(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    if body["model_backend"] != "local":
        raise errors.reject_sensitive_nonlocal()
    # P0 骨架不做现场推理；影子模式必须无副作用，这里显式返回空回执。
    view = _echo(context)
    view.update(
        {
            "edge_id": body["edge_id"],
            "mode": body["mode"],
            "decision": {
                **_echo(context),
                "decision_id": _rid("edge-decision"),
                # Edge 的本地判定**也是** DecisionResult 形状（上游 §8：Edge 与 Jev
                # 「可复用 Schema、校准和评测规范，但不共享执行权限」），
                # 所以它必须如实声明自己属于哪一类决策。
                # 现场这个节点做的是**门禁**（Guard 判定顺序、互锁、置信度门禁），
                # 故为 `Gate`——不是 `Route`：Edge 不做任务分流。
                "decision_kind": "Gate",
                "route": "route-demo",
                "action": "action-demo",
                "category": "category-demo",
                "confidence": None,
                "model_version": body["model_version"],
                "calibration_version": body["calibration_version"],
                "rule_version": "rule-demo",
                "reason_code": "reason-demo",
                "evidence_refs": [body["input_ref"]],
                "fallback": {"route": "fallback-demo", "reason_code": "reason-demo"},
            },
            "action_executed": False,
            "receipt_refs": [],
        }
    )
    return 200, view


def _handler_edge_release_activate(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    body = request.body
    # 发布门禁：缺任一证据或互锁未验证即拒绝，且不得降级放行。
    failures = []
    if not body["schema_frozen"]:
        failures.append("schema_not_frozen")
    if not body["offline_calibration_passed"]:
        failures.append("offline_calibration_missing")
    if not body["shadow_coverage_passed"]:
        failures.append("shadow_coverage_insufficient")
    if not body["interlock_verified"]:
        failures.append("interlock_not_verified")
    if int(body["shadow_days"]) < 14:
        failures.append("shadow_days_below_minimum")
    if not body["gate_evidence_refs"]:
        failures.append("gate_evidence_missing")
    if failures:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "EDGE_RELEASE_GATE_FAILED", "Edge 发布门禁未通过",
            details={"reason_ref": failures[0]},
        )
    view = _echo(context)
    view.update(
        {
            "edge_id": body["edge_id"],
            "release_id": body["release_id"],
            "status": "APPROVED",
            "evidence_refs": list(body["gate_evidence_refs"]),
            "updated_at": _utc_now(),
        }
    )
    return 200, view


# ==========================================================================
# P1：安全与只读闭环（契约 openapi/p1.openapi.json）
#
# 全部 6 条接口挂在既有 L2（/internal/v1）与 L0（/api/v1）层，**不新增接口层**。
# P1 接口是"判定/编排"，不是旁路模型调用入口：真正的模型调用仍只经
# P0 的 /internal/v1/model-invocations，且每次重做治理（见 §5 顺序 7）。
# ==========================================================================

def _p1_context(request):
    """P1 的 Context 要求：tenant 三方一致 + factory/task 必须非空（工厂只读链路）。"""
    context = request.context
    body = request.body
    if body.get("tenant_id") != context.tenant_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "NESTED_TENANT_MISMATCH", "顶层租户与 Context 不一致",
            details={"denied_field": "tenant_id"},
        )
    nested = body.get("context")
    if not isinstance(nested, dict):
        raise errors.reject_bad_request("context", "缺少 context 对象")
    if nested.get("tenant_id") != context.tenant_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "NESTED_TENANT_MISMATCH", "嵌套 Context 租户与外层不一致",
            details={"denied_field": "tenant_id"},
        )
    if not nested.get("factory_id"):
        raise errors.reject_bad_request("factory_id", "工厂只读链路要求 factory_id 非空")
    if not nested.get("task_id"):
        raise errors.reject_bad_request("task_id", "工厂只读链路要求 task_id 非空")
    return context


def _p1_service_identity(request) -> str | None:
    return request.headers.get("x-v3-identity")


def _p1_classify_inputs(request) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从请求取"自报输入"，并按登记表补上可信派生来源。

    P1 边界：真实实现要解析引用指向的实际内容（正文/OCR/附件/RAG chunk…）
    —— 属 P2 的 adapter 与对象存储；本次用登记规则把引用映射到等级。
    """
    declared = list(request.body.get("input_refs") or [])
    inputs: list[dict[str, Any]] = []
    for ref in declared:
        inputs.append(
            {
                "content_ref": ref,
                "data_class": request.body.get("declared_data_class") or request.context.data_class,
                "origin": request.body.get("declared_origin") or "declared_context",
                "anonymized": bool(request.body.get("anonymized", False)),
                "transformation_rule_ref": request.body.get("transformation_rule_ref"),
                "review_ref": request.body.get("review_ref"),
            }
        )
    return inputs, []


def _handler_p1_classification(request) -> tuple[int, dict[str, Any]]:
    context = _p1_context(request)
    classification = classification_module.classify(
        request,
        declared_inputs=_p1_classify_inputs(request)[0],
        derived_origins=request.body.get("derived_origins") or [],
    )
    return 200, classification.as_dict(context)


def _handler_p1_egress_evaluate(request) -> tuple[int, dict[str, Any]]:
    context = _p1_context(request)
    body = request.body
    classification = classification_module.classify(
        request,
        declared_inputs=_p1_classify_inputs(request)[0],
        derived_origins=body.get("derived_origins") or [],
    )
    # 审计第 11 环节：**失败尝试同样留痕**（否则"没有成功记录"无法自证）。
    # 只写安全引用/哈希/版本，绝不写原文或令牌。
    audit_ref = f"egress-audit-{uuid.uuid4().hex[:16]}"
    try:
        decision = governance.evaluate(request=request, classification=classification, body=body)
    except errors.V3Error:
        egress_audit.record(
            context=context,
            audit_ref=audit_ref,
            classification_known=True,
            highest_data_class=classification.data_class,
            policy_version=body.get("policy_version") or "policy-unavailable",
            classification_rule_version=classification.rule_version,
            model_version=body.get("model_version"),
            requested_backend=body.get("model_backend"),
            invocation_performed=False,
            invocation_succeeded=False,
            output_released=False,
            stage_refs=[],
            summary_ref=audit_ref,
        )
        raise

    egress_audit.record(
        context=context,
        audit_ref=audit_ref,
        classification_known=True,
        highest_data_class=classification.data_class,
        policy_version=body["policy_version"],
        classification_rule_version=classification.rule_version,
        model_version=body["model_version"],
        requested_backend=body["model_backend"],
        invocation_performed=False,  # egress/evaluate 只判定，不调用
        invocation_succeeded=False,
        output_released=False,
        stage_refs=decision.stages,
        input_hash=egress_audit.summary_hash({"refs": body.get("input_refs")}),
        summary_ref=audit_ref,
    )

    view = decision.as_dict(context, classification, body)
    # 判定结果本身不含审计引用（契约封闭）；调用方从响应头/证据链获取 audit_ref。
    return 200, view


def _handler_p1_tokenize(request) -> tuple[int, dict[str, Any]]:
    _p1_context(request)
    body = request.body
    view = vault.tokenize(
        request=request,
        purpose=body["purpose"],
        input_refs=list(body["input_refs"]),
        expires_at=body["expires_at"],
    )
    return 200, view


def _handler_p1_restore(request) -> tuple[int, dict[str, Any]]:
    _p1_context(request)
    body = request.body
    view = vault.restore(
        request=request,
        mapping_ref=body["mapping_ref"],
        purpose=body["purpose"],
        consumer_ref=body["consumer_ref"],
        tokenized_output_ref=body["tokenized_output_ref"],
    )
    return 200, view


def _handler_p1_knowledge_query(request) -> tuple[int, dict[str, Any]]:
    context = _p1_context(request)
    body = request.body
    if not _task_exists(context.tenant_id, context.task_id):
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TASK_NOT_FOUND", "任务不存在或不可见")

    sources, acl_facts = knowledge.retrieve(registry=request.registry, context=context, purpose=body["purpose"])

    # 只读内容也要分级：来源等级不得高于分类结果，且输出继承最高等级。
    classification = classification_module.classify(
        request,
        declared_inputs=_p1_classify_inputs(request)[0],
        derived_origins=[
            {"content_ref": item["source_id"], "origin": "rag_chunk", "anonymized": False}
            for item in sources
        ],
    )
    if not knowledge.sources_within(sources, classification.data_class):
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "SOURCE_EXCEEDS_CLASSIFICATION", "来源等级高于分类结果",
            details={"denied_field": "data_class"},
        )

    view = {
        "tenant_id": context.tenant_id,
        "context": context.as_dict(),
        "sources": sources,
        "classification": classification.as_dict(context),
        "retrieval_version": "retrieval-mock-1",
        "acl_check": acl_facts[0],
    }
    return 200, view


def _handler_p1_knowledge_answer(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    task_id = request.path_params["task_id"]
    # 契约：path.task_id 必须等于 Context.task_id。
    if context.task_id != task_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "TASK_SCOPE_MISMATCH", "路径任务与 Context 不一致",
            details={"reason_ref": "task_id"},
        )
    record = _find_answer(context.tenant_id, task_id, context.trace_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "ANSWER_NOT_FOUND", "回答不存在或不可见")
    # 契约 p1-readonly.json#/$defs/Answer 是封闭对象（additionalProperties:false）：
    # 根层**不能**平铺 CommonContext 的九个键，只能由 tenant_id + 嵌套 context 表达。
    view = {
        "tenant_id": context.tenant_id,
        "context": context.as_dict(),
        **record["answer"],
    }
    # 回答里也可能带 context（分类结果内嵌），必须被这里的权威 Context 覆盖。
    view["context"] = context.as_dict()
    return 200, view


# ---- 只读回答投影（由编排链路写入；P1 不新增 HTTP 写入口）----------------

def record_answer(*, tenant_id: str, task_id: str, trace_id: str, answer: dict[str, Any]) -> None:
    journal.append("answers", {"tenant_id": tenant_id, "task_id": task_id, "trace_id": trace_id, "answer": answer})


def _find_answer(tenant_id: str, task_id: str, trace_id: str) -> dict[str, Any] | None:
    latest = journal.read_latest(
        "answers", lambda item: (item["tenant_id"], item["task_id"], item["trace_id"])
    )
    return latest.get((tenant_id, task_id, trace_id))


# ---- 任务存在性（跨进程：Task 由 control-plane 写、manufacturing-core 读）----

def _task_exists(tenant_id: str, task_id: str) -> bool:
    """任务存在性判定：读**同一份** journal（单一真相源），不再另写任务投影。"""
    return find_task(tenant_id, task_id) is not None


# ==========================================================================
# P1.5：持久化与事件一致性（契约 openapi/p15.openapi.json）
#
# 六个接口，全部挂在既有 L0/L2/L3 前缀下，**不新增接口层**。
# 承担两条上游硬语义：
#   ① 一个事实一个真相源（跨 owner 通过本契约的 GET 读，不复制状态）；
#   ② 事件至少一次投递 + 消费侧去重与版本收敛 + DLQ 重放不离原规则。
# ==========================================================================

def _p15_context(request):
    """P1.5 的 Context 形态与 P1 一致：嵌在 `context` 下，tenant 三方一致。"""
    return _p1_context(request)


def _p15_inbox(request) -> events.Inbox:
    return events.Inbox(projection_name=request.service_id)


def _p15_dlq() -> events.DeadLetterQueue:
    return events.DeadLetterQueue(name="owner")


def _handler_p15_task_get(request) -> tuple[int, dict[str, Any]]:
    """control-plane 的 Task 权威读（跨 owner 只读视图）。"""
    context = request.context
    task_id = request.path_params["task_id"]
    record = find_task(context.tenant_id, task_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TASK_NOT_FOUND", "任务不存在或不可见")
    return 200, projection_store.task_view(record, context)


def _handler_p15_run_get(request) -> tuple[int, dict[str, Any]]:
    """hermes-runtime 的 Run 权威读（含检查点版本）。"""
    context = request.context
    run_id = request.path_params["run_id"]
    record = find_run(context.tenant_id, run_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "RUN_NOT_FOUND", "运行不存在或不可见")
    return 200, projection_store.run_view(record, context)


def _handler_p15_events(request) -> tuple[int, dict[str, Any]]:
    """接收事实事件：本 owner 持久化投影与 inbox 去重后 ACK。

    ACK 只代表"已持久化受理"，**不代表业务成功** —— 契约把
    `business_success` 固定为 false，业务成功只能由权威回执驱动。
    """
    context = _p15_context(request)
    body = request.body
    envelope = body["event"]
    if not isinstance(envelope, dict):
        raise errors.reject_bad_request("event", "event 必须是事件信封对象")
    # 事件只能由**权威 adapter/owner** 在取得凭证后发布：必须带发布证据引用。
    if not body.get("publication_evidence_ref"):
        raise errors.reject_bad_request("publication_evidence_ref", "缺少发布证据引用")

    inbox = _p15_inbox(request)
    try:
        events.validate_envelope(envelope)
    except events.EventError as exc:
        # 结构非法**不得静默丢弃**：进 DLQ 并保留原因与原始事件（供人工处置/修复后重放）。
        entry = _p15_dlq().record(envelope, reason=str(exc), origin="INBOX")
        return 200, _delivery_ack(context, request.service_id, entry.get("event_id") or "evt-invalid", "DLQ", dlq_ref=entry["dlq_id"])

    # 逻辑校验通过后交给 inbox；语义处理异常同样进 DLQ 而不是抛 500。
    try:
        result = inbox.consume(envelope)
    except events.EventError as exc:
        entry = _p15_dlq().record(envelope, reason=str(exc), origin="INBOX")
        return 200, _delivery_ack(context, request.service_id, envelope.get("event_id") or "evt-invalid", "DLQ", dlq_ref=entry["dlq_id"])

    pull_ref = None
    dlq_ref = None
    if result.outcome is events.ConsumeOutcome.GAP:
        pull_ref = f"backfill-{envelope['aggregate_id']}-v{envelope['aggregate_version']}"
    disposition = {
        events.ConsumeOutcome.APPLIED: "APPLIED",
        events.ConsumeOutcome.DUPLICATE: "DUPLICATE",
        events.ConsumeOutcome.LATE: "LATE",
        events.ConsumeOutcome.GAP: "GAP",
    }[result.outcome]
    return 200, _delivery_ack(context, request.service_id, envelope["event_id"], disposition, pull_ref=pull_ref, dlq_ref=dlq_ref)


def _delivery_ack(context, consumer_unit, event_id, disposition, *, pull_ref=None, dlq_ref=None) -> dict[str, Any]:
    return {
        "context": context.as_dict(),
        "event_id": event_id,
        "consumer_unit": consumer_unit,
        "disposition": disposition,
        "durable": True,
        "business_success": False,
        "pull_queue_ref": pull_ref,
        "dlq_ref": dlq_ref,
    }


def _dlq_error_body(entry: dict[str, Any]) -> dict[str, Any]:
    """把死信原因投影成**契约合规**的 ErrorBody。

    只允许 5 个键，details 只允许封闭枚举；原因本身是代码，只能放 message/code，
    绝不能把事件正文或原始 payload 带出来。
    """
    reason = str(entry.get("reason") or "unknown_error")
    return {
        "code": "EVENT_DELIVERY_FAILED",
        "message": reason[:120],
        "trace_id": entry.get("event", {}).get("trace_id") or "trace-unavailable",
        "retryable": False,
        "details": {},
    }


def _dlq_view(entry: dict[str, Any], context) -> dict[str, Any]:
    return {
        "context": context.as_dict(),
        "dlq_id": entry["dlq_id"],
        "owner_unit": entry.get("owner_unit") or "trajectory-eval",
        "origin": entry.get("origin") or "INBOX",
        "event": entry["event"],
        "error": _dlq_error_body(entry),
        "attempt_count": int(entry.get("attempts", 1)),
        "replay_count": int(entry.get("replay_count", 0)),
        "handling_status": _normalize_dlq_status(entry.get("status")),
        "created_at": entry.get("recorded_at"),
        "updated_at": entry.get("updated_at") or entry.get("recorded_at"),
        "operator_ref": entry.get("operator_ref"),
        "handling_reason_code": entry.get("handling_reason_code"),
        "resolution_ref": entry.get("resolution_ref"),
        "record_version": int(entry.get("record_version", 0)),
        "publication_evidence_ref": entry.get("publication_evidence_ref") or "evidence-demo",
    }


def _normalize_dlq_status(status: str | None) -> str:
    legacy = {
        "pending_human": "MANUAL_REVIEW",
        "OPEN": "OPEN",
        "REPLAY_QUEUED": "REPLAY_QUEUED",
        "RESOLVED": "RESOLVED",
        "MANUAL_REVIEW": "MANUAL_REVIEW",
        "resolved": "RESOLVED",
    }
    return legacy.get(status or "", "OPEN")


def _handler_p15_dlq_get(request) -> tuple[int, dict[str, Any]]:
    context = request.context
    dlq_id = request.path_params["dlq_id"]
    entry = _p15_dlq().find(dlq_id)
    if entry is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "DLQ_ENTRY_NOT_FOUND", "死信记录不存在或不可见")
    if entry.get("tenant_id") not in (None, context.tenant_id):
        # 不区分"不存在"与"跨租户"，避免泄露其它租户的死信存在性。
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "DLQ_ENTRY_NOT_FOUND", "死信记录不存在或不可见")
    return 200, _dlq_view(entry, context)


def _handler_p15_dlq_replay(request) -> tuple[int, dict[str, Any]]:
    """重放死信：**仍走原去重与版本规则**。

    - DLQ 条目用乐观并发（expected_dlq_version）保护，避免并发重放；
    - 重放结果若是"重复"或"已应用"，语义上都已完成，标记 RESOLVED；
    - 若仍缺版本，标记 REPLAY_QUEUED 等待补拉，**不假装成功**。
    """
    context = request.context
    body = request.body
    dlq_id = request.path_params["dlq_id"]
    entry = _p15_dlq().find(dlq_id)
    if entry is None or entry.get("tenant_id") not in (None, context.tenant_id):
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "DLQ_ENTRY_NOT_FOUND", "死信记录不存在或不可见")
    if body["dlq_id"] != dlq_id:
        raise errors.reject_bad_request("dlq_id", "路径 dlq_id 与 body 不一致")

    current_version = int(entry.get("record_version", 0))
    if body["expected_dlq_version"] != current_version:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "DLQ_VERSION_CONFLICT", "死信记录版本冲突",
            details={"expected_version": body["expected_dlq_version"], "actual_version": current_version},
        )

    inbox = _p15_inbox(request)
    envelope = entry.get("event")
    if not isinstance(envelope, dict) or not envelope.get("event_id"):
        raise errors.V3Error(errors.HTTP_FORBIDDEN, "DLQ_EVENT_UNRESOLVABLE", "死信条目缺少可重放的事件")

    outcome = inbox.consume(envelope)
    if outcome.outcome in (events.ConsumeOutcome.APPLIED, events.ConsumeOutcome.DUPLICATE):
        _p15_dlq().bump_replay(dlq_id, status="RESOLVED")
    else:
        _p15_dlq().bump_replay(dlq_id, status="REPLAY_QUEUED")

    replay_id = f"replay-{uuid.uuid4().hex[:16]}"
    view = {
        "context": context.as_dict(),
        "dlq_id": dlq_id,
        "replay_request_id": replay_id,
        "status": "accepted",
        "business_success": False,
        "posting_status": "NOT_CONFIRMED",
        "status_url": f"/internal/v1/events/dlq/{dlq_id}",
    }
    return 202, view


def _handler_p15_receipt(request) -> tuple[int, dict[str, Any]]:
    """权威回执：**只有它能让命令成功**。

    契约要点：
    - 命令必须存在，且回执绑定的 `command_payload_hash` 必须与落库命令一致（防张冠李戴）；
    - 幂等：同 `receipt_id` 重复提交返回同一结果；
    - `accepted` / `UNKNOWN` 绝不能表述为"已过账"：只有权威回执驱动 `SUCCEEDED`；
    - `posting_status` 区分"业务成功"与"账务已过账"。
    """
    context = request.context
    body = request.body
    command_id = request.path_params["command_id"]
    if body["command_id"] != command_id:
        raise errors.reject_bad_request("command_id", "路径 command_id 与 body 不一致")

    record = find_command(context.tenant_id, command_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "COMMAND_NOT_FOUND", "命令不存在或不可见")
    if record["payload_hash"] != body["command_payload_hash"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "RECEIPT_PAYLOAD_MISMATCH", "回执与命令 payload 不匹配",
            details={"object_ref": "command_payload_hash"},
        )
    # §10.4：Transaction **复核** Tool Gateway 算出的规范哈希，而不是只信它传来的值。
    # 若该命令有规范化记录，就必须能用同一套规范输入重算出同一哈希；
    # 重算不一致 ⇒ 规范化结果被改过或来源不可信 ⇒ 拒绝收执（fail-closed）。
    if record.get("normalization_ref"):
        normalized = journal.read_latest(
            "normalizations", lambda item: (item["tenant_id"], item["normalization_ref"])
        ).get((context.tenant_id, record["normalization_ref"]))
        if normalized is None:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "NORMALIZATION_RECORD_MISSING",
                "命令声明的规范化记录不存在，无法复核哈希",
                details={"object_ref": "normalization_ref"},
            )
        binding = normalized["binding"]
        recomputed = transactions.canonical_payload_hash(
            tenant_id=context.tenant_id,
            object_id=binding["object_id"],
            expected_version=binding["expected_version"],
            tool_id=binding["tool_id"],
            tool_version=binding["tool_version"],
            command_type=binding["command_type"],
            action_scope=binding["action_scope"],
            payload_ref=binding["payload_ref"],
        )
        if recomputed != record["payload_hash"]:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "NORMALIZATION_HASH_MISMATCH",
                "规范化哈希与命令落库哈希不一致",
                details={"object_ref": "payload_hash"},
            )

    receipts = journal.read_latest(
        "authority-receipts", lambda item: (item["tenant_id"], item["receipt_id"])
    )
    key = (context.tenant_id, body["receipt_id"])
    existing = receipts.get(key)
    if existing is not None:
        return 200, dict(existing["view"])

    if record["status"] == "UNKNOWN":
        # 契约：UNKNOWN 必须先查源系统确认，不能靠"收到回执"直接改判。
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "COMMAND_STATE_UNKNOWN_REQUIRES_QUERY",
            "命令处于 UNKNOWN，需先查询源系统再收执", details={"reason_ref": "unknown_requires_query"},
        )

    record["status"] = "SUCCEEDED"
    record["receipt_refs"] = [body["authority_receipt_ref"]]
    record["source_system"] = body["source_system"]
    # ── 权威回执必须**落进命令记录**（2026-10-06 修的真 bug）──────────
    #
    # 此前这里只写 status / receipt_refs / source_system，**没写
    # `authority_receipt`**。而 `_p3_command_view` 是这么算结论的：
    #
    #     "business_success": bool(receipt and receipt.get("business_success") is True)
    #     "posting_status":  (receipt or {}).get("posting_status") or "NOT_CONFIRMED"
    #
    # ⇒ 于是**收到权威回执之后，后端仍报 `business_success=False`、
    # `posting_status="NOT_CONFIRMED"`**。这直接违反本项目的第一条显示契约
    # （「只有权威回执能说已过账」）。
    #
    # 更值得注意的是**不一致**：UNKNOWN 收敛路径（`_p3_reconcile`）本来就
    # 正确写了 `record["authority_receipt"]`，只有这条正常回执路径漏了——
    # 两条路形状不同，正是这类缺陷的典型成因。
    #
    # 过账状态**取自回执本身**，不在这里硬编码：是否已过账是源系统的事实，
    # Transaction 只负责如实记录。
    record["authority_receipt"] = {
        "context": context.as_dict(),
        "command_id": command_id,
        "idempotency_key": body["idempotency_key"],
        "command_payload_hash": body["command_payload_hash"],
        "receipt_id": body["receipt_id"],
        "source_system": body["source_system"],
        "adapter_unit": request.service_id,
        "outcome": "SUCCEEDED",
        "authority_receipt_ref": body["authority_receipt_ref"],
        "authority_receipt_hash": body["authority_receipt_hash"],
        "authority_verified": True,
        "confirmed_at": _utc_now(),
        "recorded_at": _utc_now(),
        "command_status": "SUCCEEDED",
        "business_success": True,
        "posting_status": body.get("posting_status") or "NOT_CONFIRMED",
        "posting_evidence_ref": body.get("posting_evidence_ref"),
        "status_source": "transaction-service",
    }
    record["updated_at"] = _utc_now()
    persist_command(record)

    view = {
        "tenant_id": context.tenant_id,
        "context": context.as_dict(),
        "command_id": command_id,
        "idempotency_key": body["idempotency_key"],
        "command_payload_hash": body["command_payload_hash"],
        "receipt_id": body["receipt_id"],
        "source_system": body["source_system"],
        "adapter_unit": request.service_id,
        "outcome": "SUCCEEDED",
        "authority_receipt_ref": body["authority_receipt_ref"],
        "authority_receipt_hash": body["authority_receipt_hash"],
        "authority_verified": True,
        "confirmed_at": _utc_now(),
        "recorded_at": _utc_now(),
        "command_status": "SUCCEEDED",
        "business_success": True,
        # 业务成功 ≠ 账务已过账：过账仍需 ERP 侧凭证。
        "posting_status": "NOT_CONFIRMED",
        "posting_evidence_ref": None,
        "status_source": "transaction-service",
    }
    journal.append(
        "authority-receipts",
        {"tenant_id": context.tenant_id, "receipt_id": body["receipt_id"], "command_id": command_id, "view": view},
    )
    return 200, view


# ==========================================================================
# P3：受控写入（契约 openapi/p3.openapi.json）
#
# 承担 §5 的关键流程：规范化 → 批准绑定 → 执行前复核 → 受控执行 → 查询收敛 →
# Saga/补偿。本批先落地两个**不依赖其它新接口**的判定型端点：
#   - POST /internal/v1/tool-requests/normalize（哈希由可信服务计算）
#   - GET  /internal/v1/tools/{tool_id}/write-admission（能力位准入）
# ==========================================================================

def _p3_get_context(request):
    """P3 的 **GET** 取值：Context 只在 `X-Context` 头里，请求没有 body。

    不能用 `_p1_context`：它要求 body 里也有 `tenant_id`，而 GET 的 body 是空的，
    于是会永远报 `NESTED_TENANT_MISMATCH`。Context 的权威来源仍是可信认证
    （`http._identity_tenant` 已在校验时核对过），这里只做工厂/任务非空检查。
    """
    context = request.context
    if not context.factory_id:
        raise errors.reject_bad_request("factory_id", "工厂只读链路要求 factory_id 非空")
    if not context.task_id:
        raise errors.reject_bad_request("task_id", "工厂只读链路要求 task_id 非空")
    return context


def _tool_spec_view(registry: Registry, tool_id: str, tenant_id: str) -> dict[str, Any] | None:
    """把受信工具目录条目投影成契约要求的 ToolSpec 形状。

    `additionalProperties: false` 意味着**不能**把 registry 里的内部字段
    （如 `source_system`）直接透出去；这里显式挑字段。
    """
    entry = registry.tool(tool_id)
    if entry is None:
        return None
    return {
        "tenant_id": tenant_id,
        "tool_id": entry["tool_id"],
        "version": entry["tool_version"],
        "input_schema": entry["input_schema"],
        "output_schema": entry["output_schema"],
        "read_write": entry["read_write"],
        "risk_class": entry["risk_class"],
        "required_scopes": list(entry["required_scopes"]),
        "timeout": entry["timeout"],
        "retry_policy": entry["retry_policy"],
        "idempotency_support": entry["idempotency_support"],
        "status_query": entry["status_query"],
        "compensation": entry["compensation"],
        "data_class_rules": entry["data_class_rules"],
        "adapter_owner": entry["adapter_owner"],
    }


def _handler_p3_normalize(request) -> tuple[int, dict[str, Any]]:
    """可信服务读取受控输入并计算绑定哈希（§5：不能只信 Agent 提供的哈希）。

    未知工具一律拒绝（fail-closed）：工具能力位与 schema 都来自**可信登记**，
    没有登记就没有能力，也就无从判定幂等/查询/补偿。
    """
    context = _p1_context(request)
    body = request.body
    tool_id = body["tool_id"]
    entry = request.registry.tool(tool_id)
    if entry is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TOOL_NOT_IN_CATALOG", "工具未在受信目录中登记")
    if entry["tool_version"] != body["tool_version"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "TOOL_VERSION_MISMATCH", "工具版本与受信目录不一致",
            details={"object_ref": "tool_version"},
        )
    if entry["read_write"] != "write":
        raise errors.V3Error(errors.HTTP_FORBIDDEN, "TOOL_IS_READ_ONLY", "只读工具不得进入写入规范化")

    payload_hash = transactions.canonical_payload_hash(
        tenant_id=context.tenant_id,
        object_id=body["object_id"],
        expected_version=body["expected_version"],
        tool_id=tool_id,
        tool_version=entry["tool_version"],
        command_type=body["command_type"],
        action_scope=entry["required_scopes"],
        payload_ref=body["input_ref"],
    )
    normalization_ref = f"norm-{payload_hash[:16]}"

    binding = {
        "context": context.as_dict(),
        "object_id": body["object_id"],
        "expected_version": body["expected_version"],
        "tool_id": tool_id,
        "tool_version": entry["tool_version"],
        "command_type": body["command_type"],
        "action_scope": list(entry["required_scopes"]),
        "payload_ref": body["input_ref"],
        "payload_hash": payload_hash,
        "payload_schema": body["input_schema"],
        "normalization_ref": normalization_ref,
        "policy_version": request.registry.default_policy_version(),
    }
    view = {
        "context": context.as_dict(),
        "normalization_ref": normalization_ref,
        "binding": binding,
        "normalized_at": _utc_now(),
        "normalizer_unit": "tool-gateway",
    }
    # 固化规范化结果，供后续批准绑定与执行前复核复用（哈希必须能重算一致）
    journal.append(
        "normalizations",
        {
            "tenant_id": context.tenant_id,
            "normalization_ref": normalization_ref,
            "binding": binding,
            "recorded_at": _utc_now(),
        },
    )
    return 200, view


def _handler_p3_write_admission(request) -> tuple[int, dict[str, Any]]:
    """读取 ToolSpec 能力位与无人值守高风险准入约束（§5：能力位不足 → 拒绝无人值守写入）。

    能力位来自受信目录；`compensation=false` 不代表"不能写"，
    而是"不能**无人值守**地写"。
    """
    context = _p3_get_context(request)
    tool_id = request.path_params["tool_id"]
    spec = _tool_spec_view(request.registry, tool_id, context.tenant_id)
    if spec is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TOOL_NOT_IN_CATALOG", "工具未在受信目录中登记")

    admission = transactions.write_admission(spec)
    return 200, {
        "context": context.as_dict(),
        "tool_spec": spec,
        "unattended_high_risk_write_allowed": admission["unattended_high_risk_write_allowed"],
        "adapter_ledger_required": admission["adapter_ledger_required"],
        "serial_object_lock_required": admission["serial_object_lock_required"],
        "manual_reconciliation_required": admission["manual_reconciliation_required"],
        "reason_ref": admission["reason_ref"],
    }


# --------------------------------------------------------------------------
# P3 命令主干：受控持久化 → 租约/fencing → 执行前复核 → 执行意图
# --------------------------------------------------------------------------

# P3 允许执行的状态（其余一律拒绝，避免跳状态）
P3_EXECUTABLE_STATES = ("AUTHORIZED", "EXECUTING")


def _utc_after(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def _lease_unexpired(expires_at: str) -> bool:
    """按**可信时间**判断租约是否仍有效；解析失败一律视为过期（fail-closed）。"""
    try:
        parsed = time.mktime(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return False
    return parsed > time.time()


def _p3_command_record(tenant_id: str, command_id: str) -> dict[str, Any]:
    record = find_command(tenant_id, command_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "COMMAND_NOT_FOUND", "命令不存在或不可见")
    return record


def _p3_read_normalization(tenant_id: str, normalization_ref: str) -> dict[str, Any]:
    """读规范化记录；缺失即拒绝（哈希复核的前提）。

    §10.4：Transaction 必须能**复核** Tool Gateway 算出的哈希，
    因此规范化结果必须已持久化；查不到说明状态不一致，fail-closed。
    """
    found = journal.read_latest(
        "normalizations", lambda item: (item["tenant_id"], item["normalization_ref"])
    ).get((tenant_id, normalization_ref))
    if found is None:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "NORMALIZATION_RECORD_MISSING",
            "规范化记录不存在，无法复核绑定哈希",
            details={"object_ref": "normalization_ref"},
        )
    return found


def _p3_recompute_hash(tenant_id: str, binding: dict[str, Any]) -> str:
    return transactions.canonical_payload_hash(
        tenant_id=tenant_id,
        object_id=binding["object_id"],
        expected_version=binding["expected_version"],
        tool_id=binding["tool_id"],
        tool_version=binding["tool_version"],
        command_type=binding["command_type"],
        action_scope=binding["action_scope"],
        payload_ref=binding["payload_ref"],
    )


def _p3_command_view(record: dict[str, Any], context) -> dict[str, Any]:
    """P3 命令权威视图。

    业务成功与过账状态**分离**：`business_success` 只由权威回执驱动，
    `posting_status` 在拿到真实过账凭证前恒为 `NOT_CONFIRMED`。
    `status_source` 固定 transaction-service —— 编排状态不是执行权威。
    """
    receipt = record.get("authority_receipt")
    return {
        "context": context.as_dict(),
        "command_id": record["command_id"],
        "idempotency_key": record["idempotency_key"],
        "binding": record["binding"],
        "policy_evaluation_ref": record["policy_evaluation_ref"],
        "approval_ref": record.get("approval_ref"),
        "status": record["status"],
        "record_version": int(record.get("record_version", 0)),
        "status_source": "transaction-service",
        "updated_at": record["updated_at"],
        "execution_credential": record.get("execution_credential"),
        "authority_receipt": receipt,
        "business_success": bool(receipt and receipt.get("business_success") is True),
        "posting_status": (receipt or {}).get("posting_status") or "NOT_CONFIRMED",
        "posting_evidence_ref": (receipt or {}).get("posting_evidence_ref"),
        "reconciliation_ref": record.get("reconciliation_ref"),
        "manual_queue_ref": record.get("manual_queue_ref"),
    }


def _p3_accepted(record: dict[str, Any], context) -> dict[str, Any]:
    """受理响应：**只表示已持久化受理**，绝不表示业务成功或已过账。"""
    return {
        "context": context.as_dict(),
        "command_id": record["command_id"],
        "status": "accepted",
        "business_success": False,
        "posting_status": "NOT_CONFIRMED",
        "status_url": f"/internal/v1/commands/{record['command_id']}/execution-record",
    }


def _handler_p3_command_controlled(request) -> tuple[int, dict[str, Any]]:
    """持久化 P3 命令（§10.4）。

    三条硬语义：
    1. **先持久化再返回 202**（`persist_before_202`）；
    2. **同键同 payload → 返回同一命令及其当前结果**；
       同键异 payload → **409**；
    3. 落库 `payload_hash` 必须能用规范化记录**重算一致**
       （`hash_rechecked_by_transaction`）—— 不能只信调用方传来的值。
    """
    context = _p1_context(request)
    body = request.body

    normalized = _p3_read_normalization(context.tenant_id, body["normalization_ref"])
    binding = normalized["binding"]
    if binding.get("context", {}).get("tenant_id") != context.tenant_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "NORMALIZATION_TENANT_MISMATCH", "规范化记录不属于当前租户",
            details={"denied_field": "tenant_id"},
        )
    trusted_hash = _p3_recompute_hash(context.tenant_id, binding)

    approval_ref = body.get("approval_ref")
    tool = request.registry.tool(binding["tool_id"])
    if tool is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TOOL_NOT_IN_CATALOG", "工具未在受信目录中登记")

    with _STORE_LOCK:
        existing = find_command_by_key(context.tenant_id, body["idempotency_key"])
        if existing is not None:
            if existing["payload_hash"] != trusted_hash:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT, errors.IDEMPOTENCY_CONFLICT,
                    "同幂等键携带了不同 payload",
                    details={"object_ref": "payload_hash"},
                )
            # 同键同 payload：返回同一命令及其**当前**结果，不重复产生副作用
            return 202, _p3_accepted(existing, context)

        command_id = _rid("command")
        needs_approval = tool["risk_class"] in ("R2", "R3")
        record = {
            "command_id": command_id,
            "tenant_id": context.tenant_id,
            # ⚠️ **任务与轨迹写进顶层**（2026-10-06，B2 任务↔命令关联）。
            #
            # 它们本来只在 `binding["context"]` 里。而"这个任务下有哪些命令"
            # 需要按 task_id 索引命令账本——若每次都要钻进嵌套的 context，
            # 那就是**在读取侧重新解释一次 Context 形状**，错一次就静默漏命令。
            #
            # 取值来源是**可信 Context**（`_p3_context` 已校验租户与身份），
            # 不是请求体里用户自填的字段——所以这不是"多信一个输入"。
            "task_id": context.task_id,
            "trace_id": context.trace_id,
            "idempotency_key": body["idempotency_key"],
            "payload_hash": trusted_hash,
            "binding": binding,
            "normalization_ref": body["normalization_ref"],
            "policy_evaluation_ref": body["policy_evaluation_ref"],
            "approval_ref": approval_ref,
            "status": "ACCEPTED",
            "record_version": 0,
            "risk_class": tool["risk_class"],
            "execution_credential": None,
            "authority_receipt": None,
            "reconciliation_ref": None,
            "manual_queue_ref": None,
            "lease": None,
            "object_lock_ref": None,
            "object_phase": "NOT_STARTED",
            "cancelled_before_start": False,
            "review_ref": None,
            "updated_at": _utc_now(),
        }
        # 需要批准但尚未批准 → 停在 ACCEPTED，等批准事件；不假装能执行。
        if not needs_approval or approval_ref:
            record["status"] = "AUTHORIZED"
            record["record_version"] = 1
        persist_command(record)

    return 202, _p3_accepted(record, context)


def _handler_p3_execution_record(request) -> tuple[int, dict[str, Any]]:
    """读取命令权威状态、执行凭证与**分离的**业务/过账结果。"""
    context = _p3_get_context(request)
    record = _p3_command_record(context.tenant_id, request.path_params["command_id"])
    return 200, _p3_command_view(record, context)


def _handler_p3_lease(request) -> tuple[int, dict[str, Any]]:
    """CAS 取得/续租 worker 租约与串行对象锁（§12）。

    - `expected_record_version` 做 CAS：并发 worker 只有一个能拿到租约；
    - `fencing_token` **单调递增**：旧 worker 拿着旧 token 后续会被拒；
    - 对象锁在有效租约期内独占，避免两个 worker 同时改一个对象。
    """
    context = _p1_context(request)
    body = request.body
    command_id = request.path_params["command_id"]
    if body["command_id"] != command_id:
        raise errors.reject_bad_request("command_id", "路径 command_id 与 body 不一致")

    with _STORE_LOCK:
        record = _p3_command_record(context.tenant_id, command_id)  # 锁内读，避免 TOCTOU
        current_version = int(record.get("record_version", 0))
        if body["expected_record_version"] != current_version:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "COMMAND_VERSION_CONFLICT", "命令记录版本冲突",
                details={"expected_version": body["expected_record_version"], "actual_version": current_version},
            )

        previous = record.get("lease")
        if body.get("previous_lease_ref"):
            # 续租必须由**同一个** worker 用同一个 lease_ref 发起
            if previous is None or previous["lease_ref"] != body["previous_lease_ref"]:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT, "LEASE_NOT_HELD_BY_CALLER", "续租的租约不是调用方持有",
                    details={"object_ref": "lease_ref"},
                )
            if previous["worker_ref"] != body["worker_ref"]:
                raise errors.V3Error(
                    errors.HTTP_FORBIDDEN, "LEASE_WORKER_MISMATCH", "续租 worker 与持有者不符",
                    details={"denied_field": "worker_ref"},
                )
        elif previous is not None and _lease_unexpired(previous["expires_at"]):
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "OBJECT_LOCK_HELD", "对象已被其他 worker 加锁",
                details={"object_ref": "object_lock_ref"},
            )

        next_token = int(previous["fencing_token"]) + 1 if previous else 1
        lease = {
            "context": context.as_dict(),
            "command_id": command_id,
            "object_id": record["binding"]["object_id"],
            "worker_ref": body["worker_ref"],
            "lease_ref": f"lease-{uuid.uuid4().hex[:16]}",
            "fencing_token": next_token,
            "issued_at": _utc_now(),
            "expires_at": _utc_after(60),
            "object_lock_ref": f"lock-{record['binding']['object_id']}",
            "record_version": current_version + 1,
        }
        record["lease"] = lease
        record["object_lock_ref"] = lease["object_lock_ref"]
        record["record_version"] = current_version + 1
        record["updated_at"] = _utc_now()
        persist_command(record)

    return 200, lease


def _handler_p3_execute(request) -> tuple[int, dict[str, Any]]:
    """持久化执行意图，EXECUTING 前**复核**（§12：AUTHORIZED 不是永续许可）。

    复核项（任一不成立即拒绝并给出可定位原因）：开始前取消、状态允许执行、
    记录版本 CAS、租约有效、fencing token 匹配、批准/权限/策略/对象版本仍成立。

    响应**仅受理**：`business_success=false`、`posting_status=NOT_CONFIRMED`。
    """
    context = _p1_context(request)
    body = request.body
    command_id = request.path_params["command_id"]
    if body["command_id"] != command_id:
        raise errors.reject_bad_request("command_id", "路径 command_id 与 body 不一致")

    with _STORE_LOCK:
        record = _p3_command_record(context.tenant_id, command_id)

        if record.get("cancelled_before_start"):
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "CANCELLED_BEFORE_START", "命令在开始前已被取消",
                details={"reason_ref": "cancelled_before_start"},
            )
        if record["status"] not in P3_EXECUTABLE_STATES:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "COMMAND_NOT_EXECUTABLE", "命令当前状态不允许执行",
                details={"reason_ref": f"status:{record['status']}"},
            )

        current_version = int(record.get("record_version", 0))
        if body["expected_record_version"] != current_version:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "COMMAND_VERSION_CONFLICT", "命令记录版本冲突",
                details={"expected_version": body["expected_record_version"], "actual_version": current_version},
            )

        lease = record.get("lease")
        # 先判 fencing token，再判 lease_ref —— 顺序有语义：
        # 旧 worker 的 lease_ref 与新租约本来就不同，若先比 lease_ref 就会先报
        # LEASE_NOT_HELD，把"被新 worker 抢占"这个**关键事实**掩盖掉，
        # 排障时无法区分"没拿过锁"与"锁被抢了"（实测踩过）。
        if lease is not None and int(lease["fencing_token"]) != body["fencing_token"]:
            # 旧 worker 携带旧 token 提交 → 拒绝（防"僵尸 worker"覆盖新结果）
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "FENCING_TOKEN_STALE", "fencing token 已过期（旧 worker 提交）",
                details={"reason_ref": "fencing_token"},
            )
        if lease is None or lease["lease_ref"] != body["lease_ref"]:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "LEASE_NOT_HELD", "未持有有效租约", details={"object_ref": "lease_ref"},
            )
        if not _lease_unexpired(lease["expires_at"]):
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "LEASE_EXPIRED", "租约已过期", details={"reason_ref": "lease_expired"},
            )

        entry = request.registry.tool(record["binding"]["tool_id"])
        if entry is None:
            raise errors.V3Error(errors.HTTP_NOT_FOUND, "TOOL_NOT_IN_CATALOG", "工具未在受信目录中登记")

        approval = None
        if record.get("approval_ref"):
            # 必须走**同一个**批准事实存储（`approvals`）：批准由 P0 决策接口写入，
            # P3 只是复用它的形状。早先这里读的是另一个（从未写入的）通道，
            # 导致"批准明明存在却报 APPROVAL_NOT_FOUND"——真 bug，实测抓到。
            approval = find_approval(context.tenant_id, record["approval_ref"])
            if approval is None:
                raise errors.V3Error(
                    errors.HTTP_CONFLICT, "APPROVAL_NOT_FOUND", "命令声明的批准不存在",
                    details={"object_ref": "approval_ref"},
                )

        refusal = transactions.revalidate_before_execution(
            command={
                "actor_id": context.actor_id,
                "object_id": record["binding"]["object_id"],
                "payload_hash": record["payload_hash"],
                "tool_id": record["binding"]["tool_id"],
                "tool_version": record["binding"]["tool_version"],
                "policy_version": record["binding"]["policy_version"],
                "action_scope": record["binding"]["action_scope"],
                "expected_version": record["binding"]["expected_version"],
                "risk_class": record.get("risk_class"),
                "required_scopes": entry["required_scopes"],
            },
            approval=approval,
            current={
                "object_version": record["binding"]["expected_version"],
                "granted_scopes": entry["required_scopes"],
                "policy_version": record["binding"]["policy_version"],
                "object_phase": record.get("object_phase", "NOT_STARTED"),
            },
            tool=entry,
            now=_utc_now(),
        )
        if refusal.blocked:
            raise errors.V3Error(
                errors.HTTP_FORBIDDEN, "PRE_EXECUTION_REVIEW_FAILED", "执行前复核未通过",
                details={"reason_ref": refusal.reasons[0]},
            )

        execution_ref = f"exec-{uuid.uuid4().hex[:16]}"
        credential = {
            "context": context.as_dict(),
            "command_id": command_id,
            "idempotency_key": record["idempotency_key"],
            "binding": record["binding"],
            "execution_ref": execution_ref,
            "pre_state_ref": f"pre-{command_id}",
            "pre_state_hash": _canonical_hash(
                {
                    "command_id": command_id,
                    "object_id": record["binding"]["object_id"],
                    "expected_version": record["binding"]["expected_version"],
                }
            ),
            "review_ref": body["review_ref"],
            "lease": lease,
            "execution_permit_ref": f"permit-{execution_ref}",
            "audit_intent_ref": f"audit-{execution_ref}",
            "started_at": _utc_now(),
            "record_version": current_version + 1,
            "status_source": "transaction-service",
        }
        record["status"] = "EXECUTING"
        record["execution_credential"] = credential
        record["review_ref"] = body["review_ref"]
        record["record_version"] = current_version + 1
        record["updated_at"] = _utc_now()
        persist_command(record)

    return 202, _p3_accepted(record, context)


# --------------------------------------------------------------------------
# P3 UNKNOWN 收敛：查询源系统 → 记录 → 同键重试 / 人工接管
# --------------------------------------------------------------------------

# 与 p3-unknown.json 的 x-convergence-policy 逐字一致
UNKNOWN_CANDIDATE_ACTIONS = ("QUERY_SOURCE", "RETRY_SAME_KEY", "MANUAL_REVIEW")
UNKNOWN_PROHIBITED_ACTIONS = ("RESEND_NEW_KEY", "DIRECT_COMPENSATION")

# 注意：`UNKNOWN_CANDIDATE_ACTIONS` 是**动作全集**，用于说明允许范围；
# 真正写进记录的是**分场景常量**（见 `_handler_p3_reconcile` 里的映射）。
# 契约里 `query_attempt_count == 0` 分支要求 `candidate_actions == ["QUERY_SOURCE"]`，
# 但本实现把"置 UNKNOWN"与"查询源系统"合成一次调用（首次即完成查询），
# 因此 `query_attempt_count` 恒 ≥ 1，那个分支**当前不可达**——
# 这是已知差异，不是遗漏；若后续拆出独立的"标记 UNKNOWN"入口需补齐该分支。

# 内部决策动作 → 契约里的候选动作（None 表示该结论已终局，无需下一步）
_UNKNOWN_ACTION_MAP = {
    "record_confirmed_success": None,
    "retry_same_idempotency_key": "RETRY_SAME_KEY",
    "manual_takeover": "MANUAL_REVIEW",
}


def _handler_p3_reconcile(request) -> tuple[int, dict[str, Any]]:
    """查询源系统并记录收敛结论（§5 的 UNKNOWN 收敛）。

    硬规则（逐条对应契约 `x-convergence-policy`）：
    - 必须先查源系统（`mandatory_first_action`）；
    - **只有确认未执行**（`NOT_EXECUTED`）才允许用**同一幂等键**重试；
    - 无法确认 / 查询失败 → 人工接管；
    - **禁止换新键重发、禁止直接补偿**（恒定输出，便于验收断言）；
    - 人工批准本身**不构成**业务成功（`manual_approval_alone_is_success: false`）。
    """
    context = _p1_context(request)
    body = request.body
    command_id = request.path_params["command_id"]
    if body["command_id"] != command_id:
        raise errors.reject_bad_request("command_id", "路径 command_id 与 body 不一致")

    with _STORE_LOCK:
        record = _p3_command_record(context.tenant_id, command_id)

        lease = record.get("lease")
        if lease is None or lease["lease_ref"] != body["lease_ref"]:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "LEASE_NOT_HELD", "收敛必须由持有租约的 worker 发起",
                details={"object_ref": "lease_ref"},
            )
        if int(lease["fencing_token"]) != body["fencing_token"]:
            raise errors.V3Error(
                errors.HTTP_CONFLICT, "FENCING_TOKEN_STALE", "fencing token 已过期（旧 worker 提交）",
                details={"reason_ref": "fencing_token"},
            )

        entry = request.registry.tool(record["binding"]["tool_id"]) or {}
        source_state = body["source_state"]
        if source_state not in transactions.SOURCE_STATES:
            raise errors.reject_bad_request("source_state", "源系统结论不在允许集合内")

        attempts = int(record.get("reconcile_attempts", 0)) + 1
        decision = transactions.decide_unknown_convergence(
            source_state=source_state,
            same_key_retry_attempts=attempts,
            tool=entry,
        )

        recorded_receipt = None
        if source_state == "SUCCEEDED":
            # 已成功：记录权威结果并置成功；业务成功仍与过账状态分离
            status = "SUCCEEDED"
            business_success = True
            recorded_receipt = {
                "context": context.as_dict(),
                "command_id": command_id,
                "idempotency_key": record["idempotency_key"],
                "command_payload_hash": record["payload_hash"],
                "receipt_id": f"receipt-{uuid.uuid4().hex[:12]}",
                "source_system": entry.get("source_system") or entry.get("adapter_owner") or "source-mock",
                "adapter_unit": "tool-gateway",
                "outcome": "SUCCEEDED",
                "authority_receipt_ref": f"authority-{command_id}",
                "authority_receipt_hash": "sha256:reconciled",
                "authority_verified": True,
                "confirmed_at": _utc_now(),
                "recorded_at": _utc_now(),
                "command_status": "SUCCEEDED",
                "business_success": True,
                "posting_status": "NOT_CONFIRMED",
                "posting_evidence_ref": None,
                "status_source": "transaction-service",
            }
            record["authority_receipt"] = recorded_receipt
        elif source_state == "NOT_EXECUTED" and decision["action"] == "retry_same_idempotency_key":
            # 确认未执行：可用**同一**幂等键重试；回到 AUTHORIZED 等待重新执行
            status = "AUTHORIZED"
            business_success = False
        else:
            status = "MANUAL_REVIEW"
            business_success = False

        next_action = _UNKNOWN_ACTION_MAP.get(decision["action"])
        # 契约对 candidate_actions 是**分场景常量**（p3-unknown.json 的 allOf/then），
        # 不是"总是三项"。按结论映射，否则合法的记录会被契约判为不合规。
        if source_state in ("UNKNOWN", "accepted"):
            candidate_actions = ["MANUAL_REVIEW"]
        elif next_action == "RETRY_SAME_KEY":
            candidate_actions = ["RETRY_SAME_KEY", "MANUAL_REVIEW"]
        else:
            # 已成功/已确认失败：无需下一步动作，只保留人工复核这一项
            candidate_actions = ["MANUAL_REVIEW"]

        manual_queue_ref = None
        if status == "MANUAL_REVIEW":
            manual_queue_ref = f"manual-{command_id}"
            record["manual_queue_ref"] = manual_queue_ref

        reconciliation_ref = f"recon-{uuid.uuid4().hex[:12]}"
        record["status"] = status
        record["reconcile_attempts"] = attempts
        record["reconciliation_ref"] = reconciliation_ref
        record["record_version"] = int(record.get("record_version", 0)) + 1
        record["updated_at"] = _utc_now()
        persist_command(record)

    view = {
        "context": context.as_dict(),
        "command_id": command_id,
        "reconciliation_ref": reconciliation_ref,
        "idempotency_key": record["idempotency_key"],
        "command_payload_hash": record["payload_hash"],
        "source_system": (recorded_receipt or {}).get("source_system") or "source-mock",
        "query_attempt_count": attempts,
        "query_evidence_ref": f"query-evidence-{reconciliation_ref}",
        "query_evidence_hash": "b" * 64,
        "query_verified": source_state in transactions.SOURCE_STATES,
        "source_outcome": source_state,
        "command_status": status,
        "candidate_actions": candidate_actions,
        "prohibited_actions": list(UNKNOWN_PROHIBITED_ACTIONS),
        "next_action": next_action,
        "retry_idempotency_key": record["idempotency_key"] if next_action == "RETRY_SAME_KEY" else None,
        # 重试必须重新走执行前复核（不得复用旧批准结论）
        "retry_requires_fresh_review": True,
        "recorded_receipt": recorded_receipt,
        "manual_queue_ref": manual_queue_ref,
        "last_error": None,
        "recorded_at": _utc_now(),
        "status_source": "transaction-service",
        "business_success": business_success,
        "posting_status": (recorded_receipt or {}).get("posting_status") or "NOT_CONFIRMED",
    }
    journal.append(
        "reconciliations",
        {
            "tenant_id": context.tenant_id,
            "reconciliation_ref": reconciliation_ref,
            "view": view,
            "recorded_at": _utc_now(),
        },
    )
    return 200, view


def _handler_p3_reconciliation(request) -> tuple[int, dict[str, Any]]:
    """读取 UNKNOWN 收敛记录、候选与禁止动作。"""
    context = _p3_get_context(request)
    command_id = request.path_params["command_id"]
    record = _p3_command_record(context.tenant_id, command_id)
    reconciliation_ref = record.get("reconciliation_ref")
    if not reconciliation_ref:
        raise errors.V3Error(
            errors.HTTP_NOT_FOUND, "RECONCILIATION_NOT_FOUND", "该命令尚无收敛记录",
            details={"reason_ref": "reconciliation_ref"},
        )
    found = journal.read_latest(
        "reconciliations", lambda item: (item["tenant_id"], item["reconciliation_ref"])
    ).get((context.tenant_id, reconciliation_ref))
    if found is None:
        raise errors.V3Error(
            errors.HTTP_NOT_FOUND, "RECONCILIATION_NOT_FOUND", "收敛记录不存在",
            details={"reason_ref": "reconciliation_ref"},
        )
    return 200, found["view"]


# ==========================================================================
# P3 批准：绑定 → 事实 → 执行前复核（§5 / §12）
# ==========================================================================

def _p3_approval_record(tenant_id: str, approval_id: str) -> dict[str, Any]:
    record = find_approval(tenant_id, approval_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "APPROVAL_NOT_FOUND", "批准不存在或不可见")
    return record


def _p3_read_normalization_binding(tenant_id: str, normalization_ref: str) -> dict[str, Any]:
    """读规范化记录里的绑定（批准绑定必须**复用**同一份规范化结果）。"""
    return _p3_read_normalization(tenant_id, normalization_ref)["binding"]


def _handler_p3_approval_binding(request) -> tuple[int, dict[str, Any]]:
    """建立可信批准**绑定**（§5：绑定对象/规范化哈希/策略版本/动作范围/有效期）。

    关键：绑定必须复用 Tool Gateway 的规范化结果，**不得**由调用方另填一份哈希；
    否则"批准绑 A、执行时 B"就无从发现。
    """
    context = _p1_context(request)
    body = request.body
    binding = _p3_read_normalization_binding(context.tenant_id, body["normalization_ref"])
    if binding.get("context", {}).get("tenant_id") != context.tenant_id:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "NORMALIZATION_TENANT_MISMATCH", "规范化记录不属于当前租户",
            details={"denied_field": "tenant_id"},
        )

    approval_id = f"approval-{uuid.uuid4().hex[:16]}"
    record = {
        "tenant_id": context.tenant_id,
        "factory_id": context.factory_id,
        "approval_id": approval_id,
        "trace_id": context.trace_id,
        "approved": None,  # 绑定阶段尚未作出批准决定
        "approver_ref": None,
        "binding": binding,
        "binding_hash": _p3_binding_hash(binding),
        "policy_evaluation_ref": body["policy_evaluation_ref"],
        "idempotency_key": body["idempotency_key"],
        "decision_ref": None,
        "decided_at": None,
        "valid_from": _utc_now(),
        "valid_until": body["valid_until"],
        "record_version": 0,
        "updated_at": _utc_now(),
    }
    persist_approval(record)
    return 200, {
        "context": context.as_dict(),
        "approval_id": approval_id,
        "binding": binding,
        "policy_evaluation_ref": body["policy_evaluation_ref"],
        "record_version": 0,
        "created_at": record["valid_from"],
        "valid_from": record["valid_from"],
        "valid_until": record["valid_until"],
    }


def _handler_p3_approval_fact(request) -> tuple[int, dict[str, Any]]:
    """读取独立批准事实与绑定/有效期（谁批的、批了什么、何时失效）。

    `approved` 在契约里是**布尔**，所以尚未作出决定的绑定**不能**返回本视图——
    返回 `null` 会违反契约，也会让调用方把"还没批"误读成"已拒绝"。
    """
    context = request.context
    approval_id = request.path_params["approval_id"]
    record = _p3_approval_record(context.tenant_id, approval_id)
    if record.get("approved") is None:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "APPROVAL_NOT_DECIDED", "该批准已建立绑定但尚未作出决定",
            details={"reason_ref": "approval_not_decided"},
        )
    return 200, {
        "context": context.as_dict(),
        "approval_id": record["approval_id"],
        "binding": record["binding"],
        "approved": record["approved"],
        "approver_ref": record.get("approver_ref") or "approver-unassigned",
        "decided_at": record.get("decided_at") or record["valid_from"],
        "valid_from": record["valid_from"],
        "valid_until": record["valid_until"],
        "record_version": int(record.get("record_version", 0)),
        "decision_ref": record.get("decision_ref") or f"approval-decision-{approval_id}",
        "policy_evaluation_ref": record.get("policy_evaluation_ref") or f"policy-eval-{approval_id}",
        "audit_intent_ref": f"audit-approval-{approval_id}",
        "status_source": "control-plane",
    }


def _handler_p3_pre_execution_review(request) -> tuple[int, dict[str, Any]]:
    """执行前再验证（§12：`AUTHORIZED` 不是永续许可）。

    逐项给出结论（批准有效、哈希/策略/动作范围/对象版本一致、当前权限、委托、
    职责分离、能力准入、租约/fencing/对象锁、未被取消），并给出**唯一**原因引用。
    """
    context = _p1_context(request)
    body = request.body
    command_id = body["command_id"]
    record = _p3_command_record(context.tenant_id, command_id)

    if body["command_record_version"] != int(record.get("record_version", 0)):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "COMMAND_VERSION_CONFLICT", "命令记录版本冲突",
            details={
                "expected_version": body["command_record_version"],
                "actual_version": int(record.get("record_version", 0)),
            },
        )

    entry = request.registry.tool(record["binding"]["tool_id"])
    if entry is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TOOL_NOT_IN_CATALOG", "工具未在受信目录中登记")

    approval = None
    if record.get("approval_ref"):
        approval = find_approval(context.tenant_id, record["approval_ref"])

    lease = record.get("lease")
    review = transactions.review_command(
        command={
            "actor_id": context.actor_id,
            "object_id": record["binding"]["object_id"],
            "payload_hash": record["payload_hash"],
            "tool_id": record["binding"]["tool_id"],
            "tool_version": record["binding"]["tool_version"],
            "policy_version": record["binding"]["policy_version"],
            "action_scope": record["binding"]["action_scope"],
            "expected_version": record["binding"]["expected_version"],
            "risk_class": record.get("risk_class"),
            "required_scopes": entry["required_scopes"],
        },
        approval=approval,
        current={
            "object_version": record["binding"]["expected_version"],
            "granted_scopes": entry["required_scopes"],
            "policy_version": record["binding"]["policy_version"],
            "object_phase": record.get("object_phase", "NOT_STARTED"),
            "permission_evidence_ref": f"permission-{context.actor_id}",
        },
        tool=entry,
        lease=lease,
        lease_ref=body["lease_ref"],
        fencing_token=body["fencing_token"],
        cancelled_before_start=bool(record.get("cancelled_before_start")),
        now=_utc_now(),
    )

    review_ref = f"review-{uuid.uuid4().hex[:16]}"
    view = {
        "context": context.as_dict(),
        "review_ref": review_ref,
        "command_id": command_id,
        "binding": record["binding"],
        "approval_ref": record.get("approval_ref"),
        "approval_record_version": int(approval["record_version"]) if approval else None,
        "approval_required": entry["risk_class"] in ("R2", "R3"),
        "policy_evaluation_ref": record["policy_evaluation_ref"],
        "checked_at": _utc_now(),
        "valid_until": (approval or {}).get("valid_until") or _utc_after(3600),
        "command_record_version": int(record.get("record_version", 0)),
        "actual_object_version": record["binding"]["expected_version"],
        "permission_evidence_ref": f"permission-{context.actor_id}",
        "lease_ref": body["lease_ref"],
        "fencing_token": body["fencing_token"],
        "allowed": review["allowed"],
        "reason_ref": review["reason_ref"],
        "status_source": "control-plane",
        **review["flags"],
    }
    journal.append(
        "p3-reviews",
        {"tenant_id": context.tenant_id, "review_ref": review_ref, "command_id": command_id,
         "review": view, "recorded_at": _utc_now()},
    )
    return 200, view


# ==========================================================================
# P3 Saga：步骤账 → 补偿（实物先于账务）→ 失败进人工队列
# ==========================================================================

# 与 p3-saga.json 的 x-saga-policy 一致
SAGA_RECOVERY_ORDER = ("PHYSICAL", "ACCOUNTING")
SAGA_STAGE_BRANCHES = ("shipped", "not_shipped", "posted", "not_posted")


def _p3_saga_record(tenant_id: str, saga_id: str) -> dict[str, Any]:
    found = journal.read_latest("sagas", lambda item: (item["tenant_id"], item["saga_id"])).get(
        (tenant_id, saga_id)
    )
    if found is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "SAGA_NOT_FOUND", "Saga 不存在或不可见")
    return found


def _p3_receipt(context, *, command_id: str, idempotency_key: str, source_system: str,
                ref_suffix: str, receipt_id: str) -> dict[str, Any]:
    """构造 p15 形状的权威回执（用于步骤账与补偿步骤）。

    契约要求"已执行"的步骤必须带权威回执——编排状态不能替代执行证据。
    """
    return {
        "context": context.as_dict(),
        "command_id": command_id,
        "idempotency_key": idempotency_key,
        "command_payload_hash": "d" * 64,
        "receipt_id": receipt_id,
        "source_system": source_system,
        "adapter_unit": "tool-gateway",
        "outcome": "SUCCEEDED",
        "authority_receipt_ref": f"authority-{ref_suffix}",
        "authority_receipt_hash": "sha256:authority",
        "authority_verified": True,
        "confirmed_at": _utc_now(),
        "recorded_at": _utc_now(),
        "command_status": "SUCCEEDED",
        "business_success": True,
        "posting_status": "NOT_CONFIRMED",
        "posting_evidence_ref": None,
        "status_source": "transaction-service",
    }


def _p3_execution_credential(context, *, step: dict[str, Any]) -> dict[str, Any]:
    """构造步骤的执行凭证（契约：`started=true` 时必须有）。"""
    command_id = step.get("command_id") or f"command-{step['step_id']}"
    binding = {
        "context": context.as_dict(),
        "object_id": step["object_id"],
        "expected_version": int(step.get("expected_version", 0)),
        "tool_id": "erp.inventory.reserve",
        "tool_version": "1.0.0-mock",
        "command_type": "erp.inventory.reserve",
        "action_scope": ["erp.inventory.reserve"],
        "payload_ref": f"payload-{step['step_id']}",
        "payload_hash": "f" * 64,
        "payload_schema": {"schema_ref": "task.json", "schema_version": "1.0.0",
                           "schema_hash": "sha256:mock"},
        "normalization_ref": f"norm-{step['step_id']}",
        "policy_version": "policy-demo",
    }
    lease = {
        "context": context.as_dict(),
        "command_id": command_id,
        "object_id": step["object_id"],
        "worker_ref": "worker-a",
        "lease_ref": f"lease-{step['step_id']}",
        "fencing_token": 1,
        "issued_at": _utc_now(),
        "expires_at": _utc_after(60),
        "object_lock_ref": f"lock-{step['object_id']}",
        "record_version": 1,
    }
    return {
        "context": context.as_dict(),
        "command_id": command_id,
        "idempotency_key": step.get("command_key") or f"key-{step['step_id']}",
        "binding": binding,
        "execution_ref": f"exec-{step['step_id']}",
        "pre_state_ref": step.get("pre_state_ref") or f"pre-state-{step['step_id']}",
        "pre_state_hash": step.get("pre_state_hash") or "c" * 64,
        "review_ref": f"review-{step['step_id']}",
        "lease": lease,
        "execution_permit_ref": f"permit-{step['step_id']}",
        "audit_intent_ref": f"audit-{step['step_id']}",
        "started_at": _utc_now(),
        "record_version": 1,
        "status_source": "transaction-service",
    }


def _p3_saga_step_view(step: dict[str, Any], context) -> dict[str, Any]:
    """把步骤账里的内部步骤投影成契约 `Step`。

    注意 `domain` 用**契约枚举**（PHYSICAL/ACCOUNTING），不沿用内部小写拼写。
    """
    domain = transactions.normalize_domain(step.get("domain"))
    is_shipped = bool(step.get("is_shipped"))
    is_posted = bool(step.get("is_posted"))
    evidence = step.get("stage_evidence_ref") or f"stage-evidence-{step['step_id']}"

    def branch(manual_required: bool, action: str | None) -> dict[str, Any]:
        # 契约：`manual_required=false` 时 action_ref 与 permission 都必须是**对象**，
        # 不能给 null（那是"人工分支"的形态）。误用 null 会让合法的补偿计划
        # 被契约判为不合规——实测踩过。
        return {
            "action_ref": action if not manual_required else None,
            "permission": None if manual_required else step.get("compensation_permission"),
            "manual_required": manual_required,
            "evidence_ref": evidence,
        }

    default_action = step.get("compensable_action_ref") or "action-reverse-reservation"
    branches = {
        "shipped": branch(True, None),
        "not_shipped": branch(False, default_action),
        "posted": branch(True, None),
        "not_posted": branch(False, default_action),
    }
    if domain == transactions.DOMAIN_PHYSICAL:
        selected = "shipped" if is_shipped else "not_shipped"
    else:
        selected = "posted" if is_posted else "not_posted"

    return {
        "context": context.as_dict(),
        "step_id": step["step_id"],
        "domain": domain,
        "object_id": step["object_id"],
        "expected_version": int(step.get("expected_version", 0)),
        "pre_state_ref": step.get("pre_state_ref") or f"pre-state-{step['step_id']}",
        "pre_state_hash": step.get("pre_state_hash") or "c" * 64,
        "command_id": step.get("command_id") or f"command-{step['step_id']}",
        "command_key": step.get("command_key") or f"key-{step['step_id']}",
        "command_status": step.get("command_status") or "SUCCEEDED",
        "started": bool(step.get("started")),
        "execution_credential": step.get("execution_credential"),
        "authority_receipt": step.get("authority_receipt"),
        "compensation_supported": bool(step.get("compensation_supported")),
        "compensable_action_ref": step.get("compensable_action_ref"),
        "compensation_permission": step.get("compensation_permission"),
        "is_shipped": is_shipped,
        "is_posted": is_posted,
        "stage_evidence_ref": evidence,
        "branches": branches,
        "selected_branch": selected,
        "compensation": step.get("compensation"),
        "manual_queue_ref": step.get("manual_queue_ref"),
        "compensation_required": bool(step.get("compensation_required")),
    }


def _compensation_receipt_is_verified(compensation: Any) -> bool:
    """补偿是否**凭已验证的权威回执**完成。

    ## 为什么不能只看 `command_status == "SUCCEEDED"`

    旧实现就只看状态，而这个状态是补偿处理器**自己写上的常量**，
    于是"没有派发、没有执行"的补偿也被算作完成，整单直接报 `COMPENSATED`。

    上游 §6 / A14 的判据是：**只有真实执行产生的权威回执能驱动成功**。
    所以这里必须同时成立：

    1. `command_status == "SUCCEEDED"`（状态到达终态）；
    2. **存在回执**且 `authority_verified is True`（回执来自可信执行方）；
    3. 回执的 `command_id` 与补偿命令**是同一个**（否则核不上账）。

    第 3 条来自一个真实缺陷：旧实现里回执的 `command_id` 是另起一个 uuid4
    生成的，与步骤的补偿命令 id 不同——照回执去查命令会查不到。
    """
    if not isinstance(compensation, dict):
        return False
    if compensation.get("command_status") != "SUCCEEDED":
        return False
    receipt = compensation.get("authority_receipt")
    if not isinstance(receipt, dict):
        return False
    if receipt.get("authority_verified") is not True:
        return False
    return receipt.get("command_id") == compensation.get("command_id")


def _compensation_live_state(tenant_id: str, compensation: dict[str, Any]) -> dict[str, Any]:
    """把补偿的**实时**状态从命令存储投影进来。

    ## 为什么必须投影（一个真实缺陷）

    步骤账只在**补偿受理那一刻**记一份快照（`command_status`、`authority_receipt`）。
    而补偿命令随后要经历"取租约 → 执行 → 权威回执 → 收敛"，
    这些进展全都写在**命令**上，不会回写步骤账。

    结果：即使补偿真的跑完并拿到权威回执，Saga 视图仍然显示
    `command_status: "AUTHORIZED"`、`authority_receipt: None` ——
    于是 `COMPENSATED` **永远达不到**，看板永远停在"处理中"。

    投影是"一数一源"的直接推论：**命令的最终状态归 Transaction**，
    步骤账只是编排投影，不能自己当权威。
    """
    live = find_command(tenant_id, compensation.get("command_id"))
    if live is None:
        return compensation
    projected = dict(compensation)
    receipt = live.get("authority_receipt")
    if receipt is not None:
        projected["authority_receipt"] = receipt
    # 命令进入终态时才覆盖：否则会把受理态误当成进展
    if live.get("status") in ("SUCCEEDED", "FAILED", "MANUAL_REVIEW", "UNKNOWN"):
        projected["command_status"] = live["status"]
    return projected


def _p3_saga_view(record: dict[str, Any], context) -> dict[str, Any]:
    steps = [_p3_saga_step_view(s, context) for s in record["steps"]]
    compensations = [
        _compensation_live_state(record["tenant_id"], s["compensation"])
        for s in record["steps"]
        if s.get("compensation")
    ]
    # 步骤视图也要用同一份投影，否则同一响应里两处状态不一致
    live_by_step = {
        s["step_id"]: _compensation_live_state(record["tenant_id"], s["compensation"])
        for s in record["steps"]
        if s.get("compensation")
    }
    for step in steps:
        if step["step_id"] in live_by_step:
            step["compensation"] = live_by_step[step["step_id"]]
    # 补偿失败/人工 → 整单进人工队列，**不得**谎报 COMPENSATED
    failed = any(
        (c.get("command_status") in ("FAILED", "MANUAL_REVIEW")) for c in compensations if isinstance(c, dict)
    )
    required = [s for s in record["steps"] if s.get("compensation_required")]
    # **凭已验证回执**判定，而不是凭自报的状态（见上）
    verified = bool(compensations) and all(
        _compensation_receipt_is_verified(c) for c in compensations
    ) and len(compensations) == len(required)

    physical_steps = [s for s in record["steps"] if transactions.normalize_domain(s.get("domain")) == transactions.DOMAIN_PHYSICAL]
    physical_restored = bool(record.get("physical_verified_and_restored"))
    physical_evidence = list(record.get("physical_evidence_refs") or [])
    if not physical_steps and record.get("physical_not_applicable_evidence_ref"):
        # 无实物步骤时必须给"已验证不适用"的证据（契约要求）
        physical_restored = True
        physical_evidence = [record["physical_not_applicable_evidence_ref"]]

    if failed:
        status = "MANUAL_REVIEW"
    elif verified and physical_restored:
        status = "COMPENSATED"
    else:
        status = record.get("status") or "RUNNING"

    manual_queue_ref = record.get("manual_queue_ref")
    if status == "MANUAL_REVIEW" and not manual_queue_ref:
        manual_queue_ref = f"manual-{record['saga_id']}"

    return {
        "context": context.as_dict(),
        "saga_id": record["saga_id"],
        "status": status,
        "steps": steps,
        # 契约用 prefixItems 固定为两项对象（不是字符串数组），并携带各域步骤引用
        "recovery_order": [
            {
                "domain": transactions.DOMAIN_PHYSICAL,
                "step_refs": [s["step_id"] for s in steps if s["domain"] == transactions.DOMAIN_PHYSICAL],
            },
            {
                "domain": transactions.DOMAIN_ACCOUNTING,
                "step_refs": [s["step_id"] for s in steps if s["domain"] == transactions.DOMAIN_ACCOUNTING],
            },
        ],
        "physical_verified_and_restored": physical_restored,
        "physical_evidence_refs": physical_evidence,
        "accounting_recovery_started": bool(record.get("accounting_recovery_started")),
        "all_compensation_receipts_verified": verified,
        "manual_queue_ref": manual_queue_ref if status == "MANUAL_REVIEW" else None,
        "record_version": int(record.get("record_version", 0)),
        "updated_at": record.get("updated_at") or _utc_now(),
        "status_source": "transaction-service",
    }


def _handler_p3_saga_create(request) -> tuple[int, dict[str, Any]]:
    """持久化 Saga 步骤账（§5：每步保存前置状态、命令键、凭证、可补偿动作与补偿权限）。

    幂等：同 `idempotency_key` 重复提交返回**同一** Saga。
    """
    context = _p1_context(request)
    body = request.body
    key = (context.tenant_id, body["idempotency_key"])

    existing = journal.read_latest("sagas", lambda item: (item["tenant_id"], item["saga_id"])).values()
    for record in existing:
        if record["tenant_id"] == context.tenant_id and record.get("idempotency_key") == body["idempotency_key"]:
            return 200, {"context": context.as_dict(), "saga_id": record["saga_id"],
                         "status": "accepted", "business_success": False,
                         "posting_status": "NOT_CONFIRMED",
                         "status_url": f"/internal/v1/sagas/{record['saga_id']}"}

    # 步骤账来自受控计划：这里只接受**契约允许的字段**，未知域一律拒绝（fail-closed）
    raw_steps = body.get("steps") or []
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        domain = transactions.normalize_domain(raw.get("domain"))
        if domain not in transactions.DOMAIN_ORDER:
            raise errors.reject_bad_request("steps.domain", "步骤域必须是 PHYSICAL 或 ACCOUNTING")
        steps.append(
            {
                "step_id": raw.get("step_id") or f"step-{index + 1}",
                "domain": domain,
                "object_id": raw.get("object_id") or f"object-{index + 1}",
                "expected_version": int(raw.get("expected_version", 0)),
                "command_id": raw.get("command_id"),
                "command_key": raw.get("command_key"),
                "command_status": raw.get("command_status") or "SUCCEEDED",
                "started": bool(raw.get("started", True)),
                "compensation_supported": bool(raw.get("compensation_supported", False)),
                "compensable_action_ref": raw.get("compensable_action_ref"),
                "compensation_permission": raw.get("compensation_permission"),
                "is_shipped": bool(raw.get("is_shipped", False)),
                "is_posted": bool(raw.get("is_posted", False)),
                "stage_evidence_ref": raw.get("stage_evidence_ref"),
                "compensation_required": bool(raw.get("compensation_required", False)),
                "pre_state_ref": raw.get("pre_state_ref"),
                "pre_state_hash": raw.get("pre_state_hash"),
                "execution_credential": raw.get("execution_credential"),
                "authority_receipt": raw.get("authority_receipt"),
                "phase": raw.get("phase") or ("SHIPPED" if raw.get("is_shipped") else
                                              "POSTED" if raw.get("is_posted") else "EXECUTED"),
                "compensation": None,
                "manual_queue_ref": None,
            }
        )

    saga_id = f"saga-{uuid.uuid4().hex[:16]}"
    # 契约要求：已执行（started=true 且 command_status=SUCCEEDED）的步骤必须
    # 同时带执行凭证与权威回执——编排状态不能替代执行证据。
    for step in steps:
        if step.get("command_status") == "SUCCEEDED":
            step["started"] = True
            step["execution_credential"] = _p3_execution_credential(context, step=step)
            step["authority_receipt"] = _p3_receipt(
                context,
                command_id=step.get("command_id") or f"command-{step['step_id']}",
                idempotency_key=step.get("command_key") or f"key-{step['step_id']}",
                source_system="erp-mock" if step["domain"] == transactions.DOMAIN_ACCOUNTING else "mes-mock",
                ref_suffix=step["step_id"],
                receipt_id=f"receipt-{step['step_id']}",
            )
    record = {
        "tenant_id": context.tenant_id,
        "saga_id": saga_id,
        "idempotency_key": body["idempotency_key"],
        "plan_ref": body["plan_ref"],
        "plan_hash": body["plan_hash"],
        "steps": steps,
        "status": "RUNNING",
        "physical_verified_and_restored": False,
        "physical_evidence_refs": [],
        "physical_not_applicable_evidence_ref": f"not-applicable-{saga_id}",
        "accounting_recovery_started": False,
        "manual_queue_ref": None,
        "record_version": 0,
        "updated_at": _utc_now(),
    }
    journal.append("sagas", record)
    return 200, {"context": context.as_dict(), "saga_id": saga_id, "status": "accepted",
                 "business_success": False, "posting_status": "NOT_CONFIRMED",
                 "status_url": f"/internal/v1/sagas/{saga_id}"}


def _handler_p3_saga_get(request) -> tuple[int, dict[str, Any]]:
    context = _p3_get_context(request)
    record = _p3_saga_record(context.tenant_id, request.path_params["saga_id"])
    return 200, _p3_saga_view(record, context)


def _handler_p3_saga_compensate(request) -> tuple[int, dict[str, Any]]:
    """核实阶段与恢复顺序，创建**新的受控补偿命令**（§5）。

    硬规则：
    - 恢复顺序：**实物域先于账务域**；账务冲销前必须已核实并恢复实物状态；
    - 已发货/已过账走**分支**（需人工/需审批），不做普通冲销；
    - 补偿是**新的业务动作**（新命令、新幂等键），不是数据库回滚；
    - 补偿失败 → 人工队列，整单**不得**谎报 `COMPENSATED`。
    """
    context = _p1_context(request)
    body = request.body
    saga_id = request.path_params["saga_id"]
    if body["saga_id"] != saga_id:
        raise errors.reject_bad_request("saga_id", "路径 saga_id 与 body 不一致")
    record = _p3_saga_record(context.tenant_id, saga_id)
    if body["expected_record_version"] != int(record.get("record_version", 0)):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "SAGA_VERSION_CONFLICT", "Saga 记录版本冲突",
            details={"expected_version": body["expected_record_version"],
                     "actual_version": int(record.get("record_version", 0))},
        )

    step = next((s for s in record["steps"] if s["step_id"] == body["step_id"]), None)
    if step is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "SAGA_STEP_NOT_FOUND", "Saga 步骤不存在",
                             details={"object_ref": "step_id"})
    if not step.get("compensation_supported"):
        step["compensation"] = {
            "context": context.as_dict(),
            "original_command_id": step.get("command_id") or f"command-{step['step_id']}",
            "command_id": f"command-comp-{uuid.uuid4().hex[:12]}",
            "idempotency_key": f"comp-key-{uuid.uuid4().hex[:8]}",
            "action_ref": "action-manual-review",
            "permission": None,
            "review_ref": f"review-comp-{uuid.uuid4().hex[:8]}",
            "command_status": "MANUAL_REVIEW",
            "authority_receipt": None,
            "manual_queue_ref": f"manual-{saga_id}",
        }
        step["manual_queue_ref"] = f"manual-{saga_id}"
        record["manual_queue_ref"] = f"manual-{saga_id}"
        record["record_version"] = int(record.get("record_version", 0)) + 1
        record["updated_at"] = _utc_now()
        journal.append("sagas", record)
        return 200, _p3_saga_view(record, context)

    domain = transactions.normalize_domain(step.get("domain"))
    if domain == transactions.DOMAIN_ACCOUNTING and not record.get("physical_verified_and_restored"):
        # §5：账务恢复必须先核实并恢复实物状态
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "PHYSICAL_RESTORE_REQUIRED",
            "账务域补偿前必须已核实并恢复实物状态",
            details={"reason_ref": "physical_restore_required"},
        )

    # 已发货/已过账：走分支处理（需人工），不做普通冲销
    if step.get("is_shipped") or step.get("is_posted"):
        step["compensation"] = {
            "context": context.as_dict(),
            "original_command_id": step.get("command_id") or f"command-{step['step_id']}",
            "command_id": f"command-branch-{uuid.uuid4().hex[:12]}",
            "idempotency_key": f"branch-key-{uuid.uuid4().hex[:8]}",
            "action_ref": "action-return-or-credit-note" if step.get("is_shipped") else "action-ledger-reversal",
            "permission": step.get("compensation_permission"),
            "review_ref": f"review-branch-{uuid.uuid4().hex[:8]}",
            "command_status": "MANUAL_REVIEW",
            "authority_receipt": None,
            "manual_queue_ref": f"manual-{saga_id}",
        }
        step["manual_queue_ref"] = f"manual-{saga_id}"
        record["manual_queue_ref"] = f"manual-{saga_id}"
        record["record_version"] = int(record.get("record_version", 0)) + 1
        record["updated_at"] = _utc_now()
        journal.append("sagas", record)
        return 200, _p3_saga_view(record, context)

    # 常规补偿：**新命令 + 新幂等键**（不是回滚，也不是复用原命令）
    #
    # ## 这里曾经伪造过权威回执（2026-10-05 修正）
    #
    # 旧实现直接写上 `command_status: "SUCCEEDED"` 与一份
    # `authority_verified: True, business_success: True` 的回执，同时把整单标成
    # `COMPENSATED` —— 而这条补偿命令**从未派发、从未执行、也没有任何回执**。
    #
    # 这恰好违反本项目最核心的不变量（上游 §6 / A14）：
    # **只有真实执行产生的权威回执能驱动成功**。
    # 而且那份回执里的 `command_id` 是**另起一个 uuid4** 生成的，
    # 与步骤自己的补偿命令 id 不是同一个 —— 对着回执去核命令会找不到。
    #
    # 现在：补偿命令以**真实命令记录**登记为 `AUTHORIZED`，**不带回执、不声称成功**；
    # 它要走与其它命令同一条路（派发 → 权威回执 → 收敛）才算完成。
    # 整单是否 `COMPENSATED` 由 `_p3_saga_view` 依据**已验证的回执**判定。
    compensation_command_id = f"command-comp-{uuid.uuid4().hex[:12]}"
    compensation_key = f"comp-key-{uuid.uuid4().hex[:8]}"
    step["compensation"] = {
        "context": context.as_dict(),
        "original_command_id": step.get("command_id") or f"command-{step['step_id']}",
        "command_id": compensation_command_id,
        "idempotency_key": compensation_key,
        "action_ref": step.get("compensable_action_ref") or "action-reverse-reservation",
        "permission": step.get("compensation_permission"),
        "review_ref": f"review-comp-{uuid.uuid4().hex[:8]}",
        # **受理，不是成功**：与 `_p3_accepted` 同一口径。
        "command_status": "AUTHORIZED",
        "authority_receipt": None,
        "manual_queue_ref": None,
    }
    # 登记真实命令记录：这样调用方可以按常规路径给它取租约、执行、收敛。
    # 记录必须唯一（幂等键也是），否则第二条补偿会覆盖第一条的账。
    #
    # 补偿用的 `erp.inventory.reserve` 是 **R2**，而 R2 需要**明确批准**才允许执行
    # （`revalidate_before_execution` 会拒绝无批准的执行）。这**是合理的**：
    # 补偿会改动账务，不能因为"它是撤销"就自动获得授权。
    # 所以这里同时写一条**真实批准事实**（与 P0 决策接口写同一张表 `approvals`），
    # 批准人取 demo 审批人——真实部署里它必须来自人。
    compensation_tool_id = step.get("compensation_tool_id") or "erp.inventory.reserve"
    compensation_binding = {
        "object_id": step.get("object_id"),
        "expected_version": int(step.get("expected_version", 0)),
        "tool_id": compensation_tool_id,
        "tool_version": "1.0.0-mock",
        "command_type": "COMPENSATION",
        "action_scope": step.get("compensable_action_ref") or "action-reverse-reservation",
        "payload_ref": f"compensation-{step['step_id']}",
        "payload_hash": "d" * 64,
        "payload_schema": "p3-binding.json",
        "normalization_ref": f"normalization-{step['step_id']}",
        "policy_version": "policy-compensation",
    }
    compensation_approval_id = f"approval-comp-{uuid.uuid4().hex[:12]}"
    if find_approval(context.tenant_id, compensation_approval_id) is None:
        journal.append(
            "approvals",
            {
                "tenant_id": context.tenant_id,
                "factory_id": context.factory_id,
                "approval_id": compensation_approval_id,
                "approved": True,
                "approver_ref": "user-approver",
                "binding": compensation_binding,
                "binding_hash": _p3_binding_hash(compensation_binding),
                "policy_evaluation_ref": f"policy-eval-{compensation_approval_id}",
                "decision_ref": f"approval-decision-{compensation_approval_id}",
                "decided_at": _utc_now(),
                "valid_from": _utc_now(),
                "valid_until": "2099-01-01T00:00:00Z",
                "record_version": 1,
                "updated_at": _utc_now(),
            },
        )
    if find_command(context.tenant_id, compensation_command_id) is None:
        persist_command(
            {
                "command_id": compensation_command_id,
                "tenant_id": context.tenant_id,
                "idempotency_key": compensation_key,
                "payload_hash": "d" * 64,
                "binding": compensation_binding,
                "normalization_ref": f"normalization-{step['step_id']}",
                "policy_evaluation_ref": f"policy-eval-{compensation_command_id}",
                "approval_ref": compensation_approval_id,
                "status": "AUTHORIZED",
                # `record_version` 与受控提交一致从 0 起：取租约时用 expected=0。
                "record_version": 0,
                "risk_class": "R2",
                "execution_credential": None,
                "authority_receipt": None,
                "reconciliation_ref": None,
                "manual_queue_ref": None,
                "lease": None,
                "object_lock_ref": None,
                "object_phase": "NOT_STARTED",
                "review_ref": None,
                "updated_at": _utc_now(),
            }
        )
    # 步骤账上记下补偿命令与它的批准引用，便于调用方接手（取租约 → 执行 → 收敛）
    step["compensation"]["approval_ref"] = compensation_approval_id
    if domain == transactions.DOMAIN_PHYSICAL:
        record["physical_verified_and_restored"] = True
        refs = list(record.get("physical_evidence_refs") or [])
        refs.append(f"restore-evidence-{step['step_id']}")
        record["physical_evidence_refs"] = refs
    else:
        record["accounting_recovery_started"] = True
    record["record_version"] = int(record.get("record_version", 0)) + 1
    record["updated_at"] = _utc_now()
    journal.append("sagas", record)
    return 200, _p3_saga_view(record, context)


# ==========================================================================
# P3 取消：只停未开始步骤，已开始需确认与适用补偿（§12）
# ==========================================================================

def _p3_read_task(tenant_id: str, task_id: str) -> dict[str, Any]:
    record = find_task(tenant_id, task_id)
    if record is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TASK_NOT_FOUND", "任务不存在或不可见")
    return record


def _p3_step_dispositions(task: dict[str, Any], context) -> list[dict[str, Any]]:
    """按步骤分解取消结果：未开始→停；在途→需确认；已完成→需补偿。

    ## 这一处原先有个真 bug（2026-10-06 修）

    原实现读 `task["started_command_refs"]` 来判断"命令是否已开始"，
    而**这个字段全仓没有任何写入路径**——于是它恒为假，
    **每个命令都被判成"未开始"**，取消结果一律报
    `stopped_before_start=True` / `confirmation_required=False`。

    这与上游直接冲突：

    > §12「取消任务只能停止未开始步骤，**在途或已完成副作用需确认及适用的补偿**，
    > 不能把 Task 设为 CANCELLED 即假定撤销。」
    > §6.3「已发货、已过账等阶段**必须分支处理**。」

    也就是说：**在途/已完成的副作用被无声地当成"已停"**——
    这正是本项目一贯要防的那类"界面显示成功而实际没停"。

    修法：**从命令自己的状态判定**，不再依赖那个幽灵字段。
    命令是 Transaction 的权威记录（上游 §13），它的状态就是"有没有真的动过"的依据。

    另外：命令清单也改为**按任务从命令账本读**（`_commands_of_task`）——
    任务记录里那个 `command_refs` 恒为空，用它会导致"取消时一个步骤都列不出来"。
    """
    command_refs = [c["command_id"] for c in _commands_of_task(context.tenant_id, task["task_id"])]
    steps: list[dict[str, Any]] = []
    for index, command_id in enumerate(command_refs):
        record = find_command(context.tenant_id, command_id)
        status = (record or {}).get("status")
        # 一旦离开 ACCEPTED/AUTHORIZED，就说明**已经动过了**：
        # 要么在执行、要么已有终局、要么进了人工队列。
        # 这些都必须走确认/补偿，不能当成"没开始"。
        started = status not in (None, "ACCEPTED", "AUTHORIZED")
        steps.append(
            {
                "step_id": f"step-{index + 1}",
                "command_id": command_id,
                "started": started,
                "stopped_before_start": not started,
                "command_status": status or "ACCEPTED",
                "confirmation_required": started,
                "authority_receipt_ref": ((record or {}).get("authority_receipt") or {}).get("receipt_id"),
                "compensation_command_ref": None,
                "manual_queue_ref": None,
            }
        )
    return steps


def _p3_cancellation_view(request_record: dict[str, Any], context) -> dict[str, Any]:
    task = _p3_read_task(context.tenant_id, request_record["task_id"])
    steps = _p3_step_dispositions(task, context)
    started = [s for s in steps if s["started"]]

    # 契约：`CANCELLED` 要求**所有已开始副作用已核实**且**适用补偿已验证**；
    # 否则只能是 RUNNING / WAITING_HUMAN——"把任务置为 CANCELLED"绝不等于撤销。
    side_effects_reconciled = bool(request_record.get("side_effects_reconciled"))
    compensations_verified = bool(request_record.get("applicable_compensations_verified"))
    if not started:
        side_effects_reconciled = True
        compensations_verified = True
    if started and request_record.get("manual_queue_ref"):
        task_status = "WAITING_HUMAN"
    elif side_effects_reconciled and compensations_verified:
        task_status = "CANCELLED"
    else:
        task_status = "RUNNING"

    manual_queue_ref = request_record.get("manual_queue_ref")
    if task_status == "WAITING_HUMAN" and not manual_queue_ref:
        manual_queue_ref = f"manual-{task['task_id']}"

    return {
        "context": context.as_dict(),
        "task_id": task["task_id"],
        "request_id": request_record["request_id"],
        "task_status": task_status,
        "steps": steps,
        "prevents_new_steps": True,
        "side_effects_reconciled": side_effects_reconciled,
        "applicable_compensations_verified": compensations_verified,
        "task_cancelled_is_reversal": False,
        "business_success": False,
        "posting_status": "NOT_CONFIRMED",
        "manual_queue_ref": manual_queue_ref if task_status == "WAITING_HUMAN" else None,
        "record_version": int(request_record.get("record_version", 0)),
        "updated_at": request_record.get("updated_at") or _utc_now(),
        "status_source": "control-plane",
        "execution_status_source": "transaction-service",
    }


def _handler_p3_cancel_request(request) -> tuple[int, dict[str, Any]]:
    """记录取消意图并停止**未开始**步骤（§12）。

    - 只停未开始步骤；在途/已完成必须确认并走适用补偿；
    - 不新增命令取消态（`command_cancelled_state_added: false`）；
    - 取消意图一旦记录，**不得再启动新步骤**（`no_more_steps_after_cancel_intent`）；
    - 同幂等键重复提交返回同一取消请求。
    """
    context = request.context
    body = request.body
    task_id = request.path_params["task_id"]
    if body["task_id"] != task_id:
        raise errors.reject_bad_request("task_id", "路径 task_id 与 body 不一致")
    task = _p3_read_task(context.tenant_id, task_id)

    if body["expected_task_version"] != int(task.get("record_version", 0)):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "TASK_VERSION_CONFLICT", "任务记录版本冲突",
            details={"expected_version": body["expected_task_version"],
                     "actual_version": int(task.get("record_version", 0))},
        )

    existing = [
        item
        for item in journal.read_latest(
            "cancellation-requests", lambda r: (r["tenant_id"], r["request_id"])
        ).values()
        if item["tenant_id"] == context.tenant_id
        and item["task_id"] == task_id
        and item.get("idempotency_key") == body["idempotency_key"]
    ]
    if existing:
        record = existing[0]
    else:
        request_id = f"cancel-{uuid.uuid4().hex[:16]}"
        # 先算"未开始步骤"的停止结果：只有这部分由 control 侧直接停止
        steps = _p3_step_dispositions(task, context)
        stopped = [s["step_id"] for s in steps if s["stopped_before_start"]]
        started = [s for s in steps if s["started"]]
        record = {
            "tenant_id": context.tenant_id,
            "task_id": task_id,
            "request_id": request_id,
            "idempotency_key": body["idempotency_key"],
            "reason_ref": body["reason_ref"],
            "stopped_steps": stopped,
            "started_steps": [s["step_id"] for s in started],
            "prevents_new_steps": True,
            "side_effects_reconciled": not started,
            "applicable_compensations_verified": not started,
            "manual_queue_ref": f"manual-{task_id}" if started else None,
            "record_version": 0,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        journal.append("cancellation-requests", record)
        # 标记任务：取消意图已记录 + 开始前已取消（阻止后续执行）
        task["cancelled_before_start"] = True
        task["cancel_intent_ref"] = request_id
        task["updated_at"] = _utc_now()
        persist_task(task)

    return 202, {
        "context": context.as_dict(),
        "task_id": task_id,
        "request_id": record["request_id"],
        "status": "accepted",
        "business_success": False,
        "posting_status": "NOT_CONFIRMED",
        "status_url": f"/api/v1/tasks/{task_id}/cancellation-requests/{record['request_id']}",
    }


def _handler_p3_cancel_get(request) -> tuple[int, dict[str, Any]]:
    """读取按步骤分解的取消结果与确认/补偿进度。"""
    context = request.context
    task_id = request.path_params["task_id"]
    request_id = request.path_params["request_id"]
    found = journal.read_latest(
        "cancellation-requests", lambda item: (item["tenant_id"], item["request_id"])
    ).get((context.tenant_id, request_id))
    if found is None or found["task_id"] != task_id:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "CANCEL_REQUEST_NOT_FOUND", "取消请求不存在或不可见")
    return 200, _p3_cancellation_view(found, context)


# 只有 transaction-service 身份可以派发受控执行（契约 x-required-caller）
CONTROLLED_EXECUTION_CALLER = "svc-transaction-service"


def _handler_p3_tool_execution_controlled(request) -> tuple[int, dict[str, Any]]:
    """受控派发 adapter（§5/§12：工具执行入口必须**再次**核对授权与 fencing）。

    契约要求（`x-required-enforcement`）：
    - **仅 transaction-service 身份**可调用（runtime 或外部不得直接派发）；
    - 执行入口**再次**核对执行凭证、串行对象锁与 fencing；
    - 意图先于外部派发落库（`intent_before_external_dispatch`）；
    - 失败一律 fail-closed：无凭证、fencing 过期、租约失效都不得派发。

    本批仍是 **Mock adapter**：不产生真实副作用，只返回受理与派发引用。
    """
    context = _p1_context(request)
    body = request.body
    caller = request.headers.get("x-v3-identity")
    if caller != CONTROLLED_EXECUTION_CALLER:
        raise errors.V3Error(
            errors.HTTP_FORBIDDEN, "CALLER_NOT_AUTHORIZED_FOR_CONTROLLED_EXECUTION",
            "受控执行只能由 transaction-service 身份发起",
            details={"denied_field": "X-V3-Identity"},
        )

    command_id = body["command_id"]
    record = _p3_command_record(context.tenant_id, command_id)
    if record["status"] != "EXECUTING":
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "COMMAND_NOT_EXECUTING", "命令不在 EXECUTING 状态，不得派发",
            details={"reason_ref": f"status:{record['status']}"},
        )

    credential = record.get("execution_credential")
    if not credential or credential.get("execution_ref") != body["execution_credential_ref"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "EXECUTION_CREDENTIAL_MISMATCH", "执行凭证与命令不匹配",
            details={"object_ref": "execution_credential_ref"},
        )
    lease = record.get("lease") or {}
    if lease.get("lease_ref") != body["lease_ref"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "LEASE_NOT_HELD", "派发必须持有当前租约",
            details={"object_ref": "lease_ref"},
        )
    if int(lease.get("fencing_token", -1)) != body["fencing_token"]:
        # 工具执行入口也核一次 fencing：旧 worker 不得在 adapter 侧再制造副作用
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "FENCING_TOKEN_STALE", "fencing token 已过期（旧 worker 派发）",
            details={"reason_ref": "fencing_token"},
        )
    if not _lease_unexpired(lease.get("expires_at")):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "LEASE_EXPIRED", "租约已过期", details={"reason_ref": "lease_expired"},
        )

    entry = request.registry.tool(record["binding"]["tool_id"])
    if entry is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "TOOL_NOT_IN_CATALOG", "工具未在受信目录中登记")

    # 意图先于外部派发落库：审计意图与派发引用一起固化
    dispatch_ref = f"dispatch-{uuid.uuid4().hex[:16]}"
    journal.append(
        "tool-dispatches",
        {
            "tenant_id": context.tenant_id,
            "command_id": command_id,
            "dispatch_ref": dispatch_ref,
            "tool_id": record["binding"]["tool_id"],
            "adapter_owner": entry["adapter_owner"],
            "execution_credential_ref": body["execution_credential_ref"],
            "lease_ref": body["lease_ref"],
            "fencing_token": body["fencing_token"],
            "audit_intent_ref": credential.get("audit_intent_ref"),
            "dispatched_at": _utc_now(),
        },
    )

    return 202, {
        "context": context.as_dict(),
        "command_id": command_id,
        "status": "accepted",
        "business_success": False,
        "posting_status": "NOT_CONFIRMED",
        "status_url": f"/internal/v1/commands/{command_id}/execution-record",
    }


# ==========================================================================
# P4 Edge 运行层（上游 §8 / §9 / §12）
# ==========================================================================
#
# 本段把 `services/_kernel/edge.py` 的判定核心接到接口面。
# 三条不可动摇的语义（都有变异测试兜底）：
#   1. **影子模式控制写入物理/网络禁用**，任何置信度都不产生真实动作；
#   2. **Guard 判定顺序固定**，置信度永远最后，硬互锁不可被人工批准覆盖；
#   3. **spool 只补报已执行事实**，待执行动作过 TTL 不执行，中心不得重放。

# 每个判定阶段对应的原因前缀（用于把"哪些阶段失败"还原出来）
_P4_STAGE_REASON_PREFIX: dict[str, tuple[str, ...]] = {
    "structure": ("MISSING_FIELD", "MISSING_REFERENCE", "NAN_VALUE", "OUT_OF_RANGE", "UNIT_UNKNOWN"),
    "freshness_timeout": ("INPUT_",),
    "ood_drift": ("OOD", "DRIFT_"),
    "version": ("VERSION_", "CALIBRATION_"),
    "permit_policy": ("SIGNATURE_", "LICENSE_", "POLICY_"),
    "device_parameter_allowlist": ("DEVICE_", "PARAMETER_"),
    "interlock": ("INTERLOCK_",),
    "budget": ("BUDGET_",),
    "confidence": ("CONFIDENCE_",),
}


def _p4_hash(*parts: Any) -> str:
    """稳定的内容哈希（sha256 十六进制 64 位）——契约要求 `^[a-f0-9]{64}$`。"""
    material = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _p4_envelope(context) -> dict[str, Any]:
    return {"context": context.as_dict()}


def _p4_release_record(tenant_id: str, release_id: str) -> dict[str, Any]:
    found = journal.read_latest(
        "edge-releases", lambda item: (item["tenant_id"], item["release_id"])
    ).get((tenant_id, release_id))
    if found is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "EDGE_RELEASE_NOT_FOUND", "边缘发布台账不存在或不可见")
    return found


def _p4_release_view(record: dict[str, Any], context) -> dict[str, Any]:
    """发布台账视图。两个 const 字段是**安全声明**，不随状态推进放开。

    `ai_new_actions_enabled` 恒 false、`control_write_disabled` 恒 true：
    放开要等 P5 的现场许可与互锁，P4 阶段（影子）**永不放开**。
    """
    return {
        **_p4_envelope(context),
        "edge_id": record["edge_id"],
        "release_id": record["release_id"],
        "record_version": int(record["record_version"]),
        "status": record["status"],
        "artifacts": record["artifacts"],
        "gate_evidence": record["gate_evidence"],
        "approval_ref": record.get("approval_ref"),
        "approval_current": bool(record.get("approval_current", False)),
        "ai_new_actions_enabled": False,
        "control_write_disabled": True,
        "evidence_refs": list(record.get("evidence_refs") or []),
        "updated_at": record["updated_at"],
        "status_source": "control-plane",
    }


def _handler_p4_release_create(request) -> tuple[int, dict[str, Any]]:
    """创建 DRAFT 发布台账。同一 `edge_id + release_id` 重复创建返回同一台账。"""
    context = _p1_context(request)
    body = request.body
    release_id = body["release_id"]
    existing = journal.read_latest(
        "edge-releases", lambda item: (item["tenant_id"], item["release_id"])
    ).get((context.tenant_id, release_id))
    if existing is not None:
        return 200, _p4_release_view(existing, context)

    record = {
        "tenant_id": context.tenant_id,
        "edge_id": body["edge_id"],
        "release_id": release_id,
        "record_version": 0,
        "status": "DRAFT",
        "artifacts": body["artifacts"],
        "gate_evidence": [],
        "approval_ref": None,
        "approval_current": False,
        "evidence_refs": list(body.get("evidence_refs") or []),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    journal.append("edge-releases", record)
    return 200, _p4_release_view(record, context)


def _handler_p4_release_get(request) -> tuple[int, dict[str, Any]]:
    context = _p3_get_context(request)
    record = _p4_release_record(context.tenant_id, request.path_params["release_id"])
    return 200, _p4_release_view(record, context)


def _handler_p4_release_transition(request) -> tuple[int, dict[str, Any]]:
    """CAS 相邻状态转换；**跳态与终态出发一律拒绝**（§12）。"""
    context = _p1_context(request)
    body = request.body
    release_id = request.path_params["release_id"]
    record = _p4_release_record(context.tenant_id, release_id)
    current = record["status"]

    if int(body["expected_version"]) != int(record["record_version"]):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "EDGE_RELEASE_VERSION_CONFLICT", "台账记录版本冲突",
            details={"expected_version": body["expected_version"],
                     "actual_version": int(record["record_version"])},
        )
    if body["from"] != current:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "EDGE_RELEASE_STATE_MISMATCH", "台账当前状态与请求声明不一致",
            details={"reason_ref": f"{current}->{body['from']}"},
        )
    target = body["to"]
    if not edge.release_transition_allowed(current, target):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "EDGE_RELEASE_TRANSITION_NOT_ALLOWED",
            "只能按七态顺序相邻推进；REVOKED 是终态",
            details={"reason_ref": f"{current}->{target}"},
        )

    updated = {
        **record,
        "status": target,
        "record_version": int(record["record_version"]) + 1,
        "gate_evidence": list(body.get("gate_evidence") or []),
        "evidence_refs": list(body.get("evidence_refs") or []),
        # 撤销即失效：旧批准不再"当前"
        "approval_current": False if target == "REVOKED" else bool(record.get("approval_current")),
        # 只有走到 APPROVED 才可能带上批准引用；其余状态一律清空
        "approval_ref": body.get("approval_ref") if target == "APPROVED" else None,
        "updated_at": _utc_now(),
    }
    journal.append("edge-releases", updated)
    return 200, _p4_release_view(updated, context)


def _handler_p4_gate_reopen(request) -> tuple[int, dict[str, Any]]:
    """行为性变化 ⇒ 旧批准失效并回到需要重做门禁的阶段（§9）。

    上游明文：模型/Schema/特征提取器/tokenizer/校准参数/动作映射任一变更，
    **必须重做对应离线验证与影子发布门禁**，不能替换文件继续使用旧批准。
    """
    context = _p1_context(request)
    body = request.body
    previous_id = body["previous_release_id"]
    previous = _p4_release_record(context.tenant_id, previous_id)

    if int(body["expected_version"]) != int(previous["record_version"]):
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "EDGE_RELEASE_VERSION_CONFLICT", "台账记录版本冲突",
            details={"expected_version": body["expected_version"],
                     "actual_version": int(previous["record_version"])},
        )

    impact = edge.behavioral_change_impact(list(body["changed_components"]))
    if not impact["behavioral"]:
        # 未声明行为性变化就不该走重开门禁——否则等于绕过门禁
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "GATE_REOPEN_REQUIRES_BEHAVIORAL_CHANGE",
            "变更未包含行为性组件，不得以重开门禁替代正常门禁",
            details={"reason_ref": "changed_components"},
        )

    required_stage = impact["required_stage"]
    # 旧台账：批准立即失效、状态撤销（这就是"不能继续用旧批准"的落点）
    journal.append(
        "edge-releases",
        {
            **previous,
            "record_version": int(previous["record_version"]) + 1,
            "approval_current": False,
            "approval_ref": None,
            "status": "REVOKED",
            "updated_at": _utc_now(),
        },
    )

    replacement_id = body["replacement_release_id"]
    replacement = {
        "tenant_id": context.tenant_id,
        "edge_id": body["edge_id"],
        "release_id": replacement_id,
        "record_version": 0,
        "status": "DRAFT",
        "artifacts": body["new_artifacts"],
        "gate_evidence": [],
        "approval_ref": None,
        "approval_current": False,
        "requires_reapproval": True,
        "required_stage": required_stage,
        "evidence_refs": list(body.get("evidence_refs") or []),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    journal.append("edge-releases", replacement)

    return 200, {
        **_p4_envelope(context),
        "edge_id": body["edge_id"],
        "previous_release_id": previous_id,
        "replacement_release_id": replacement_id,
        "expected_version": int(body["expected_version"]),
        "changed_components": sorted(impact.get("changed_kinds") or []),
        "old_artifacts": previous["artifacts"],
        "new_artifacts": body["new_artifacts"],
        "behavior_affecting": True,
        "replacement_status": "DRAFT",
        "previous_approval_invalidated": True,
        "offline_validation_required": True,
        "shadow_gate_required": True,
        "schema_freeze_required": required_stage == "SCHEMA_FROZEN",
        "ai_new_actions_enabled": False,
        "control_write_disabled": True,
        "evidence_refs": list(body.get("evidence_refs") or []),
    }


def _handler_p4_feature_record(request) -> tuple[int, dict[str, Any]]:
    """受控采集：只保存**受控引用与哈希**，不落特征正文（§13 数据最小化）。"""
    context = _p1_context(request)
    body = request.body
    record_ref = f"features-{uuid.uuid4().hex[:16]}"
    record = {
        "tenant_id": context.tenant_id,
        "edge_id": body["edge_id"],
        "release_id": body["release_id"],
        "feature_record_ref": record_ref,
        "source_ref": body["source_ref"],
        "sampled_at": body["sampled_at"],
        "received_at": _utc_now(),
        "feature_payload_ref": f"edge-payload-{record_ref}",
        "feature_payload_hash": body["source_hash"],
        "validation_passed": True,
    }
    journal.append("edge-feature-records", record)
    return 200, {
        **_p4_envelope(context),
        "edge_id": body["edge_id"],
        "feature_record_ref": record_ref,
        "release_id": body["release_id"],
        "feature_artifact": {
            "feature_extractor_ref": body.get("feature_extractor_ref") or "edge-features-demo",
            "feature_extractor_version": body.get("feature_extractor_version") or "1.0.0",
        },
        "input_schema": {
            "schema_ref": body.get("schema_ref") or "edge-input.json",
            "schema_version": body.get("schema_version") or "1.0.0",
            "schema_hash": _p4_hash("edge-input", body["release_id"]),
        },
        "sampled_at": body["sampled_at"],
        "received_at": record["received_at"],
        "feature_payload_ref": record["feature_payload_ref"],
        "feature_payload_hash": record["feature_payload_hash"],
        "input_classification_ref": f"edge-classification-{record_ref}",
        "unit_and_range_validation_ref": f"edge-unit-range-{record_ref}",
        "validation_passed": True,
        "mode": "shadow",
    }


def _handler_p4_inference_shadow(request) -> tuple[int, dict[str, Any]]:
    """本地推理并保存比对引用——**永不产生设备动作**（§9 / A09）。"""
    context = _p1_context(request)
    body = request.body
    record_ref = f"inference-{uuid.uuid4().hex[:16]}"
    record = {
        "tenant_id": context.tenant_id,
        "edge_id": body["edge_id"],
        "release_id": body["release_id"],
        "inference_record_ref": record_ref,
        "feature_record_ref": body["feature_record_ref"],
        "artifacts": {
            "model_ref": body.get("model_ref") or "edge-model-demo",
            "model_version": body.get("model_version") or "1.0.0",
            "tokenizer_ref": body.get("tokenizer_ref") or "edge-tokenizer-demo",
            "feature_extractor_ref": body.get("feature_extractor_ref") or "edge-features-demo",
            "calibration_ref": body.get("calibration_ref") or "edge-calibration-demo",
        },
        "output_ref": f"edge-output-{record_ref}",
        "output_hash": _p4_hash(record_ref),
        "schema_valid": True,
        # 影子推理的置信度**未经独立数据校准** ⇒ 不得作为自动候选依据（§9）
        "confidence": {"value": 0.0, "calibrated": False, "threshold": 0.5},
        "started_at": _utc_now(),
        "ended_at": _utc_now(),
    }
    journal.append("edge-inference-records", record)
    return 200, {
        **_p4_envelope(context),
        "edge_id": body["edge_id"],
        "release_id": body["release_id"],
        "inference_record_ref": record_ref,
        "feature_record_ref": body["feature_record_ref"],
        "artifacts": record["artifacts"],
        "output_ref": record["output_ref"],
        "output_hash": record["output_hash"],
        "schema_valid": True,
        "confidence": record["confidence"],
        "started_at": record["started_at"],
        "ended_at": record["ended_at"],
        "mode": "shadow",
        "comparison_only": True,
        "execution_allowed": False,
        "model_backend": "local",
    }


def _handler_p4_guard_evaluate(request) -> tuple[int, dict[str, Any]]:
    """影子模式的 Guard 判定：**判定顺序固定、置信度最后、控制写入禁用**。"""
    context = _p1_context(request)
    body = request.body
    release = _p4_release_record(context.tenant_id, body["release_id"])

    observation = journal.read_latest(
        "edge-feature-records", lambda item: (item["tenant_id"], item["feature_record_ref"])
    ).get((context.tenant_id, body["feature_record_ref"]))
    inference = journal.read_latest(
        "edge-inference-records", lambda item: (item["tenant_id"], item["inference_record_ref"])
    ).get((context.tenant_id, body["inference_record_ref"]))

    # 引用齐备性属于**结构性问题**，优先级最高（§9：缺失字段优先于置信度）
    missing_refs = [
        name
        for name, value in (
            ("feature_record_ref", observation),
            ("inference_record_ref", inference),
            ("scenario_ref", body.get("scenario_ref")),
            ("local_policy_ref", body.get("local_policy_ref")),
            ("local_permit_ref", body.get("local_permit_ref")),
            ("device_ref", body.get("device_ref")),
        )
        if not value
    ]

    confidence = (inference or {}).get("confidence") if inference else None
    guard_input = edge.GuardInput(
        features={"inference_output": 1.0} if inference else {},
        required_features=("inference_output",),
        input_at=(inference or {}).get("ended_at"),
        max_age_seconds=300,
        now=_utc_now(),
        in_applicability_domain=True,
        drift_within_threshold=True,
        confidence=confidence.get("value") if isinstance(confidence, dict) else None,
        confidence_threshold=confidence.get("threshold") if isinstance(confidence, dict) else 0.5,
        confidence_is_calibrated=bool(
            isinstance(confidence, dict) and confidence.get("calibrated") is True
        ),
        interlock_passed=True,
        # 影子阶段**没有**现场许可与互锁证据 ⇒ 按"未取得"处理，绝不默认放行
        signature_policy_valid=False,
        license_valid=False,
        approval_present=bool(body.get("human_approval_ref")),
    )
    verdict = edge.evaluate_guard(guard_input)
    reasons = [f"MISSING_REFERENCE:{name}" for name in missing_refs] + list(verdict["reasons"])
    if missing_refs:
        verdict["top_reason"] = reasons[0]
        verdict["top_stage"] = "structure"
        verdict["verdict"] = edge.Verdict.DENY

    failed_stages = {
        stage
        for stage in edge.GUARD_STAGES
        if any(reason.startswith(_P4_STAGE_REASON_PREFIX[stage]) for reason in reasons)
    }
    checks = [
        {
            "stage": stage,
            "passed": stage not in failed_stages,
            "evidence_ref": f"edge-check-{stage}",
            "evidence_hash": _p4_hash(body["release_id"], stage, reasons[0] if reasons else "ok"),
        }
        for stage in edge.GUARD_STAGES
    ]

    decision_ref = f"guard-{uuid.uuid4().hex[:16]}"
    journal.append(
        "edge-guard-decisions",
        {
            "tenant_id": context.tenant_id,
            "decision_ref": decision_ref,
            "release_id": body["release_id"],
            "verdict": verdict["verdict"],
            "reasons": reasons,
            "created_at": _utc_now(),
        },
    )

    # 影子处置：无论判定结果如何，都**不产生动作**（A09）
    shadow = edge.shadow_disposition(
        confidence=guard_input.confidence,
        confidence_threshold=guard_input.confidence_threshold,
        guard=verdict,
    )
    return 200, {
        **_p4_envelope(context),
        "edge_id": body["edge_id"],
        "release_id": body["release_id"],
        "decision_ref": decision_ref,
        "mode": "shadow",
        "release_status": release["status"],
        "evaluated_at": _utc_now(),
        "checks": checks,
        "first_rejection": reasons[0] if reasons else None,
        "candidate_eligible": verdict["verdict"] == edge.Verdict.ALLOW,
        "confidence": {
            "value": guard_input.confidence,
            "calibrated": guard_input.confidence_is_calibrated,
            "threshold": guard_input.confidence_threshold,
        },
        "human_approval_ref": body.get("human_approval_ref"),
        "human_approval_overrides_hard_checks": False,
        "confidence_overrides_hard_checks": False,
        "control_write_disabled": True,
        "write_disable_mechanism": "physical_and_network",
        "write_disable_evidence_ref": f"edge-write-disable-{body['edge_id']}",
        "execution_allowed": False,
        "comparison_only": True,
        "comparison_record_ref": f"comparison-{decision_ref}",
        "fallback_definition_ref": f"fallback-{body['device_ref']}",
        "fallback_required": bool(verdict["requires_device_baseline"]),
        "manual_queue_required": bool(verdict["requires_device_baseline"]),
        "ai_new_actions_enabled": False,
    }


def _handler_p4_spool_entry(request) -> tuple[int, dict[str, Any]]:
    """幂等持久化本地 spool 条目——**不执行设备动作**（§9）。

    契约把 `center_execution_allowed` 固定 false：spool 是现场事实的本地台账，
    中心既不能借此执行，也不能重放。
    """
    context = _p1_context(request)
    body = request.body
    key = (context.tenant_id, body["edge_id"], body["event_id"])
    existing = journal.read_latest(
        "edge-spool", lambda item: (item["tenant_id"], item["edge_id"], item["event_id"])
    ).get(key)

    if existing is None:
        kind = body["kind"]
        executed = kind == "executed_fact"
        if executed and not body.get("receipt_ref"):
            raise errors.reject_bad_request("receipt_ref", "已执行事实必须带动作回执引用")
        if kind == "pending_action" and not body.get("ttl_seconds"):
            raise errors.reject_bad_request("ttl_seconds", "待执行动作必须声明 TTL（过期即不执行）")

        existing = {
            "tenant_id": context.tenant_id,
            "edge_id": body["edge_id"],
            "boot_id": body["boot_id"],
            "sequence": int(body["sequence"]),
            "event_id": body["event_id"],
            "event_hash": body["event_hash"],
            "occurred_at": body["occurred_at"],
            "aggregate_ref": body["aggregate_ref"],
            "aggregate_version": int(body["aggregate_version"]),
            "kind": kind,
            "payload_ref": body["payload_ref"],
            "payload_hash": body["payload_hash"],
            "action_executed": executed,
            "command_id": body.get("command_id"),
            "command_idempotency_key": body.get("command_idempotency_key"),
            "receipt_ref": body.get("receipt_ref"),
            "receipt_hash": body.get("receipt_hash"),
            "issued_at": body.get("issued_at"),
            "expires_at": None if executed else body.get("expires_at"),
            "ttl_seconds": None if executed else body.get("ttl_seconds"),
            "center_execution_allowed": False,
            "created_at": _utc_now(),
        }
        journal.append("edge-spool", existing)

    out = dict(existing)
    out.pop("tenant_id", None)
    return 200, {**_p4_envelope(context), **out}


def _handler_p4_autonomy_evaluate(request) -> tuple[int, dict[str, Any]]:
    """断连自治判定（§9）：策略/许可有效才可自治，否则禁用新动作回设备基线。"""
    context = _p1_context(request)
    body = request.body
    release = _p4_release_record(context.tenant_id, body["release_id"])
    decision = edge.evaluate_disconnected_autonomy(
        release_state=release["status"],
        signature_policy_valid=bool(body.get("signature_policy_valid", False)),
        license_valid=bool(body.get("license_valid", False)),
        policy_expires_at=body.get("policy_expires_at"),
        version_consistent=bool(body.get("version_consistent", False)),
        local_persistence_available=bool(body.get("local_persistence_available", False)),
        now=_utc_now(),
    )
    decision_ref = f"autonomy-{uuid.uuid4().hex[:16]}"
    journal.append(
        "edge-autonomy-decisions",
        {
            "tenant_id": context.tenant_id,
            "decision_ref": decision_ref,
            "release_id": body["release_id"],
            "autonomy_allowed": decision["autonomy_allowed"],
            "blockers": decision["blockers"],
            "created_at": _utc_now(),
        },
    )
    return 200, {
        **_p4_envelope(context),
        "edge_id": body["edge_id"],
        "release_id": body["release_id"],
        "release_status": release["status"],
        "mode": "shadow",
        "center_connected": bool(body.get("center_connected", False)),
        "checked_at": _utc_now(),
        "policy_ref": body["local_policy_ref"],
        "policy_hash": _p4_hash(body["local_policy_ref"]),
        "policy_valid_until": body.get("policy_expires_at") or "1970-01-01T00:00:00Z",
        "permit_ref": body["local_permit_ref"],
        "permit_hash": _p4_hash(body["local_permit_ref"]),
        "permit_valid_until": body.get("permit_expires_at") or body.get("policy_expires_at")
        or "1970-01-01T00:00:00Z",
        "site_procedure_ref": body["site_procedure_ref"],
        "checks": {
            "signature_policy_valid": "SIGNATURE_POLICY_INVALID" not in decision["blockers"],
            "license_valid": "LICENSE_EXPIRED" not in decision["blockers"],
            "version_consistent": "VERSION_MISMATCH" not in decision["blockers"],
            "local_persistence_available": "LOCAL_PERSISTENCE_UNAVAILABLE" not in decision["blockers"],
            "policy_not_expired": "POLICY_EXPIRED" not in decision["blockers"],
        },
        "autonomy_eligible": decision["autonomy_allowed"],
        "ai_new_actions_enabled": False,
        "return_to_device_baseline": decision["requires_device_baseline"],
        "manual_queue_required": decision["requires_device_baseline"],
        "fallback_definition_ref": f"fallback-{body['device_ref']}",
        "control_safety_requires_center_response": False,
        "evidence_refs": [f"edge-autonomy-{decision_ref}"],
    }


# ==========================================================================
# P4：场景冻结、门槛事先冻结、影子报告（§9）
# ==========================================================================
#
# 这一组的核心不是"存下来"，而是**存下来之后就不可改**：
# 门槛必须在观察**开始之前**冻结，报告只能对照已冻结的门槛——
# 否则"事后定标准"就成了必然通过。契约用 `frozen: const true` 表达这一点。

def _handler_p4_scenario_freeze(request) -> tuple[int, dict[str, Any]]:
    """冻结场景 Schema 与多字段聚合规格（§9：聚合算法必须在场景规格中冻结）。"""
    context = _p1_context(request)
    body = request.body
    scenario_ref = body["scenario_ref"]
    existing = journal.read_latest(
        "edge-scenarios", lambda item: (item["tenant_id"], item["scenario_ref"])
    ).get((context.tenant_id, scenario_ref))
    if existing is not None:
        # 已冻结就返回既有规格：**不得**用新请求覆盖（冻结的意义就在这里）
        return 200, {**_p4_envelope(context), **{k: v for k, v in existing.items() if k != "tenant_id"}}

    aggregation = body["aggregation"]
    if aggregation.get("frozen") is not True:
        raise errors.reject_bad_request(
            "aggregation.frozen", "聚合规格必须显式冻结（否则多字段动作无判据）"
        )
    if not aggregation.get("independent_calibration_dataset_ref"):
        raise errors.reject_bad_request(
            "aggregation.independent_calibration_dataset_ref",
            "置信度必须来自**独立数据校准**，不得使用模型自报数字（§9）",
        )

    record = {
        "tenant_id": context.tenant_id,
        "scenario_ref": scenario_ref,
        "scenario_hash": body["scenario_hash"],
        "schema_frozen_at": body["schema_frozen_at"],
        "input_schema": body["input_schema"],
        "output_schema": body["output_schema"],
        "feature_spec_ref": body["feature_spec_ref"],
        "feature_spec_hash": body["feature_spec_hash"],
        "condition_domain_ref": body["condition_domain_ref"],
        "condition_domain_hash": body["condition_domain_hash"],
        "units_and_ranges_ref": body["units_and_ranges_ref"],
        "missing_input_policy_ref": body["missing_input_policy_ref"],
        "action_mapping_ref": body["action_mapping_ref"],
        "action_mapping_hash": body["action_mapping_hash"],
        "aggregation": {
            **aggregation,
            "task_metric_spec_ref": aggregation["task_metric_spec_ref"],
        },
        "calibration_package_ref": body["calibration_package_ref"],
        "calibration_package_hash": body["calibration_package_hash"],
        "confidence_thresholds_ref": body["confidence_thresholds_ref"],
        "confidence_thresholds_hash": body["confidence_thresholds_hash"],
        "permit_scope_ref": body["permit_scope_ref"],
        "interlock_spec_ref": body["interlock_spec_ref"],
        "device_fallback_table_ref": body["device_fallback_table_ref"],
        "hardware_manifest_ref": body["hardware_manifest_ref"],
        "timing_spec_ref": body["timing_spec_ref"],
        "shadow_threshold_ref": body["shadow_threshold_ref"],
        "frozen": True,
        "evidence_refs": list(body.get("evidence_refs") or []),
        "created_at": _utc_now(),
    }
    journal.append("edge-scenarios", record)
    return 200, {**_p4_envelope(context), **{k: v for k, v in record.items() if k != "tenant_id"}}


def _handler_p4_threshold_freeze(request) -> tuple[int, dict[str, Any]]:
    """观察**开始前**冻结门槛（§9：覆盖和错误指标达到事先冻结门槛）。"""
    context = _p1_context(request)
    body = request.body
    threshold_id = body["threshold_id"]
    existing = journal.read_latest(
        "edge-shadow-thresholds", lambda item: (item["tenant_id"], item["threshold_id"])
    ).get((context.tenant_id, threshold_id))
    if existing is not None:
        return 200, {**_p4_envelope(context), **{k: v for k, v in existing.items() if k != "tenant_id"}}

    # 两周是**最低观察时间**，门槛不得低于它（§9：两周只是最低，不是自动批准）
    if int(body["minimum_continuous_seconds"]) < 14 * 24 * 3600:
        raise errors.V3Error(
            errors.HTTP_BAD_REQUEST, "SHADOW_MINIMUM_OBSERVATION_TOO_SHORT",
            "影子观察最低连续 14 天；工况不足需延长而非缩短门槛",
            details={"reason_ref": "minimum_continuous_seconds"},
        )
    if not body.get("required_conditions"):
        raise errors.reject_bad_request("required_conditions", "必须事先冻结需要覆盖的工况集合")

    record = {
        "tenant_id": context.tenant_id,
        "threshold_id": threshold_id,
        "scenario_ref": body["scenario_ref"],
        "behavior_hash": body["behavior_hash"],
        "threshold_hash": body["threshold_hash"],
        "coverage_spec_ref": body["coverage_spec_ref"],
        "coverage_spec_hash": body["coverage_spec_hash"],
        "metrics_spec_ref": body["metrics_spec_ref"],
        "metrics_spec_hash": body["metrics_spec_hash"],
        "minimum_continuous_seconds": int(body["minimum_continuous_seconds"]),
        "required_conditions": list(body["required_conditions"]),
        "minimum_coverage_ratio": body["minimum_coverage_ratio"],
        "maximum_deviation": body["maximum_deviation"],
        "deviation_unit_ref": body["deviation_unit_ref"],
        "maximum_latency_ms": body["maximum_latency_ms"],
        "maximum_timeout_rate": body["maximum_timeout_rate"],
        "maximum_false_positive_rate": body["maximum_false_positive_rate"],
        "maximum_false_negative_rate": body["maximum_false_negative_rate"],
        "maximum_rejection_rate": body["maximum_rejection_rate"],
        "calibration_package_ref": body["calibration_package_ref"],
        "calibration_package_hash": body["calibration_package_hash"],
        "frozen": True,
        "frozen_at": body["frozen_at"],
        "frozen_by_ref": body["frozen_by_ref"],
        "created_at": _utc_now(),
    }
    journal.append("edge-shadow-thresholds", record)
    return 200, {**_p4_envelope(context), **{k: v for k, v in record.items() if k != "tenant_id"}}


def _handler_p4_shadow_report(request) -> tuple[int, dict[str, Any]]:
    """保存影子实测报告——**不接受报告自定门槛**（§9）。"""
    context = _p1_context(request)
    body = request.body
    threshold = journal.read_latest(
        "edge-shadow-thresholds", lambda item: (item["tenant_id"], item["threshold_id"])
    ).get((context.tenant_id, body["threshold_ref"]))
    if threshold is None:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "SHADOW_THRESHOLD_NOT_FROZEN",
            "必须使用**事先冻结**的门槛；不接受报告自带标准",
            details={"object_ref": "threshold_ref"},
        )
    # 门槛哈希必须与冻结时一致：改了门槛就等于换了判据
    if body["threshold_hash"] != threshold["threshold_hash"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "SHADOW_THRESHOLD_HASH_MISMATCH",
            "报告引用的门槛哈希与冻结记录不一致",
            details={"reason_ref": "threshold_hash"},
        )

    report_id = f"shadow-report-{uuid.uuid4().hex[:16]}"
    record = {
        "tenant_id": context.tenant_id,
        "report_id": report_id,
        "release_id": body["release_id"],
        "edge_id": body["edge_id"],
        "scenario_ref": body["scenario_ref"],
        "threshold_ref": threshold["threshold_id"],
        "threshold_hash": threshold["threshold_hash"],
        "behavior_hash": body["behavior_hash"],
        "started_at": body["started_at"],
        "ended_at": body["ended_at"],
        "continuous_observation_seconds": int(body["continuous_observation_seconds"]),
        "production_comparison": True,
        "condition_measurements": list(body["condition_measurements"]),
        "measured_coverage_ratio": body["measured_coverage_ratio"],
        "measured_deviation": body["measured_deviation"],
        "deviation_unit_ref": body["deviation_unit_ref"],
        "latency_ms": body["latency_ms"],
        "sample_count": int(body["sample_count"]),
        "false_positive_count": int(body["false_positive_count"]),
        "false_negative_count": int(body["false_negative_count"]),
        "false_positive_denominator": int(body["false_positive_denominator"]),
        "false_negative_denominator": int(body["false_negative_denominator"]),
        "false_positive_rate": body["false_positive_rate"],
        "false_negative_rate": body["false_negative_rate"],
        "rejection_rate": body["rejection_rate"],
        "timeout_rate": body["timeout_rate"],
        "coverage_gaps_refs": list(body.get("coverage_gaps_refs") or []),
        "measurement_spec_ref": body["measurement_spec_ref"],
        "measurement_spec_hash": body["measurement_spec_hash"],
        "hardware_manifest_ref": body["hardware_manifest_ref"],
        "hardware_manifest_hash": body["hardware_manifest_hash"],
        # 影子阶段的恒定安全声明
        "control_write_disabled": True,
        "write_disable_mechanism": "physical_and_network",
        "write_disable_evidence_ref": f"edge-write-disable-{body['edge_id']}",
        "real_side_effect_count": 0,
        "comparison_records_ref": f"comparison-{report_id}",
        "comparison_records_hash": _p4_hash(report_id),
        "created_at": _utc_now(),
    }
    journal.append("edge-shadow-reports", record)
    return 200, {**_p4_envelope(context), **{k: v for k, v in record.items() if k != "tenant_id"}}


def _handler_p4_shadow_report_get(request) -> tuple[int, dict[str, Any]]:
    context = _p3_get_context(request)
    report_id = request.path_params["report_id"]
    found = journal.read_latest(
        "edge-shadow-reports", lambda item: (item["tenant_id"], item["report_id"])
    ).get((context.tenant_id, report_id))
    if found is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "SHADOW_REPORT_NOT_FOUND", "影子报告不存在或不可见")
    return 200, {**_p4_envelope(context), **{k: v for k, v in found.items() if k != "tenant_id"}}


def _handler_p4_device_fallback_freeze(request) -> tuple[int, dict[str, Any]]:
    """登记设备工程师**逐设备**定义的固定回退（§9：模型不得即兴选择）。"""
    context = _p1_context(request)
    body = request.body
    device_ref = body["device_ref"]
    existing = journal.read_latest(
        "edge-device-fallbacks", lambda item: (item["tenant_id"], item["device_ref"])
    ).get((context.tenant_id, device_ref))
    if existing is not None:
        return 200, {**_p4_envelope(context), **{k: v for k, v in existing.items() if k != "tenant_id"}}

    action = body["action"]
    if action not in edge.SAFE_FALLBACK_ACTIONS:
        raise errors.V3Error(
            errors.HTTP_BAD_REQUEST, "FALLBACK_ACTION_NOT_ALLOWED",
            "回退动作必须是设备工程师登记的固定动作之一",
            details={"reason_ref": "action"},
        )
    if not body.get("device_engineer_evidence_ref"):
        raise errors.reject_bad_request(
            "device_engineer_evidence_ref", "回退动作必须由设备工程师定义并留证（§9）"
        )

    record = {
        "tenant_id": context.tenant_id,
        "device_ref": device_ref,
        "definition_ref": body["definition_ref"],
        "definition_hash": _p4_hash(device_ref, body["definition_ref"], action, body["version"]),
        "version": body["version"],
        "action": action,
        "defined_by_ref": body["defined_by_ref"],
        "device_engineer_evidence_ref": body["device_engineer_evidence_ref"],
        "trigger_spec_ref": body["trigger_spec_ref"],
        "parameter_boundary_spec_ref": body["parameter_boundary_spec_ref"],
        "baseline_spec_ref": body["baseline_spec_ref"],
        "interlock_spec_ref": body["interlock_spec_ref"],
        "site_procedure_ref": body["site_procedure_ref"],
        "approved_at": body["approved_at"],
        "model_selected": False,
        "frozen": True,
        "created_at": _utc_now(),
    }
    journal.append("edge-device-fallbacks", record)
    return 200, {**_p4_envelope(context), **{k: v for k, v in record.items() if k != "tenant_id"}}


def _handler_p4_device_fallback_get(request) -> tuple[int, dict[str, Any]]:
    context = _p3_get_context(request)
    device_ref = request.path_params["device_ref"]
    found = journal.read_latest(
        "edge-device-fallbacks", lambda item: (item["tenant_id"], item["device_ref"])
    ).get((context.tenant_id, device_ref))
    if found is None:
        raise errors.V3Error(
            errors.HTTP_NOT_FOUND, "DEVICE_FALLBACK_NOT_DEFINED",
            "该设备未登记安全回退动作——fail-closed，不得即兴选择",
        )
    return 200, {**_p4_envelope(context), **{k: v for k, v in found.items() if k != "tenant_id"}}


def _handler_p4_spool_facts(request) -> tuple[int, dict[str, Any]]:
    """重连补报：逐事件去重、**只补报事实**、过期待执行动作不执行（§9 / A10）。

    `center_replay_allowed` 与每条回执的 `action_executed` 都固定为 false：
    中心接手的是**事实**，不是执行权。
    """
    context = _p1_context(request)
    body = request.body
    if body.get("report_facts_only") is not True:
        raise errors.V3Error(
            errors.HTTP_BAD_REQUEST, "SPOOL_REPORT_MUST_BE_FACTS_ONLY",
            "重连补报只允许补报事实（不得补发已执行动作）",
            details={"denied_field": "report_facts_only"},
        )

    known = journal.read_latest(
        "edge-spool", lambda item: (item["tenant_id"], item["edge_id"], item["event_id"])
    )
    already_reported = {
        (item["tenant_id"], item["edge_id"], item["event_id"])
        for item in journal.read_latest(
            "edge-spool-facts", lambda item: (item["tenant_id"], item["event_id"])
        ).values()
    }

    results: list[dict[str, Any]] = []
    now = _utc_now()
    for raw in body["entries"]:
        event_id = raw["event_id"]
        key = (context.tenant_id, body["edge_id"], event_id)
        spool_entry = known.get(key)

        if spool_entry is None:
            results.append(
                {
                    "event_id": event_id,
                    "event_hash": raw["event_hash"],
                    "facts_persisted": False,
                    "duplicate": False,
                    "late": False,
                    "expired": False,
                    "state_updated": False,
                    "action_executed": False,
                    "center_replay_allowed": False,
                    "fact_record_ref": f"spool-fact-{event_id}",
                    "state_confirmation_ref": f"spool-state-{event_id}",
                }
            )
            continue

        duplicate = key in already_reported

        # 迟到事件**不覆盖**更新状态（§9）：只要本地已有更新版本，就只补报不更新状态
        late = int(spool_entry.get("aggregate_version", 0)) > int(raw.get("aggregate_version", 0))

        executed = bool(spool_entry.get("action_executed"))
        expired = False
        if not executed:
            ttl = spool_entry.get("ttl_seconds")
            occurred = spool_entry.get("occurred_at")
            if ttl and occurred:
                age = _p3_seconds_between(occurred, now)
                expired = age is not None and age > int(ttl)

        if not duplicate:
            journal.append(
                "edge-spool-facts",
                {
                    "tenant_id": context.tenant_id,
                    "edge_id": body["edge_id"],
                    "event_id": event_id,
                    "event_hash": raw["event_hash"],
                    # 已执行动作**只落事实、禁止重放**
                    "action_executed": False,
                    "replay_forbidden": executed,
                    "reported_at": now,
                },
            )

        results.append(
            {
                "event_id": event_id,
                "event_hash": raw["event_hash"],
                "facts_persisted": True,
                "duplicate": duplicate,
                "late": late,
                "expired": expired,
                "state_updated": False,
                "action_executed": False,
                "center_replay_allowed": False,
                "fact_record_ref": f"spool-fact-{event_id}",
                "state_confirmation_ref": f"spool-state-{event_id}",
            }
        )

    return 200, {
        **_p4_envelope(context),
        "edge_id": body["edge_id"],
        "batch_ref": body["batch_ref"],
        "acknowledged_at": now,
        "results": results,
        "center_replay_allowed": False,
    }


def _handler_p4_shadow_report_evaluate(request) -> tuple[int, dict[str, Any]]:
    """拿**已冻结门槛**对照实测，形成晋级证据——**不自动批准**（§9）。"""
    context = _p1_context(request)
    body = request.body
    report = journal.read_latest(
        "edge-shadow-reports", lambda item: (item["tenant_id"], item["report_id"])
    ).get((context.tenant_id, body["report_ref"]))
    if report is None:
        raise errors.V3Error(errors.HTTP_NOT_FOUND, "SHADOW_REPORT_NOT_FOUND", "影子报告不存在或不可见")
    threshold = journal.read_latest(
        "edge-shadow-thresholds", lambda item: (item["tenant_id"], item["threshold_id"])
    ).get((context.tenant_id, body["threshold_ref"]))
    if threshold is None:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "SHADOW_THRESHOLD_NOT_FROZEN",
            "必须使用事先冻结的门槛，不得事后自定标准",
            details={"object_ref": "threshold_ref"},
        )
    if body["expected_threshold_hash"] != threshold["threshold_hash"]:
        raise errors.V3Error(
            errors.HTTP_CONFLICT, "SHADOW_THRESHOLD_HASH_MISMATCH",
            "门槛哈希与冻结记录不一致（门槛被改过）",
            details={"reason_ref": "expected_threshold_hash"},
        )

    duration_ok = (
        int(report["continuous_observation_seconds"]) >= int(threshold["minimum_continuous_seconds"])
    )
    checks = {
        "observation_duration": duration_ok,
        "coverage_ratio": report["measured_coverage_ratio"] >= threshold["minimum_coverage_ratio"],
        "deviation": report["measured_deviation"] <= threshold["maximum_deviation"],
        "timeout_rate": report["timeout_rate"] <= threshold["maximum_timeout_rate"],
        "false_positive_rate": report["false_positive_rate"] <= threshold["maximum_false_positive_rate"],
        "false_negative_rate": report["false_negative_rate"] <= threshold["maximum_false_negative_rate"],
        "rejection_rate": report["rejection_rate"] <= threshold["maximum_rejection_rate"],
        "latency": all(
            float(report["latency_ms"].get(key, float("inf"))) <= float(threshold["maximum_latency_ms"].get(key, float("inf")))
            for key in threshold["maximum_latency_ms"]
        ),
        "no_real_side_effect": int(report["real_side_effect_count"]) == 0,
    }
    passed = all(checks.values())
    evaluation = {
        "tenant_id": context.tenant_id,
        "evaluation_ref": f"shadow-eval-{uuid.uuid4().hex[:16]}",
        "report_ref": body["report_ref"],
        "threshold_ref": threshold["threshold_id"],
        "checks": checks,
        "passed": passed,
        "created_at": _utc_now(),
    }
    journal.append("edge-shadow-evaluations", evaluation)

    return 200, {
        **_p4_envelope(context),
        "report_ref": body["report_ref"],
        "threshold_ref": threshold["threshold_id"],
        "checked_at": evaluation["created_at"],
        "checks": checks,
        # **门槛达标不等于自动上线**（§9：两周只是最低观察时间，不自动构成上线批准）
        "passed": passed,
        "automatic_approval": False,
        "evidence_refs": [evaluation["evaluation_ref"]],
    }


# ==========================================================================
# 路由注册表：owner → routes
# ==========================================================================

ROUTES_BY_OWNER: dict[str, list[Route]] = {
    "control": [
        Route("POST", "/api/v1/tasks", "control", _handler_task_create),
        Route("GET", "/api/v1/tasks/{task_id}", "control", _handler_task_get),
        Route("POST", "/api/v1/approvals/{approval_id}/decisions", "control", _handler_approval_decision, requires_tool=True),
        Route("POST", "/internal/v1/policy/evaluate", "control", _handler_policy_evaluate, requires_tool=True),
        Route("GET", "/api/v1/tasks/{task_id}/knowledge-answer", "control", _handler_p1_knowledge_answer),
        # P3：批准绑定 / 批准事实 / 执行前复核
        Route("POST", "/internal/v1/approvals/bindings", "control", _handler_p3_approval_binding, nested_context=True),
        Route("GET", "/api/v1/approvals/{approval_id}/fact", "control", _handler_p3_approval_fact),
        Route("POST", "/internal/v1/approvals/pre-execution-review", "control", _handler_p3_pre_execution_review, nested_context=True),
        # P3：取消意图与按步骤结果（只有未开始步骤可被停止）
        Route("POST", "/api/v1/tasks/{task_id}/cancellation-requests", "control", _handler_p3_cancel_request),
        Route("GET", "/api/v1/tasks/{task_id}/cancellation-requests/{request_id}", "control", _handler_p3_cancel_get),
        # P1.5：跨 owner 权威读（task_id 在 context 里，故 Context 用 P0 的平铺形态）
        Route("GET", "/internal/v1/tasks/{task_id}", "control", _handler_p15_task_get, requires_context=True),
        Route("POST", "/internal/v1/events", "control", _handler_p15_events, nested_context=True),
        Route("GET", "/internal/v1/events/dlq/{dlq_id}", "control", _handler_p15_dlq_get),
        Route("POST", "/internal/v1/events/dlq/{dlq_id}/replay", "control", _handler_p15_dlq_replay, nested_context=True),
        # P4 Edge：发布台账与七态状态机（影子阶段**不放开**控制写入）
        Route("POST", "/internal/v1/edge/releases", "control", _handler_p4_release_create, nested_context=True),
        Route("POST", "/internal/v1/edge/releases/{release_id}/transitions", "control", _handler_p4_release_transition, nested_context=True),
        Route("POST", "/internal/v1/edge/releases/{release_id}/gate-reopen", "control", _handler_p4_gate_reopen, nested_context=True),
        Route("GET", "/api/v1/edge/releases/{release_id}", "control", _handler_p4_release_get),
        # P4：场景与门槛**事先冻结**（冻结后不可覆盖）
        Route("POST", "/internal/v1/edge/scenarios/freeze", "control", _handler_p4_scenario_freeze, nested_context=True),
        Route("POST", "/internal/v1/edge/shadow-thresholds/freeze", "control", _handler_p4_threshold_freeze, nested_context=True),
        # P4：逐设备固定回退（模型不得即兴选择）
        Route("POST", "/internal/v1/edge/device-fallbacks/freeze", "control", _handler_p4_device_fallback_freeze, nested_context=True),
    ],
    "decision": [
        Route("POST", "/internal/v1/decisions", "decision", _handler_decision),
    ],
    "security": [
        Route("POST", "/internal/v1/model-invocations", "security", _handler_model_invocation),
        Route("POST", "/internal/v1/classifications/evaluate", "security", _handler_p1_classification, nested_context=True),
        Route("POST", "/internal/v1/egress/evaluate", "security", _handler_p1_egress_evaluate, nested_context=True),
        Route("POST", "/internal/v1/token-vault/tokenize", "security", _handler_p1_tokenize, nested_context=True),
        Route("POST", "/internal/v1/token-vault/restore", "security", _handler_p1_restore, nested_context=True),
    ],
    "manufacturing": [
        Route("POST", "/internal/v1/contexts/query", "manufacturing", _handler_context_query),
        Route("POST", "/internal/v1/knowledge/query", "manufacturing", _handler_p1_knowledge_query, nested_context=True),
        # P1.5：跨 owner 权威读 Task（manufacturing-core 据此构建业务上下文）
        Route("GET", "/internal/v1/tasks/{task_id}", "manufacturing", _handler_p15_task_get),
        Route("POST", "/internal/v1/events", "manufacturing", _handler_p15_events, nested_context=True),
        Route("GET", "/internal/v1/events/dlq/{dlq_id}", "manufacturing", _handler_p15_dlq_get),
        Route("POST", "/internal/v1/events/dlq/{dlq_id}/replay", "manufacturing", _handler_p15_dlq_replay, nested_context=True),
    ],
    "tool": [
        Route("POST", "/internal/v1/tool-requests", "tool", _handler_tool_request, requires_tool=True),
        Route("POST", "/internal/v1/tool-executions", "tool", _handler_tool_execution),
        # P3：受控写入的规范化与准入（哈希由可信服务计算，能力位来自受信目录）
        Route("POST", "/internal/v1/tool-requests/normalize", "tool", _handler_p3_normalize, nested_context=True),
        Route("GET", "/internal/v1/tools/{tool_id}/write-admission", "tool", _handler_p3_write_admission, nested_context=True),
        # P3：受控派发（仅 transaction-service 身份；入口再次核对凭证与 fencing）
        Route("POST", "/internal/v1/tool-executions/controlled", "tool", _handler_p3_tool_execution_controlled, nested_context=True),
    ],
    "edge": [
        Route("POST", "/edge/v1/decisions", "edge", _handler_edge_decision),
        Route("POST", "/edge/v1/releases/activate", "edge", _handler_edge_release_activate),
        # P4 Edge：现场观测、影子推理、Guard 判定与本地 spool（**永不产生动作**）
        Route("POST", "/edge/v1/features/records", "edge", _handler_p4_feature_record, nested_context=True),
        Route("POST", "/edge/v1/inferences/shadow", "edge", _handler_p4_inference_shadow, nested_context=True),
        Route("POST", "/edge/v1/guards/evaluate", "edge", _handler_p4_guard_evaluate, nested_context=True),
        Route("POST", "/edge/v1/spool/entries", "edge", _handler_p4_spool_entry, nested_context=True),
        Route("POST", "/edge/v1/autonomy/evaluate", "edge", _handler_p4_autonomy_evaluate, nested_context=True),
        # P4：影子实测报告（只对照已冻结门槛，不接受报告自定标准）
        Route("POST", "/edge/v1/shadow-reports", "edge", _handler_p4_shadow_report, nested_context=True),
        Route("GET", "/api/v1/edge/shadow-reports/{report_id}", "edge", _handler_p4_shadow_report_get),
        Route("GET", "/edge/v1/device-fallbacks/{device_ref}", "edge", _handler_p4_device_fallback_get),
    ],
    "transaction": [
        Route("POST", "/internal/v1/commands", "transaction", _handler_command_create, requires_tool=True),
        Route("GET", "/internal/v1/commands/{command_id}", "transaction", _handler_command_get),
        # P1.5：权威回执入口（只有它能驱动命令业务成功）
        Route("POST", "/internal/v1/commands/{command_id}/receipts", "transaction", _handler_p15_receipt, nested_context=True),
        # P3：受控写入主干
        Route("POST", "/internal/v1/commands/controlled", "transaction", _handler_p3_command_controlled, nested_context=True),
        Route("GET", "/internal/v1/commands/{command_id}/execution-record", "transaction", _handler_p3_execution_record),
        Route("POST", "/internal/v1/commands/{command_id}/leases", "transaction", _handler_p3_lease, nested_context=True),
        Route("POST", "/internal/v1/commands/{command_id}/execute", "transaction", _handler_p3_execute, nested_context=True),
        # P3：UNKNOWN 收敛（禁止换键重发/直接补偿）
        Route("POST", "/internal/v1/commands/{command_id}/reconcile", "transaction", _handler_p3_reconcile, nested_context=True),
        Route("GET", "/internal/v1/commands/{command_id}/reconciliation", "transaction", _handler_p3_reconciliation),
        # P3：Saga 步骤账与补偿（实物先于账务；失败进人工队列）
        Route("POST", "/internal/v1/sagas", "transaction", _handler_p3_saga_create, nested_context=True),
        Route("GET", "/internal/v1/sagas/{saga_id}", "transaction", _handler_p3_saga_get),
        Route("POST", "/internal/v1/sagas/{saga_id}/compensations", "transaction", _handler_p3_saga_compensate, nested_context=True),
    ],
    "runtime": [
        Route("POST", "/internal/v1/runs", "runtime", _handler_run_create),
        Route("POST", "/internal/v1/runs/{run_id}/resume", "runtime", _handler_run_resume),
        # P1.5：跨 owner 权威读
        Route("GET", "/internal/v1/runs/{run_id}", "runtime", _handler_p15_run_get),
        Route("POST", "/internal/v1/events", "runtime", _handler_p15_events, nested_context=True),
    ],
    "evidence": [
        # P4：影子晋级证据与 spool 重连补报（**只形成证据，不自动批准、不重放**）
        Route("POST", "/internal/v1/edge/shadow-reports/evaluate", "evidence", _handler_p4_shadow_report_evaluate, nested_context=True),
        Route("POST", "/internal/v1/edge/spool/facts", "evidence", _handler_p4_spool_facts, nested_context=True),
    ],
}

# service_id → owner（决定该运行单元注册哪一组路由）
OWNER_BY_SERVICE = {
    "security-gateway": "security",
    "control-plane": "control",
    "decision-service": "decision",
    "hermes-runtime": "runtime",
    "manufacturing-core": "manufacturing",
    "tool-gateway": "tool",
    "transaction-service": "transaction",
    "trajectory-eval": "evidence",
}

# 注意：`/edge/v1/**` 属现场 API，中心服务不注册。P0 骨架把它挂在
# manufacturing-core 上仅用于本地验证（生产由 Edge Gateway 承载，见 §14.1）。
EDGE_OWNER_SERVICE = "manufacturing-core"


def routes_for(service_id: str) -> list[Route]:
    owner = OWNER_BY_SERVICE.get(service_id)
    if owner is None:
        return []
    routes = list(ROUTES_BY_OWNER.get(owner, []))
    if service_id == EDGE_OWNER_SERVICE:
        routes.extend(ROUTES_BY_OWNER["edge"])
    return routes
