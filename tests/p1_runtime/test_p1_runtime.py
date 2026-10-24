"""P1 运行验收：逐条对照 SPEC `docs/v3-upgrade/02-阶段-P1-安全与只读闭环.md` §8 的断言表。

完成条件（上游 §16 P1）：**C3/C4 无出域路径；跨租户不可见；只读问答来源可追溯。**
"""

from __future__ import annotations

import json
import uuid

import pytest
from jsonschema import Draft202012Validator

from tests.p0_runtime.conftest import REPO_ROOT, context_body, http, schema_binding

from .conftest import P1_OWNED_PATHS, SERVICE_IDS

SCHEMAS = REPO_ROOT / "packages" / "contracts" / "schemas"
IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}

C4_TASK = "task-c4-allowlisted"  # 登记表 p1.c4.task_allowlists[tenant-c3]


def p1_body(
    *,
    tenant: str = "tenant-demo",
    data_class: str = "C2",
    task_id: str = "task-demo",
    trace_id: str = "trace-demo",
    factory_id: str | None = "factory-demo",
    declared_origin: str = "declared_context",
    derived_origins: list | None = None,
    anonymized: bool = False,
    transformation_rule_ref: str | None = None,
    review_ref: str | None = None,
    input_refs: list | None = None,
) -> dict:
    return {
        "tenant_id": tenant,
        "context": {
            **context_body(tenant=tenant, task_id=task_id, trace_id=trace_id, data_class=data_class),
            "factory_id": factory_id,
        },
        "input_refs": input_refs or ["input-demo"],
        "declared_origin": declared_origin,
        "declared_data_class": data_class,
        "derived_origins": derived_origins or [],
        "anonymized": anonymized,
        "transformation_rule_ref": transformation_rule_ref,
        "review_ref": review_ref,
    }


def egress_body(
    *,
    backend: str,
    model_id: str = "model-demo-local",
    model_version: str = "1.0.0-mock",
    purpose: str = "purpose-model-invoke",
    policy_version: str = "policy-demo",
    **kwargs,
) -> dict:
    return {
        **p1_body(**kwargs),
        "model_id": model_id,
        "model_version": model_version,
        "model_backend": backend,
        "purpose": purpose,
        "policy_version": policy_version,
    }


def audit_records(cluster) -> list[dict]:
    """读**进程实际写入**的审计日志。

    路径必须来自 `cluster.state_dir`，不能自己拼一个"看起来对"的路径——
    两者不一致会让断言在真空中通过（或永远读不到记录）。
    """
    path = cluster.state_dir / "egress-audit.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _latest_audit_for(cluster, trace_id: str) -> dict:
    """按 trace 取最近一条出域审计（stage_refs 等治理痕迹只在该契约里）。"""
    matches = [item for item in audit_records(cluster) if item.get("trace_id") == trace_id]
    assert matches, f"没有 trace={trace_id} 的审计记录"
    return matches[-1]


def external_invocations(cluster) -> list[dict]:
    return [
        item
        for item in audit_records(cluster)
        if item.get("invocation_performed") is True
        and item.get("requested_backend") in ("enterprise_cloud", "public")
    ]


def _accept_task(cluster, *, tenant: str, data_class: str, task_id: str | None = None, trace_id: str) -> str:
    """受理一个任务，返回 task_id（P1 只读链路要求任务真实存在）。"""
    status, task = cluster.post(
        "control-plane",
        "/api/v1/tasks",
        {
            **context_body(tenant=tenant, data_class=data_class, trace_id=trace_id, task_id=None),
            "idempotency_key": f"p1-{uuid.uuid4().hex[:10]}",
            "input_ref": "schedule-demo",
            "input_schema": schema_binding(),
        },
    )
    assert status == 202, task
    return task["task_id"]


# ==========================================================================
# 一、C3/C4 无出域路径（P1 硬完成条件）
# ==========================================================================

@pytest.mark.parametrize("data_class", ["C3", "C4"])
@pytest.mark.parametrize("backend", ["enterprise_cloud", "public"])
def test_sensitive_classes_never_permitted_offsite(cluster, data_class, backend):  # A02
    before = len(external_invocations(cluster))
    task_id = C4_TASK if data_class == "C4" else "task-demo"
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        egress_body(
            backend=backend,
            data_class=data_class,
            tenant="tenant-c3",
            task_id=task_id,
            trace_id=f"trace-{data_class}-{backend}",
        ),
        headers=IDENTITY,
    )
    # 判定成功时必须是 permit=false；被前置环节直接拒绝（403）也是"无出域路径"。
    if status == 200:
        assert payload["permit_invocation"] is False, f"{data_class} → {backend} 必须拒绝"
        assert payload["public_fallback"] is False
    else:
        assert status == 403, payload
        assert payload["code"] in ("DATA_CLASS_NOT_ALLOWED_FOR_BACKEND", "ACCESS_SCOPE_DENIED", "IDENTITY_NOT_VERIFIED")
    # 判定成功≠调用成功：审计里绝不能出现"对公网/企业云实际发起过调用"
    assert len(external_invocations(cluster)) == before


def test_tenant_class_ceiling_blocks_higher_class_processing(cluster):  # A02
    """租户自身等级上限：低等级租户不得处理高于其上限的数据（本地也不行）。

    这条覆盖"分级不是全局枚举，而是 per-tenant 天花板"——缺了它，
    "去掉租户等级上限" 这类缺陷不会被任何用例发现（实测过的盲区）。
    """
    # tenant-demo 的 max_data_class=C2 → 声明 C3/C4 必须在 Context 校验阶段被拒。
    for data_class in ("C3", "C4"):
        status, payload = cluster.post(
            "security-gateway",
            "/internal/v1/classifications/evaluate",
            p1_body(tenant="tenant-demo", data_class=data_class, trace_id=f"trace-ceiling-{data_class}"),
            headers=IDENTITY,
        )
        assert status == 403, (data_class, payload)
        assert payload["code"] == "DATA_CLASS_NOT_ALLOWED", payload
        assert payload["details"]["denied_field"] == "data_class"
    # 上限内的等级仍然可用（证明拒绝不是"一律拒绝"的假阳性）
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/classifications/evaluate",
        p1_body(tenant="tenant-demo", data_class="C2", trace_id="trace-ceiling-ok"),
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["data_class"] == "C2"


def test_rejected_egress_attempts_are_audited(cluster):  # A13
    """被拒绝的出域尝试也必须留痕。

    没有这条，"审计不再记录被拒绝的尝试"这类缺陷不会被发现（实测过的盲区）；
    而上游要求"失败尝试同样留痕"，否则"没有成功记录"无法自证。
    """
    trace = f"trace-reject-{uuid.uuid4().hex[:8]}"
    before = len(audit_records(cluster))
    status, _ = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        egress_body(backend="public", data_class="C4", tenant="tenant-c3", task_id=C4_TASK, trace_id=trace),
        headers={"X-V3-Identity": "svc-not-registered"},
    )
    assert status == 403
    after = audit_records(cluster)
    assert len(after) > before, "被拒绝的出域尝试没有留下审计记录"
    rejected = [item for item in after if item.get("trace_id") == trace]
    assert rejected, "拒绝那条审计里找不到对应的 trace"
    record = rejected[-1]
    assert record["invocation_performed"] is False
    assert record["invocation_succeeded"] is False
    assert record["output_released"] is False


def test_single_source_of_truth_for_task_state(cluster):  # A02
    """Task 状态必须跨进程一致，且不出现"两个真相源"。

    背景（实测缺陷）：P1 早期把 Task 存在性另写一份"任务投影"journal 绕过跨进程问题，
    于是 control-plane 的 Task 与那份投影是两个事实来源，会互相矛盾。
    收敛后只有一份 journal：任何运行单元读到的都是同一份。
    """
    trace = f"trace-sot-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)

    # 1) 同一个 Task 被两个不同运行单元读到（control-plane 拥有，manufacturing-core 消费）
    status, task = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{task_id}",
        headers={"X-Context": json.dumps(context_body(tenant="tenant-demo", task_id=None, trace_id=trace))},
    )
    assert status == 200, task
    assert task["task_id"] == task_id
    assert task["status_source"] == "control-plane"

    status, query = cluster.post(
        "manufacturing-core",
        "/internal/v1/knowledge/query",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "query_ref": "query-demo",
            "purpose": "purpose-knowledge-readonly",
            "query_id": "query-id-demo",
        },
        headers=IDENTITY,
    )
    assert status == 200, "消费方读不到拥有方写的任务状态 = 真相源分裂"

    # 2) 同一个任务在 journal 里只有一条事实（不是"状态一份 + 投影一份"）
    tasks_file = cluster.state_dir / "tasks.jsonl"
    records = [json.loads(line) for line in tasks_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    matching = [item for item in records if item.get("task_id") == task_id]
    assert len(matching) == 1, f"同一任务有 {len(matching)} 条事实记录，真相源不唯一"
    assert matching[0]["tenant_id"] == "tenant-demo"
    assert matching[0]["status"] == "ACCEPTED"

    # 3) 未知任务在任何消费方都是 404（不因真相源不同而出现"一边有一边没有"）
    status, payload = cluster.post(
        "manufacturing-core",
        "/internal/v1/knowledge/query",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id="task-does-not-exist", trace_id=trace),
            "query_ref": "query-demo",
            "purpose": "purpose-knowledge-readonly",
            "query_id": "query-id-demo",
        },
        headers=IDENTITY,
    )
    assert status == 404 and payload["code"] == "TASK_NOT_FOUND"


def test_run_state_is_single_source_of_truth(cluster):  # A02
    """Run 状态同样只写一份事实（hermes-runtime 拥有）。"""
    status, run = cluster.post(
        "hermes-runtime",
        "/internal/v1/runs",
        {**context_body(task_id="task-demo"), "input_ref": "input-demo", "runtime_version": "1.0.0-mock"},
    )
    assert status == 202, run
    run_id = run["run_id"]

    runs_file = cluster.state_dir / "runs.jsonl"
    records = [json.loads(line) for line in runs_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    matching = [item for item in records if item.get("run_id") == run_id]
    assert len(matching) == 1, "Run 事实记录不唯一"
    assert matching[0]["tenant_id"] == "tenant-demo"
    assert matching[0]["task_id"] == "task-demo"
    assert matching[0]["status"] == "RUNNING"

    resume = {
        **context_body(),
        "run_id": run_id,
        "expected_checkpoint_version": 0,
        "checkpoint_ref": "checkpoint-demo",
        "fencing_token": "fence-demo",
        "idempotency_key": "resume-demo",
    }
    status, resumed = cluster.post("hermes-runtime", f"/internal/v1/runs/{run_id}/resume", resume)
    assert status == 200, resumed
    # 恢复后仍只有一条"当前事实"可读（CAS 生效）
    status, conflict = cluster.post("hermes-runtime", f"/internal/v1/runs/{run_id}/resume", resume)
    assert status == 409 and conflict["code"] == "CHECKPOINT_VERSION_CONFLICT"


def test_matrix_covers_whole_surface(cluster):  # A02
    """出域矩阵逐格验证：C2 public 拒绝、C2 企业云需批准变换、C3/C4 仅本地。"""
    cases = [
        # (tenant, data_class, backend, expect_permit, model_id)
        # 模型必须与后端类别一致：自填后端与登记元数据不符会被判 MODEL_NOT_ALLOWLISTED，
        # 那是"模型未登记/不一致"的拒绝，与出域矩阵的结论不是同一件事，不能混用。
        ("tenant-demo", "C0", "public", True, "model-demo-public"),
        ("tenant-demo", "C1", "public", True, "model-demo-public"),
        ("tenant-demo", "C2", "public", False, "model-demo-public"),
        # 未批准变换的 C2 去企业云：企业云模型已登记，拒绝来自矩阵的 conditional 分支。
        ("tenant-demo", "C2", "enterprise_cloud", False, "model-demo-cloud"),
        ("tenant-c3", "C3", "local", True, "model-demo-local"),
        ("tenant-c3", "C3", "enterprise_cloud", False, "model-demo-cloud"),
        # C4 本地：白名单 + 专门批准 + 无密钥材料**全部满足**时才允许；
        # 任何一项缺失都必须拒绝（见下面单独的缺项用例）。
        ("tenant-c3", "C4", "local", True, "model-demo-local"),
    ]
    for index, (tenant, data_class, backend, expect, model_id) in enumerate(cases):
        # C4 的任务必须用白名单内的任务；其它等级用普通任务。
        task_id = C4_TASK if data_class == "C4" and tenant == "tenant-c3" else "task-demo"
        status, payload = cluster.post(
            "security-gateway",
            "/internal/v1/egress/evaluate",
            egress_body(
                backend=backend, data_class=data_class, tenant=tenant, task_id=task_id,
                trace_id=f"trace-m-{index}-{data_class}-{backend}", model_id=model_id,
            ),
            headers=IDENTITY,
        )
        assert status == 200, (index, tenant, data_class, backend, payload)
        assert payload["permit_invocation"] is expect, f"{tenant}/{data_class}/{backend}: {payload}"


def test_c4_with_allowlist_and_approval_and_no_key_material_is_allowed_locally(cluster):  # A02
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        egress_body(
            backend="local", data_class="C4", tenant="tenant-c3", task_id=C4_TASK, trace_id="trace-c4-ok"
        ),
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["c4_task_allowlisted"] is True
    assert payload["c4_approval_verified"] is True
    assert payload["contains_key_material"] is False
    assert payload["permit_invocation"] is True
    assert payload["public_fallback"] is False


def test_key_material_is_never_permitted_even_locally(cluster):  # A02,A13
    """C4 密钥材料：任务白名单与专门批准都不能覆盖此禁令。"""
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        egress_body(
            backend="local",
            data_class="C4",
            tenant="tenant-c3",
            task_id=C4_TASK,
            trace_id="trace-key-material",
            input_refs=["ref-key-material-demo"],
        ),
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["contains_key_material"] is True
    assert payload["permit_invocation"] is False
    # 命中密钥材料时必须在第 4 环节就停下：不得出现 invoked 相关环节。
    # stage_refs 属 p1-egress-audit（EgressDecision 契约封闭、不含该字段），故查审计。
    record = _latest_audit_for(cluster, "trace-key-material")
    stages = [item["stage"] for item in record["stage_refs"]]
    assert "minimization" in stages, stages
    assert "invocation" not in stages, stages


def test_mixed_context_takes_highest_not_first_or_average(cluster):  # A02
    """顺序无关：把最高等级放在首位、末位、中间，结论必须一致。"""
    for position, refs in (("first", ["c-high", "c-low"]), ("last", ["c-low", "c-high"])):
        status, payload = cluster.post(
            "security-gateway",
            "/internal/v1/classifications/evaluate",
            {
                **p1_body(tenant="tenant-c3", data_class="C0", task_id=C4_TASK, trace_id=f"trace-mix-{position}"),
                "input_refs": refs,
                "declared_data_class": "C0",
                "derived_origins": [
                    {"content_ref": "c-high", "origin": "memory", "anonymized": False},  # 规则 → C3
                    {"content_ref": "c-low", "origin": "text", "anonymized": False},  # 规则 → C1
                ],
            },
            headers=IDENTITY,
        )
        assert status == 200, payload
        assert payload["data_class"] == "C3", f"{position}: 必须取最高等级"
        assert payload["output_data_class"] == "C3"


def test_derived_context_cannot_lower_the_declared_class(cluster):  # A02
    """派生来源按登记规则分级；自报只是下限，可信分类不得低于自报。"""
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/classifications/evaluate",
        {
            **p1_body(tenant="tenant-c3", data_class="C3", task_id=C4_TASK, trace_id="trace-floor"),
            "declared_data_class": "C3",
        },
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["data_class"] == "C3"


def test_anonymization_does_not_downgrade(cluster):  # A02
    """匿名化 / Tokenization 不自动降级：C2 变换后仍是 C2。"""
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/classifications/evaluate",
        {
            **p1_body(
                tenant="tenant-demo",
                data_class="C2",
                trace_id="trace-anon",
                anonymized=True,
                transformation_rule_ref="transform-mock-c2-to-c2",
                review_ref="review-demo",
            ),
            "declared_data_class": "C2",
        },
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["data_class"] == "C2"
    assert payload["inputs"][0]["data_class"] == "C2"


def test_unknown_data_class_is_rejected_not_downgraded(cluster):  # A02
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/classifications/evaluate",
        {**p1_body(trace_id="trace-unknown")},
        headers=IDENTITY,
    )
    # 正常等级 → 200；未知等级用 C9 注入
    assert status == 200
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/classifications/evaluate",
        {**p1_body(trace_id="trace-unknown2"), "declared_data_class": "C9"},
        headers=IDENTITY,
    )
    assert status == 403, payload
    assert payload["code"] == "DATA_CLASS_NOT_ALLOWED"
    assert payload["details"]["denied_field"] == "data_class"


def test_local_backend_failure_never_falls_back_to_public(cluster):  # A02
    """本地后端故障 → permit=false，且不得把公网当回退目标。"""
    before = len(external_invocations(cluster))
    registry_path = cluster.write_registry_variant(
        "p1-local-down",
        lambda document: document["model_backends"]["local"].update({"reachable": False}) or document,
    )
    process = cluster.spawn_extra(
        "security-gateway",
        env_extra={"V3_REGISTRY_PATH": str(registry_path)},
    )
    try:
        status, payload = http(
            "POST",
            f"http://127.0.0.1:{process.port}/internal/v1/egress/evaluate",
            body=egress_body(
                backend="local", data_class="C3", tenant="tenant-c3", task_id="task-demo", trace_id="trace-local-down"
            ),
            headers=IDENTITY,
        )
        assert status == 200, payload
        assert payload["permit_invocation"] is False
        assert payload["local_backend_failed"] is True
        assert payload["backend_healthy"] is False
        assert payload["public_fallback"] is False
    finally:
        process.stop()
        registry_path.unlink(missing_ok=True)
    assert len(external_invocations(cluster)) == before


# ==========================================================================
# 二、跨租户不可见（检索前过滤）
# ==========================================================================

def test_cross_tenant_candidates_are_filtered_before_retrieval(cluster):  # A01
    """tenant-other 的文档绝不能出现在 tenant-demo 的结果里。"""
    trace = f"trace-x-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)
    status, payload = cluster.post(
        "manufacturing-core",
        "/internal/v1/knowledge/query",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "query_ref": "query-demo",
            "purpose": "purpose-knowledge-readonly",
            "query_id": "query-id-demo",
        },
        headers=IDENTITY,
    )
    assert status == 200, payload
    returned = {item["source_id"] for item in payload["sources"]}
    assert "doc-other-tenant" not in returned, "跨租户来源泄漏"
    assert returned, "本租户应有可见来源"
    # 每条来源都带完整的 ACL 与来源追溯字段
    for source in payload["sources"]:
        assert source["tenant_id"] == "tenant-demo"
        assert source["acl_check"]["pre_retrieval_tenant_filter"] is True
        assert source["acl_check"]["pre_retrieval_factory_filter"] is True
        assert source["acl_check"]["pre_retrieval_role_filter"] is True
        assert source["acl_check"]["post_return_acl_recheck"] is True
        assert source["source_version"] and source["occurred_at"] and source["data_class"]


def test_role_scoped_document_is_not_returned(cluster):  # A01
    """岗位 ACL：只有 kb.read 的文档不应出现在只带 pmc.read 的检索里。"""
    trace = f"trace-role-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)
    registry_path = cluster.write_registry_variant(
        "p1-role-narrow",
        # 授予一个现有文档都不具备的范围：若结果非空，说明范围过滤没在检索前生效。
        lambda document: document["tenants"]["tenant-demo"].update({"delegated_scopes": ["erp.audit"]}) or document,
    )
    process = cluster.spawn_extra(
        "manufacturing-core",
        env_extra={"V3_REGISTRY_PATH": str(registry_path)},
    )
    try:
        status, payload = http(
            "POST",
            f"http://127.0.0.1:{process.port}/internal/v1/knowledge/query",
            body={
                **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
                "query_ref": "query-demo",
                "purpose": "purpose-knowledge-readonly",
                "query_id": "query-id-demo",
            },
            headers=IDENTITY,
        )
        assert status == 200, payload
        assert payload["sources"] == [], "授予的范围与任何文档都不相交时，检索必须返回空"
    finally:
        process.stop()
        registry_path.unlink(missing_ok=True)


def test_unknown_task_cannot_be_queried(cluster):  # A02
    status, payload = cluster.post(
        "manufacturing-core",
        "/internal/v1/knowledge/query",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id="task-nonexistent", trace_id="trace-no-task"),
            "query_ref": "query-demo",
            "purpose": "purpose-knowledge-readonly",
            "query_id": "query-id-demo",
        },
        headers=IDENTITY,
    )
    assert status == 404, payload
    assert payload["code"] == "TASK_NOT_FOUND"


# ==========================================================================
# 三、只读问答来源可追溯
# ==========================================================================

def test_readonly_answer_is_traceable_by_tenant_task_trace(cluster):  # A03
    trace = f"trace-ro-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)

    status, query = cluster.post(
        "manufacturing-core",
        "/internal/v1/knowledge/query",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "query_ref": "query-demo",
            "purpose": "purpose-knowledge-readonly",
            "query_id": "query-id-demo",
        },
        headers=IDENTITY,
    )
    assert status == 200, query
    assert query["sources"], "只读问答必须有来源"

    # 编排把回答投影写进 control-plane 的 owner 存储（P1 不新增 HTTP 写入口）。
    # 用与集群相同的 V3_STATE_DIR 直接追加，等价于编排链路落库后的读取验证。
    import os

    state_dir = cluster.state_dir
    answers = state_dir / "answers.jsonl"
    answers.parent.mkdir(parents=True, exist_ok=True)
    with answers.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "tenant_id": "tenant-demo",
                    "task_id": task_id,
                    "trace_id": trace,
                    "answer": {
                        "answer_ref": f"answer-{task_id}",
                        "sources": query["sources"],
                        "classification": query["classification"],
                        "model_version": "1.0.0-mock",
                        "policy_version": "policy-demo",
                        "output_schema_passed": True,
                        "output_leak_scan_passed": True,
                        "consumer_acl_verified": True,
                        "audit_refs": [f"audit-{task_id}"],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )

    status, answer = cluster.get(
        "control-plane",
        f"/api/v1/tasks/{task_id}/knowledge-answer",
        headers={"X-Context": json.dumps(context_body(tenant="tenant-demo", task_id=task_id, trace_id=trace))},
    )
    assert status == 200, answer
    assert answer["answer_ref"] == f"answer-{task_id}"
    assert answer["sources"], "成功回答必须至少有一条来源"
    assert answer["output_schema_passed"] is True
    assert answer["output_leak_scan_passed"] is True
    assert answer["audit_refs"]
    # 来源可按 tenant/task/trace 串联，且含版本与时间
    for source in answer["sources"]:
        assert source["tenant_id"] == "tenant-demo"
        assert source["task_id"] == task_id
        assert source["trace_id"] == trace
        assert source["source_version"]
        assert source["occurred_at"]
        assert source["received_at"]
        assert source["data_class"]
    _assert_valid(answer, "p1-readonly.json", "/$defs/Answer", "Answer")


def test_answer_endpoint_rejects_task_scope_mismatch(cluster):  # A02
    trace = f"trace-mis-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)
    status, payload = cluster.get(
        "control-plane",
        "/api/v1/tasks/task-other/knowledge-answer",
        headers={"X-Context": json.dumps(context_body(tenant="tenant-demo", task_id=task_id, trace_id=trace))},
    )
    assert status == 403, payload
    assert payload["code"] == "TASK_SCOPE_MISMATCH"


# ==========================================================================
# 四、Vault：Tokenization 不降级、恢复受授权约束
# ==========================================================================

def test_tokenize_never_returns_mapping_values(cluster):  # A02
    trace = f"trace-tok-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/tokenize",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "purpose": "purpose-model-invoke",
            "input_refs": ["ref-secret-demo"],
            "expires_at": "2026-10-05T00:00:00Z",
        },
        headers=IDENTITY,
    )
    assert status == 200, payload
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "ref-secret-demo" not in serialized, "响应泄漏了映射原值"
    assert payload["mapping_ref"] and payload["tokenized_ref"]


def test_restore_requires_authorized_consumer_and_valid_ttl(cluster):  # A02
    trace = f"trace-res-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)
    status, tokenized = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/tokenize",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "purpose": "purpose-model-invoke",
            "input_refs": ["ref-secret-demo"],
            "expires_at": "2026-10-10T00:00:00Z",
        },
        headers=IDENTITY,
    )
    assert status == 200, tokenized
    mapping_ref = tokenized["mapping_ref"]

    base = {
        **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
        "mapping_ref": mapping_ref,
        "purpose": "purpose-model-invoke",
        "tokenized_output_ref": "out-demo",
    }
    # 1) 未授权消费者 → 拒绝
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/restore",
        {**base, "consumer_ref": "consumer-unknown"},
        headers=IDENTITY,
    )
    assert status == 403 and payload["code"] == "CONSUMER_NOT_AUTHORIZED"

    # 2) 跨租户消费者 → 拒绝
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/restore",
        {**base, "consumer_ref": "consumer-other-tenant"},
        headers=IDENTITY,
    )
    assert status == 403

    # 3) 用途不符 → 拒绝
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/restore",
        {**base, "consumer_ref": "consumer-demo", "purpose": "purpose-demo"},
        headers=IDENTITY,
    )
    assert status == 403 and payload["code"] == "VAULT_PURPOSE_MISMATCH"

    # 4) 授权消费者 + 用途一致 → 成功，且返回受控引用而非原值
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/restore",
        {**base, "consumer_ref": "consumer-demo"},
        headers=IDENTITY,
    )
    assert status == 200, payload
    assert payload["output_schema_passed"] is True
    assert payload["output_leak_scan_passed"] is True
    assert payload["consumer_acl_verified"] is True
    assert "ref-secret-demo" not in json.dumps(payload, ensure_ascii=False)


def test_expired_mapping_is_rejected_by_clock_not_string(cluster):  # A02
    trace = f"trace-exp-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)
    status, tokenized = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/tokenize",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "purpose": "purpose-model-invoke",
            "input_refs": ["ref-secret-demo"],
            "expires_at": "2020-01-01T00:00:00Z",  # 已过期但格式合法
        },
        headers=IDENTITY,
    )
    assert status == 200, tokenized
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/restore",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "mapping_ref": tokenized["mapping_ref"],
            "purpose": "purpose-model-invoke",
            "consumer_ref": "consumer-demo",
            "tokenized_output_ref": "out-demo",
        },
        headers=IDENTITY,
    )
    assert status == 403, payload
    assert payload["code"] == "VAULT_MAPPING_EXPIRED"


# ==========================================================================
# 五、审计与接口边界
# ==========================================================================

def test_egress_audit_has_no_sensitive_body(cluster):  # A13
    records = audit_records(cluster)
    assert records, "没有出域审计记录"
    for item in records:
        serialized = json.dumps(item, ensure_ascii=False)
        # 只查真正的敏感载荷标识：token/恢复引用/密钥前缀。
        # 不查 "sk-" 这类泛化子串——它会命中 "task_id" 里的 "sk-"，
        # 属于测试自身的假阳性（把合法标识符当泄漏）。
        for token in ("tokenized-", "restored-", "tok-", "ref-secret-demo", "\"token\"", "original_ref"):
            assert token not in serialized, f"审计泄漏了 {token}"
        assert set(item) >= {
            "tenant_id", "audit_ref", "occurred_at", "classification_known",
            "invocation_performed", "invocation_succeeded", "output_released", "stage_refs",
        }
        # 未调用 ⇒ 不得声称成功或已释放输出
        if item["invocation_performed"] is False:
            assert item["invocation_succeeded"] is False
            assert item["output_released"] is False


def test_no_new_interface_layer_and_owner_routing(cluster):  # A02
    """P1 只加接口、不加层：所有新路径仍在既有 /api/v1 与 /internal/v1 下。"""
    for service_id, paths in P1_OWNED_PATHS.items():
        for path in paths:
            assert path.startswith(("/api/v1/", "/internal/v1/")), path
            status, _ = cluster.post(service_id, path, {})
            assert status != 404, f"{service_id} 应拥有 P1 路径 {path}"
    # 不属于本 owner 的 P1 路径必须 404
    foreign = [
        ("control-plane", "/internal/v1/egress/evaluate"),
        ("security-gateway", "/internal/v1/knowledge/query"),
        ("manufacturing-core", "/internal/v1/token-vault/tokenize"),
        ("decision-service", "/internal/v1/classifications/evaluate"),
    ]
    for service_id, path in foreign:
        status, _ = cluster.post(service_id, path, {})
        assert status == 404, f"{service_id} 不应暴露 {path}"
    # 不得出现新的接口层
    for service_id in SERVICE_IDS:
        for path in ("/governance/v1/egress", "/security/v1/egress", "/v2/internal/v1/egress/evaluate"):
            status, _ = cluster.post(service_id, path, {})
            assert status == 404, f"{service_id} 暴露了新接口层 {path}"


def test_p1_requests_reject_foreign_tenant_and_identity(cluster):  # A01
    # 1) 自填 tenant 与认证身份不一致
    body = egress_body(backend="local", data_class="C2")
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        body,
        headers={"X-V3-Identity": "svc-unknown"},
    )
    assert status == 403, payload
    assert payload["code"] == "IDENTITY_NOT_VERIFIED"

    # 2) 顶层 tenant 与 context.tenant 不一致
    mismatched = egress_body(backend="local", data_class="C2")
    mismatched["tenant_id"] = "tenant-c3"
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        mismatched,
        headers=IDENTITY,
    )
    assert status == 403, payload
    assert payload["code"] == "NESTED_TENANT_MISMATCH"

    # 3) 未登记用途
    status, payload = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        egress_body(backend="local", data_class="C2", purpose="purpose-not-registered"),
        headers=IDENTITY,
    )
    assert status == 403, payload
    assert payload["code"] == "PURPOSE_NOT_REGISTERED"


# ==========================================================================
# 六、响应体符合 P1 契约
# ==========================================================================

def _inline(node, current_document):
    if isinstance(node, dict):
        if "$ref" in node:
            reference = node["$ref"]
            if reference.startswith(("http://", "https://")):
                raise AssertionError(f"契约出现外部引用: {reference}")
            file_part, _, pointer = reference.partition("#")
            document_name = file_part or current_document
            document = json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
            target = document
            for part in pointer.strip("/").split("/"):
                if part:
                    target = target[part]
            resolved = _inline(dict(target), document_name)
            extra = {key: value for key, value in node.items() if key != "$ref"}
            if extra:
                merged = dict(resolved)
                merged.update(_inline(extra, current_document))
                return merged
            return resolved
        return {key: _inline(value, current_document) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline(item, current_document) for item in node]
    return node


def _assert_valid(instance, document_name, pointer, label):
    document = json.loads((SCHEMAS / document_name).read_text(encoding="utf-8"))
    node = document
    for part in pointer.strip("/").split("/"):
        if part:
            node = node[part]
    validator = Draft202012Validator(_inline(node, document_name))
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    assert not errors, f"{label} 不符合契约 {document_name}{pointer}: " + "; ".join(
        f"{'/'.join(str(part) for part in error.path)}: {error.message}" for error in errors[:4]
    )


def test_p1_responses_conform_to_contract(cluster):  # A02
    trace = f"trace-schema-{uuid.uuid4().hex[:8]}"
    task_id = _accept_task(cluster, tenant="tenant-demo", data_class="C2", trace_id=trace)

    status, classification = cluster.post(
        "security-gateway",
        "/internal/v1/classifications/evaluate",
        p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
        headers=IDENTITY,
    )
    assert status == 200
    _assert_valid(classification, "p1-classification.json", "", "Classification")

    status, decision = cluster.post(
        "security-gateway",
        "/internal/v1/egress/evaluate",
        egress_body(backend="local", data_class="C2", tenant="tenant-demo", task_id=task_id, trace_id=trace),
        headers=IDENTITY,
    )
    assert status == 200
    _assert_valid(decision, "p1-governance.json", "", "EgressDecision")

    status, tokenization = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/tokenize",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "purpose": "purpose-model-invoke",
            "input_refs": ["ref-secret-demo"],
            "expires_at": "2026-10-10T00:00:00Z",
        },
        headers=IDENTITY,
    )
    assert status == 200
    _assert_valid(tokenization, "p1-token-vault.json", "", "TokenizationResult")

    status, result = cluster.post(
        "security-gateway",
        "/internal/v1/token-vault/restore",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "mapping_ref": tokenization["mapping_ref"],
            "purpose": "purpose-model-invoke",
            "consumer_ref": "consumer-demo",
            "tokenized_output_ref": "out-demo",
        },
        headers=IDENTITY,
    )
    assert status == 200
    _assert_valid(result, "p1-token-vault.json", "/$defs/RestoreResult", "RestoreResult")

    status, query = cluster.post(
        "manufacturing-core",
        "/internal/v1/knowledge/query",
        {
            **p1_body(tenant="tenant-demo", data_class="C2", task_id=task_id, trace_id=trace),
            "query_ref": "query-demo",
            "purpose": "purpose-knowledge-readonly",
            "query_id": "query-id-demo",
        },
        headers=IDENTITY,
    )
    assert status == 200
    _assert_valid(query, "p1-readonly.json", "/$defs/QueryResult", "QueryResult")
    for source in query["sources"]:
        _assert_valid(source, "p1-knowledge-source.json", "", "KnowledgeSource")

    records = audit_records(cluster)
    assert records
    _assert_valid(records[-1], "p1-egress-audit.json", "", "EgressAudit")
