"""A01–A14 验收断言的**可执行覆盖清单**（上游 §17）。

为什么要它：上游明说「只在文档或单元 Mock 中通过不等于完成生产验收」，
而"我们做过隔离测试"这类口头主张无法验证。这里把每条断言钉到**具体测试节点**上，
并由 `tests/assertions/test_assertion_coverage.py` 在门禁里校验：

- 清单引用的测试**必须真实存在**（改名/删测试 → 门禁报红）；
- 关键套件里的每个用例**必须声明**它支撑哪条断言（新用例不许含糊）。

`refs` 只写"支撑该断言的测试"，一个测试可支撑多条断言（用多行引用）。

**环境标注**（上游要求送审包标明环境）：`mock` = 本地真实进程 + Mock 外部依赖；
`sandbox` / `production` 目前均为 **未覆盖**，必须显式写出而不是省略。
"""

from __future__ import annotations

ASSERTIONS: dict[str, dict[str, object]] = {
    "A01": {
        "claim": "跨租户 task、Memory、RAG、object 和 command 读取均被拒绝",        "status": "covered",
        "refs": [
            "tests/p0_runtime/test_p0_runtime.py::test_unknown_tenant_and_tool_are_rejected",
            "tests/p1_runtime/test_p1_runtime.py::test_cross_tenant_candidates_are_filtered_before_retrieval",
            "tests/p1_runtime/test_p1_runtime.py::test_role_scoped_document_is_not_returned",
            "tests/p1_runtime/test_p1_runtime.py::test_p1_requests_reject_foreign_tenant_and_identity",
            "tests/p15_runtime/test_p15_runtime.py::test_cross_owner_read_is_tenant_scoped",
            "tests/p2_migrations/test_migration_runner.py::test_execution_order_follows_version_not_file_name",
            "tests/p2_migrations/test_migration_runner.py::test_second_run_is_noop",
            "tests/p2_migrations/test_migration_pg_integration.py::test_real_postgres_enforces_row_level_security_fail_closed",
            "tests/p2_migrations/test_migration_pg_integration.py::test_real_postgres_rejects_cross_owner_read",
        ],
        "environment": "mock",
        "gap": (
            "应用层隔离在 Mock 下已验证；**存储层隔离缺执行层证据**——"
            "`test_migration_pg_integration.py` 的 7 条真库用例在未提供 "
            "`V3_TEST_DATABASE_URL` 时**明确 skip**（不是通过）："
            "真实 DB owner/角色/RLS 未在本轮执行，对象存储 ACL 也未接入"
        ),
    },
    "A02": {
        "claim": "C3/C4、未知等级及混合上下文不能发送公网；本地模型故障仍不出域",
        "status": "covered",
        "refs": [
            "tests/p1_runtime/test_p1_runtime.py::test_sensitive_classes_never_permitted_offsite",
            "tests/p1_runtime/test_p1_runtime.py::test_mixed_context_takes_highest_not_first_or_average",
            "tests/p1_runtime/test_p1_runtime.py::test_local_backend_failure_never_falls_back_to_public",
            "tests/p1_runtime/test_p1_runtime.py::test_unknown_data_class_is_rejected_not_downgraded",
        ],
        "environment": "mock",
        "gap": "未经过真实出网代理/网络 ACL 验证",
    },
    "A03": {
        "claim": "恶意附件/检索内容不能新增工具、提升权限或触发任意 URL/SQL",
        "status": "covered",
        "refs": [
            "tests/p0_runtime/test_p0_runtime.py::test_unknown_tenant_and_tool_are_rejected",
            "tests/p1_runtime/test_p1_runtime.py::test_readonly_answer_is_traceable_by_tenant_task_trace",
            "tests/p3_runtime/test_p3_runtime.py::test_normalize_rejects_unknown_tool_and_version_mismatch",
            "tests/p1_runtime/test_p1_governance_attacks.py::test_injection_payload_in_input_ref_cannot_create_tools_or_escalate",
            "tests/p1_runtime/test_p1_governance_attacks.py::test_injection_payload_in_normalize_is_not_an_authority",
            "tests/p1_runtime/test_p1_governance_attacks.py::test_injection_cannot_reach_registered_write_tool_without_approval",
            "tests/p1_runtime/test_p1_governance_attacks.py::test_injection_cannot_escalate_data_class_or_tenant",
        ],
        "environment": "mock",
        "gap": (
            "注入串覆盖 6 类越权意图（新增工具/提权/任意 SQL/任意外联/绕过审批/原型污染），"
            "且验证注入后工具**仍不存在**；**未接真实附件解析与 RAG 正文**（属 P2 adapter）"
        ),
    },
    "A04": {
        "claim": "同幂等键同 payload 只产生一次业务效果，不同 payload 冲突",
        "status": "covered",
        "refs": [
            "tests/p0_runtime/test_p0_runtime.py::test_idempotency_same_key_same_payload_same_command",
            "tests/p3_runtime/test_p3_runtime.py::test_same_key_same_payload_returns_same_command",
            "tests/p3_runtime/test_p3_runtime.py::test_same_key_different_payload_is_conflict",
            "tests/p3_runtime/test_p3_runtime.py::test_concurrent_command_submit_same_key_creates_one_command",
        ],
        "environment": "mock",
        "gap": "外部系统回执计数未接真实系统",
    },
    "A05": {
        "claim": "超时后的命令进入 UNKNOWN 并查状态，不能换键重发",
        "status": "covered",
        "refs": [
            "tests/p3_runtime/test_p3_runtime.py::test_reconcile_confirmed_success_records_receipt_and_succeeds",
            "tests/p3_runtime/test_p3_runtime.py::test_reconcile_not_executed_retries_with_same_key",
            "tests/p3_runtime/test_p3_runtime.py::test_reconcile_unconfirmed_goes_to_manual_review",
            "tests/p3_runtime/test_p3_runtime.py::test_reconcile_never_exposes_new_key_resend_or_direct_compensation",
            "tests/p3_transactions/test_transaction_semantics.py::test_unknown_never_allows_new_key_or_direct_compensation",
        ],
        "environment": "mock",
        "gap": "\"外部已成功但回执丢失\"由入参模拟，未做真实故障注入",
    },
    "A06": {
        "claim": "旧审批、payload 变更、权限撤销、对象版本变动都阻止执行",
        "status": "covered",
        "refs": [
            "tests/p3_runtime/test_p3_runtime.py::test_review_denies_expired_approval_with_single_locatable_reason",
            "tests/p3_runtime/test_p3_runtime.py::test_review_denies_when_payload_changed_after_approval",
            "tests/p3_runtime/test_p3_runtime.py::test_approval_fact_is_append_only_and_replay_is_idempotent",
            "tests/p3_runtime/test_p3_runtime.py::test_concurrent_approvals_race_one_cas_winner",
            "tests/p3_transactions/test_transaction_semantics.py::test_kernel_and_review_view_agree_and_accept_both_approval_shapes",
            "tests/p3_transactions/test_transaction_semantics.py::test_permission_revoked_after_approval_blocks_execution",
            "tests/p3_transactions/test_transaction_semantics.py::test_object_version_conflict_blocks_execution",
            "tests/web_admin/test_status_contract.py::test_approval_suite_covers_the_critical_counterexamples",
        ],
        "environment": "mock",
        "gap": (
            "真实 SSO 撤销链路未接入（权限来自受信登记）。"
            "**后台侧新增**：批准页判定（23 条用例）——过期/已决定/审批人=发起人一律 BLOCKED；"
            "**自然语言「批准」不构成授权**（上游明文）；"
            "**缺角色/限额数据的三项检查记为 UNKNOWN 而非通过**，"
            "并有断言「当前数据条件下没有任何输入能得出可决定」"
        ),
    },
    "A07": {
        "claim": "重复/乱序事件及 worker 重启不覆盖新事实、不重放成功工具",
        "status": "covered",
        "refs": [
            "tests/p15_events/test_event_semantics.py::test_duplicate_event_is_applied_once",
            "tests/p15_events/test_event_semantics.py::test_late_event_never_overwrites_newer_fact",
            "tests/p15_events/test_event_semantics.py::test_version_gap_queues_for_backfill_and_fills_in_order",
            "tests/p15_runtime/test_p15_runtime.py::test_duplicate_delivery_is_deduplicated",
            "tests/p15_runtime/test_p15_runtime.py::test_late_event_does_not_overwrite_newer_fact",
            "tests/p3_runtime/test_p3_runtime.py::test_command_state_survives_transaction_service_restart",
        ],
        "environment": "mock",
        "gap": "无真实 broker；重启只覆盖 transaction-service 单点",
    },
    "A08": {
        "claim": "Saga 补偿按实物与账务阶段处理，补偿失败进入人工队列",
        "status": "covered",
        "refs": [
            "tests/p3_runtime/test_p3_runtime.py::test_saga_requires_physical_restore_before_accounting_compensation",
            "tests/p3_runtime/test_p3_runtime.py::test_shipped_or_posted_steps_go_to_branches_and_manual_queue",
            "tests/p3_runtime/test_p3_runtime.py::test_non_compensable_step_goes_to_manual_review",
            "tests/p3_transactions/test_transaction_semantics.py::test_compensation_order_is_physical_before_ledger",
            "tests/p3_transactions/test_transaction_semantics.py::test_compensation_failure_never_claims_compensated",
        ],
        "environment": "mock",
        "gap": "MES/WMS/ERP 状态断言与凭证来自 Mock；补偿命令未真正派发",
    },
    "A09": {
        "claim": "影子模式无法写控制口；高置信也不能绕过 Schema、OOD、互锁或过期许可",
        "status": "covered",
        "refs": [
            "tests/p4_edge/test_edge_semantics.py::test_shadow_never_enables_control_write_even_with_full_confidence",
            "tests/p4_edge/test_edge_semantics.py::test_confidence_cannot_override_any_earlier_stage",
            "tests/p4_edge/test_edge_semantics.py::test_hard_interlock_cannot_be_overridden_by_human_approval",
            "tests/p4_edge/test_edge_semantics.py::test_llm_reported_confidence_is_not_acceptable",
            "tests/p4_runtime/test_p4_runtime.py::test_shadow_inference_never_enables_execution",
            "tests/p4_runtime/test_p4_runtime.py::test_guard_evaluation_blocks_and_never_produces_action",
            "tests/p4_runtime/test_p4_runtime.py::test_guard_blocks_when_references_missing_before_confidence",
            "tests/p4_runtime/test_p4_runtime.py::test_release_advances_only_through_seven_states_in_order",
            "tests/p4_runtime/test_p4_runtime.py::test_behavioral_change_reopens_gate_and_invalidates_old_approval",
            "tests/p4_runtime/test_p4_runtime.py::test_p4_contract_paths_are_all_reachable_by_some_service",
            "tests/e2e/test_shadow_report_read.py::test_shadow_report_round_trip",
            "tests/e2e/test_shadow_report_read.py::test_shadow_report_missing_returns_404_not_empty_success",
        ],
        "environment": "mock",
        "gap": "**未做真实网络 ACL 验证**：控制口禁用目前是本地语义断言（写在响应里的恒定声明，非物理隔离实测）",
    },
    "A10": {
        "claim": "断网 spool 恢复只补报已执行事实，迟到/过期动作不执行",
        "status": "covered",
        "refs": [
            "tests/p4_edge/test_edge_semantics.py::test_executed_actions_are_reported_as_facts_never_replayed",
            "tests/p4_edge/test_edge_semantics.py::test_replay_of_executed_action_is_rejected",
            "tests/p4_edge/test_edge_semantics.py::test_pending_action_after_ttl_is_not_executed",
            "tests/p4_edge/test_edge_semantics.py::test_reconnect_dedups_by_event_id",
            "tests/p4_edge/test_edge_semantics.py::test_disconnected_autonomy_disabled_blocks_and_requires_baseline",
            "tests/p4_runtime/test_p4_runtime.py::test_spool_executed_fact_is_never_center_executable",
            "tests/p4_runtime/test_p4_runtime.py::test_spool_pending_action_requires_ttl",
            "tests/p4_runtime/test_p4_runtime.py::test_spool_entry_is_idempotent_by_event_id",
            "tests/p4_runtime/test_p4_runtime.py::test_autonomy_fails_closed_when_local_persistence_unavailable",
        ],
        "environment": "mock",
        "gap": "断连/重连/重启演练是语义层模拟，**未在真实边缘节点做**（无目标硬件）",
    },
    "A11": {
        "claim": "类型合法但语义错误的输出被领域约束拒绝",
        "status": "covered",
        "refs": [
            "tests/p4_edge/test_edge_semantics.py::test_each_guard_stage_blocks_with_locatable_reason",
            "tests/p3_transactions/test_transaction_semantics.py::test_risk_class_cannot_be_self_reported",
            "tests/p1_runtime/test_p1_governance_attacks.py::test_edge_guard_rejects_type_valid_but_semantically_wrong_output",
            "tests/p1_runtime/test_p1_governance_attacks.py::test_object_version_conflict_is_rejected_before_execution",
            "tests/p2_integrations/test_mapping.py::test_inconsistent_report_quantities_are_rejected",
            "tests/p2_integrations/test_mapping.py::test_missing_report_total_is_rejected",
            "tests/p2_integrations/test_mapping.py::test_negative_quantity_is_rejected",
            "tests/p2_integrations/test_mapping.py::test_factory_local_time_without_timezone_is_rejected",
            "tests/p2_integrations/test_mapping.py::test_unregistered_conversion_is_rejected",
            "tests/p2_integrations/test_mapping.py::test_boolean_is_not_an_integer",
            "tests/p2_integrations/test_mapping.py::test_identifier_rejects_free_text",
        ],
        "environment": "mock",
        "gap": (
            "覆盖越界/量纲错误/缺字段/NaN 与对象版本冲突；"
            "**接入层**新增：报工数量不自洽、缺总数、负数量、未登记单位换算、"
            "工厂本地时间无时区、布尔冒充整数、自由文本冒充标识——均拒绝而非取近似。"
            "**BOM 版本冲突的真实字段映射属待冻结**（外部 ERP/PMC 字段未提供）"
        ),
    },
    "A12": {
        "claim": "Edge 在目标硬件达到冻结端到端时限及误判门槛，14 天覆盖报告完整",
        "status": "not_covered",
        "refs": [],
        "environment": "not_covered",
        "gap": (
            "**需要目标硬件与真实产线数据**：无硬件 manifest、无分位延迟实测、"
            "无 14 天影子报告。属外部依赖，不得用 Mock 替代"
        ),
    },
    "A13": {
        "claim": "原始日志/指标无密钥和未授权敏感正文，备份恢复不跨域",
        "status": "partial",
        "refs": [
            "tests/p1_runtime/test_p1_runtime.py::test_egress_audit_has_no_sensitive_body",
            "tests/p1_runtime/test_p1_runtime.py::test_rejected_egress_attempts_are_audited",
            "tests/p1_runtime/test_p1_runtime.py::test_key_material_is_never_permitted_even_locally",
            "tests/p15_runtime/test_p15_runtime.py::test_dlq_entry_exposes_reason_attempts_and_handling_status",
            "tests/p2_observability/test_metrics.py::test_values_outside_the_closed_set_are_rejected",
            "tests/p2_observability/test_metrics.py::test_no_signal_uses_a_forbidden_label_key",
            "tests/p2_observability/test_metrics.py::test_every_emitted_label_passes_the_closed_set_check",
            "tests/p2_observability/test_metrics.py::test_prometheus_renderer_labels_no_data_explicitly",
        ],
        "environment": "mock",
        "gap": (
            "审计正文无密钥/无敏感正文已验证；**指标标签安全已加机械防线**"
            "（标签值取封闭集合，自由文本与疑似密钥在格式与取值上双重进不来）。"
            "**仍未做**：备份与恢复跨域演练（需真实环境与同等级数据域）"
        ),
    },
    "A14": {
        "claim": "任务成功只由权威执行回执驱动，accepted/未知不显示已过账",
        "status": "covered",
        "refs": [
            "tests/p15_runtime/test_p15_runtime.py::test_receipt_drives_business_success",
            "tests/p15_runtime/test_p15_runtime.py::test_duplicate_delivery_is_deduplicated",
            "tests/p3_runtime/test_p3_runtime.py::test_controlled_submit_persists_before_accept_and_is_visible_to_other_owner",
            "tests/p3_runtime/test_p3_runtime.py::test_receipt_after_restart_still_drives_success",
            "tests/p3_runtime/test_p3_runtime.py::test_controlled_dispatch_requires_transaction_identity",
            "tests/web_admin/test_status_contract.py::test_node_suite_passes",
            "tests/web_admin/test_status_contract.py::test_node_suite_covers_the_critical_counterexamples",
        ],
        "environment": "mock",
        "gap": (
            "**后台侧已接入**：`apps/web-admin` 的显示契约有 44 条用例（Node 内置测试，零依赖），"
            "关键反例包括「编排成功但无回执不得显示已过账」「回执未经权威验证不算」"
            "「UNKNOWN 单独显示需对账」「结论必带来源与更新时间」「列表页『已过账』计数不含仅编排成功」。"
            "**仍未做**：真实浏览器中的端到端验证（无 npm registry，渲染层未用框架构建）；"
            "后端 UI 契约（status_url 等）的真实联调"
        ),
    },
}

# 允许的状态取值：避免"悄悄漏掉"某条
ASSERTION_STATUSES = ("covered", "partial", "not_covered")

# 必须在门禁里被校验"每个用例都声明断言"的套件（新增用例不许含糊）
TAGGED_SUITES = (
    "tests/isolation",
    "tests/transactions",
    "tests/p0_runtime",
    "tests/p1_runtime",
    "tests/p15_events",
    "tests/p15_runtime",
    "tests/p2_integrations",
    "tests/p2_storage",
    "tests/p2_migrations",
    "tests/p2_observability",
    "tests/p2_runtime",
    "tests/p3_transactions",
    "tests/p3_runtime",
    "tests/p4_edge",
    "tests/p4_runtime",
    "tests/web_admin",
    "tests/e2e",
)

# 不要求打 `# Axx` 标签的套件 —— **必须逐条写明理由**。
#
# 为什么需要这个清单：`tests/assertions` 与 `tests/contracts` 守的是"断言清单本身"
# 与"契约元数据"，它们不是 V3 的行为断言，硬塞 A 编号只会污染证据链。
# 但"不需要打标签"不能被当成"忘了打"——所以显式登记，且理由要能被检验。
UNTAGGED_SUITES = (
    (
        "tests/decision",
        "守 **Jev 适配方式**（上游 §3「均需实现同一契约」+ §13「适配方式需 P0 冻结」）——"
        "是适配层的结构约束（封闭类型清单、两个实现跑同一套契约判据、换实现不改内核、"
        "解析 fail-closed），不描述 V3 的运行时行为，因此不属于 A01–A14",
    ),
    (
        "tests/assertions",
        "守断言清单自身（引用是否真实、标签是否一致、有无幽灵测试），不是 V3 行为断言",
    ),
    (
        "tests/contracts",
        "守契约元数据（OpenAPI/JSON Schema 的元校验与清单覆盖），同上",
    ),
    (
        "tests/p2_runtime/test_repo_hygiene.py",
        "工程卫生（测试产物不得污染仓库）——不是 V3 行为断言，属交付质量约束",
    ),
    (
        "tests/domain",
        "共享领域包的**结构约束**（纯函数性、词表唯一来源、与契约逐字对齐）——"
        "不描述 V3 的运行时行为，因此不属于 A01–A14",
    ),
    (
        "tests/infra",
        "基础设施**配置一致性**（部署清单 ↔ 代码、编排 ↔ 清单、ACL 分层、无硬编码凭据）——"
        "是交付配置的校验，不描述 V3 运行时行为",
    ),
    (
        "tests/e2e/test_route_reachability.py",
        "**全路由可达性**：每条注册路由都真的能被调到 + 探针覆盖全部注册路由。"
        "这是交付面的结构约束（防「注册了却打不到」与「零覆盖路由」），"
        "不描述任何一条 V3 业务断言，因此不属于 A01–A14。"
        "同目录 test_shadow_report_read.py 是业务用例，**已带** A01/A09 标签、不在此豁免内",
    ),
)

