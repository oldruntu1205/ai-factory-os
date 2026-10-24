"""阶段门禁可信度验证（变异测试）：注入实现缺陷，门禁必须报红。

为什么需要：门禁"跑绿"只说明没发现已知模式的问题，不说明它能发现新问题。
每次新增阶段都要重新做一遍变异——P1 阶段实测就有 2 个变异逃逸（用例覆盖缺口）。

用法：
    backend\\.venv\\Scripts\\python.exe scripts\\mutate.py --phase p1
    backend\\.venv\\Scripts\\python.exe scripts\\mutate.py --phase p15

每个变异：备份原文件 → 注入 → 跑目标测试 → 记录是否失败 → 还原。
任何变异"未被捕获"都会让本脚本 exit 1（说明门禁有盲区）。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"

# 阶段 → [(名称, 文件, 原文, 变异后, 目标测试)]
PHASES: dict[str, list[tuple[str, str, str, str, str]]] = {
    "p1": [
        (
            "放开 C3/C4 出域（矩阵改成永远 allow）",
            "services/_kernel/governance.py",
            '"C3": {"local": "allow", "enterprise_cloud": "deny", "public": "deny"},',
            '"C3": {"local": "allow", "enterprise_cloud": "allow", "public": "allow"},',
            "tests/p1_runtime/test_p1_runtime.py::test_sensitive_classes_never_permitted_offsite",
        ),
        (
            "去掉租户等级上限",
            "services/_kernel/context.py",
            "    if _class_rank(data_class) > _class_rank(max_class):\n        raise errors.reject_unknown_data_class(data_class)",
            "    if False:\n        raise errors.reject_unknown_data_class(data_class)",
            "tests/p1_runtime/test_p1_runtime.py",
        ),
        (
            "取消检索前租户过滤",
            "services/_kernel/knowledge.py",
            '        if document.get("tenant_id") != context.tenant_id:\n            # 跨租户候选在召回阶段就被排除，绝不进入后续链路。\n            continue',
            "        if False:\n            continue",
            "tests/p1_runtime/test_p1_runtime.py::test_cross_tenant_candidates_are_filtered_before_retrieval",
        ),
        (
            "Vault 不校验有效期",
            "services/_kernel/vault.py",
            "    if expires_epoch is None or expires_epoch <= _now_epoch():",
            "    if expires_epoch is None:",
            "tests/p1_runtime/test_p1_runtime.py::test_expired_mapping_is_rejected_by_clock_not_string",
        ),
        (
            "审计不再记录被拒绝的出域尝试",
            "services/_kernel/routes.py",
            "    except errors.V3Error:\n        egress_audit.record(",
            "    except errors.V3Error:\n        _ = (lambda **kwargs: None)(\n            ",
            "tests/p1_runtime/test_p1_runtime.py",
        ),
        (
            "请求自填 tenant 不再与可信身份核对",
            "services/_kernel/context.py",
            "    if identity_tenant is not None and tenant_id != identity_tenant:",
            "    if False:",
            # 该校验的用例在 P1.5 套件里（P1.5 才引入身份 tenant 头），
            # 目标必须是真正覆盖它的那个用例，否则变异会假逃逸。
            "tests/p15_runtime/test_p15_runtime.py::test_self_declared_tenant_must_match_trusted_identity",
        ),
    ],
    "p15": [
        (
            "inbox 不再按 event_id 去重",
            "services/_kernel/events.py",
            "        if self.has_seen(event_id):\n            return ConsumeResult(ConsumeOutcome.DUPLICATE, detail=\"event_id already applied\")",
            "        if False:\n            return ConsumeResult(ConsumeOutcome.DUPLICATE, detail=\"disabled\")",
            "tests/p15_runtime/test_p15_runtime.py::test_duplicate_delivery_is_deduplicated",
        ),
        (
            "迟到事件可以覆盖新事实",
            "services/_kernel/events.py",
            "        if version <= current_version:\n            journal.append(",
            "        if False:\n            journal.append(",
            "tests/p15_runtime/test_p15_runtime.py::test_late_event_does_not_overwrite_newer_fact",
        ),
        (
            "缺版本直接应用（不再进补拉队列）",
            "services/_kernel/events.py",
            "        if version > current_version + 1:\n            if self.is_queued(event_id):",
            "        if False:\n            if self.is_queued(event_id):",
            "tests/p15_runtime/test_p15_runtime.py::test_version_gap_queues_then_drains",
        ),
        (
            "回执不再核对命令 payload 哈希",
            "services/_kernel/routes.py",
            '    if record["payload_hash"] != body["command_payload_hash"]:',
            "    if False:",
            "tests/p15_runtime/test_p15_runtime.py::test_receipt_payload_hash_must_match_command",
        ),
        (
            "UNKNOWN 命令可直接被回执改判成功",
            "services/_kernel/routes.py",
            '    if record["status"] == "UNKNOWN":',
            "    if False:",
            "tests/p15_runtime/test_p15_runtime.py::test_unknown_command_cannot_be_receipted_without_query",
        ),
        (
            "跨 owner 权威读不再校验租户归属",
            "services/_kernel/routes.py",
            "    record = find_task(context.tenant_id, task_id)\n    if record is None:\n        raise errors.V3Error(errors.HTTP_NOT_FOUND, \"TASK_NOT_FOUND\"",
            "    record = find_task(context.tenant_id, task_id)\n    if record is None and False:\n        raise errors.V3Error(errors.HTTP_NOT_FOUND, \"TASK_NOT_FOUND\"",
            "tests/p15_runtime/test_p15_runtime.py",
        ),
        (
            "outbox 原子性被破坏（先落状态再落事件）",
            "services/_kernel/events.py",
            "        os.replace(tmp, target)  # 原子切换点",
            "        journal.append(self.channel, payload[\"state\"] or {})\n        os.replace(tmp, target)",
            # 目标必须是**真正覆盖原子性**的用例：早期误选了 nothing_staged，
            # 那个用例在注入缺陷后仍然通过，于是变异被误报为"逃逸"。
            "tests/p15_events/test_event_semantics.py::test_state_and_event_commit_together",
        ),
    ],
    "p2": [
        (
            "去掉 FORCE RLS（表所有者可绕过隔离）",
            "services/_kernel/storage.py",
            'ddl.statements.append(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY;")',
            "pass",
            "tests/p2_storage",
        ),
        (
            "把 tenant 移出隔离域",
            "services/_kernel/storage.py",
            '"tool_executions": ["tenant_id", "execution_ref"],',
            '"tool_executions": ["execution_ref"],',
            "tests/p2_storage",
        ),
        (
            "给应用角色加 CREATEDB",
            "services/_kernel/storage.py",
            '    app_attrs = "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"',
            '    app_attrs = "LOGIN NOINHERIT NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"',
            "tests/p2_storage",
        ),
        (
            "应用角色允许绕过 RLS（BYPASSRLS 开启）",
            "services/_kernel/storage.py",
            '    app_attrs = "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"',
            '    app_attrs = "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS"',
            "tests/p2_storage",
        ),
        (
            "允许跨 owner 授权",
            "services/_kernel/storage.py",
            'ddl.statements.append(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")',
            'ddl.statements.append(f"GRANT USAGE ON SCHEMA transaction TO {app_role_name(owner)};")\n    ddl.statements.append(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")',
            "tests/p2_storage",
        ),
        (
            "迁移生成器漏掉一个 owner（清单不完整）",
            "services/_kernel/migration_ledger.py",
            "    for owner in sorted(storage.OWNER_SCHEMAS):\n        # 磁盘上已存在的版本（新增版本后这里自动包含历史版本）",
            "    for owner in sorted(storage.OWNER_SCHEMAS)[:-1]:\n        # 磁盘上已存在的版本（新增版本后这里自动包含历史版本）",
            "tests/p2_migrations",
        ),
        (
            "迁移内容哈希不再冻结（改了也不报）",
            "services/_kernel/migration_ledger.py",
            '                digest = sha256_text(disk_path.read_text(encoding="utf-8"))',
            '                digest = "0" * 64',
            "tests/p2_migrations",
        ),
        (
            "owner 目录边界不再校验",
            "services/_kernel/migration_ledger.py",
            "        if directory.name not in known_owners:",
            "        if False:",
            "tests/p2_migrations",
        ),
        (
            "已应用迁移被改内容后不再拒绝执行",
            "services/_kernel/migration_ledger.py",
            "        if current != applied[version]:",
            "        if False:",
            "tests/p2_migrations",
        ),
        (
            "已应用迁移文件被删后不再报错",
            "services/_kernel/migration_ledger.py",
            "    if vanished:",
            "    if False:",
            "tests/p2_migrations",
        ),
        (
            "迁移版本不再按版本号排序（按文件名字典序）",
            "services/_kernel/migration_ledger.py",
            None,
            "结构上不可捕获：当前迁移文件名是零填充 4 位数字（0001/0002/…），"
            "字典序与数值序**结果完全相同**，无法通过观测区分。"
            "若将来出现非零填充命名，此变异即变得可捕获——届时请把它恢复为真实变异。",
            "tests/p2_migrations",
        ),
        (
            "清单只冻结当前版本（不再覆盖全部已发布版本）",
            "services/_kernel/migration_ledger.py",
            None,
            "结构上不可捕获：清单的版本集合是**从磁盘发现**的，"
            "若只登记当前版本，发现的集合本身也会同步缩小，两侧一致。"
            "该风险的真正防线是 `manifest_version_problems` 的**独立判据**"
            "（已由 test_manifest_validation_rejects_scrambled_version_order 覆盖）。",
            "tests/p2_migrations",
        ),
        (
            "迁移失败后继续应用后续版本（不停止）",
            "services/_kernel/migration_runner.py",
            "            result.failed_version = item.version\n            result.error = f\"{item.relative_path}: {error}\"\n            # **失败即停**：不继续应用后续版本，避免半截状态\n            raise MigrationExecutionError(result.error) from error",
            "            result.failed_version = item.version\n            result.error = f\"{item.relative_path}: {error}\"\n            continue",
            "tests/p2_migrations",
        ),
        (
            "账本哈希不再记录（无法核对已应用迁移）",
            "services/_kernel/migration_runner.py",
            "        digest = ledger.sha256_text(sql)",
            '        digest = "0" * 64',
            "tests/p2_migrations",
        ),
        (
            "语句拆分把 DO $$ 块按分号误拆（真库会语法报错）",
            "services/_kernel/migration_runner.py",
            "            if line.startswith(\"$$\", index):\n                in_dollar_quote = not in_dollar_quote",
            "            if False:\n                in_dollar_quote = not in_dollar_quote",
            "tests/p2_migrations",
        ),
        (
            "账本建表不再先于迁移执行",
            "services/_kernel/migration_runner.py",
            "    if not applied and _ledger_needs_creation(adapter):\n        adapter.execute(ledger.render_ledger_ddl())",
            "    if False:\n        adapter.execute(ledger.render_ledger_ddl())",
            "tests/p2_migrations",
        ),
        (
            "指标标签不再要求封闭取值范围（自由文本可进标签）",
            "services/_kernel/metrics.py",
            None,
            "该变异已不可捕获，且**不是**盲区：它改的 `allowed = signal.allowed_values.get(label_key)` "
            "这一行是 `validate_label` 内部的中间变量，其效果已被 `_labelled_by_value` 完全覆盖——"
            "后者对每个观测值直接调 `validate_label`，取值越界即失败（由 "
            "`test_out_of_set_dimension_value_fails_loudly_instead_of_being_silently_dropped` 与 "
            "`test_values_outside_the_closed_set_are_rejected` 两条用例锁定）。"
            "保留它作为已接受登记的冗余变异，用来记录这条演进。",
            "tests/p2_observability",
        ),
        (
            "就绪积压重新硬编码为 0（永远报告无积压）",
            "services/_kernel/metrics.py",
            "    return pending_tasks + pending_commands + pending_approvals + inbox_backlog",
            "    return pending_commands + pending_approvals + inbox_backlog",
            "tests/p2_observability",
        ),
        (
            "无样本的成功率返回 0 而不是「无数据」",
            "services/_kernel/metrics.py",
            "    if denominator <= 0:\n        return None",
            "    if denominator <= 0:\n        return 0.0",
            "tests/p2_observability",
        ),
        (
            "成本指标编造占位数值",
            "services/_kernel/metrics.py",
            '        "total": None,\n        "by_label": _labelled_by_value("model_cost_total", "backend", backends),',
            '        "total": 0.0,\n        "by_label": _labelled_by_value("model_cost_total", "backend", backends),',
            "tests/p2_observability",
        ),
        (
            "出口闭集校验被移除（自由文本可进标签）",
            "services/_kernel/metrics.py",
            "    if text not in allowed:",
            "    if False:",
            "tests/p2_observability",
        ),
        (
            "集合外维度值被静默过滤而不是失败",
            "services/_kernel/metrics.py",
            "    for value, count in observed.items():\n        result[value] = {",
            "    for value, count in observed.items():\n        if value not in (allowed := SIGNAL_BY_KEY[metric_key].allowed_values.get(label_key, ())):\n            continue\n        result[value] = {",
            "tests/p2_observability",
        ),
        (
            "缺请求字段重新被兜底成 503（调用方错误报成服务故障）",
            "services/_kernel/http.py",
            "                if isinstance(missing, str) and _is_request_field(missing, request):",
            "                if False:",
            "tests/p2_runtime",
        ),
        (
            "内部键的 KeyError 也被吞成 400（掩盖真缺陷）",
            "services/_kernel/http.py",
            "    if name in ctx_module.CONTEXT_KEYS:\n        return True\n    if name in request.path_params:\n        return True\n    return name in _declared_body_fields(request.method, request.path)",
            "    return True",
            "tests/p2_runtime",
        ),
        (
            "契约解析忽略片段（外链 $ref 解析不到声明字段）",
            "services/_kernel/http.py",
            "    if fragment:\n        return _walk_pointer(loaded, \"#\" + fragment)",
            "    if False:\n        return _walk_pointer(loaded, \"#\" + fragment)",
            "tests/p2_runtime",
        ),
        (
            "契约解析不按路径模板归一化（带真实 id 的路径匹配不到契约）",
            "services/_kernel/http.py",
            "    literals = _literal_positions()\n    wanted = _normalize(path, literals)",
            "    literals = _literal_positions()\n    wanted = _normalize(path, literals) if False else tuple(path.split('/'))",
            "tests/p2_runtime",
        ),
        (
            "仓库卫生守卫不再拦截相对路径写库",
            "tests/p2_runtime/test_repo_hygiene.py",
            '        if re.search(r"""[fFrRbBuU]{0,3}["\']\\.\\.?[/\\\\]""", value):',
            "        if False:",
            "tests/p2_runtime",
        ),
        (
            "工厂本地时间无时区时假定为 UTC（事件整体平移且不报错）",
            "integrations/_base.py",
            "    if not factory_timezone:\n        raise MappingError(",
            "    if False:\n        raise MappingError(",
            "tests/p2_integrations",
        ),
        (
            "缺必填字段改为补默认值（编造生产值）",
            "integrations/_base.py",
            "    missing = missing_external_paths(mapping, record)\n    if missing:",
            "    missing = []\n    if missing:",
            "tests/p2_integrations",
        ),
        (
            "未登记的单位换算静默按 1:1 处理",
            "integrations/_base.py",
            "    factor = UNIT_FACTORS.get((source_unit, target_unit))\n    if factor is None:",
            "    factor = UNIT_FACTORS.get((source_unit, target_unit), 1.0)\n    if False:",
            "tests/p2_integrations",
        ),
        (
            "华氏温度按倍率换算（丢偏移，产生错误温度）",
            "integrations/_base.py",
            "        return round((numeric - 32.0) * 5.0 / 9.0, 3)",
            "        return round(numeric, 3)",
            "tests/p2_integrations",
        ),
        (
            "报工数量不自洽时不再拒绝",
            "integrations/mes/adapter.py",
            "    if good + scrap != to_int(total):",
            "    if False:",
            "tests/p2_integrations",
        ),
        (
            "payload_hash 在单位换算之前定稿（哈希与内容不符）",
            "integrations/wms/adapter.py",
            "    from integrations._base import seal_payload\n\n    return seal_payload(result)",
            "    return result",
            "tests/p2_integrations",
        ),
        (
            "漂移探针不再拦截未登记原因码",
            "packages/domain/guard.py",
            '    return sorted({str(code).split(":", 1)[0].strip().upper() for code in codes} - set(GUARD_REASON_TO_STAGE))',
            "    return []",
            "tests/domain",
        ),
        (
            "阶段与契约偏离（删掉一个真实阶段的原因码归属）",
            "packages/domain/guard.py",
            '    "BUDGET_EXCEEDED": "budget",',
            "",
            "tests/domain",
        ),
        (
            "置信度不再排在最后（可覆盖硬判定）",
            "packages/domain/guard.py",
            '    "confidence",                  # 置信度（**最后**：不能覆盖以上任何一项）',
            '    "confidence",                  # 置信度\n    "interlock",',
            "tests/domain",
        ),
        (
            "发布状态机允许跳态",
            "packages/domain/release.py",
            "    return EDGE_RELEASE_ORDER[target] == EDGE_RELEASE_ORDER[current] + 1",
            "    return EDGE_RELEASE_ORDER[target] >= EDGE_RELEASE_ORDER[current] + 1",
            "tests/domain",
        ),
        (
            "行为性变化不再使旧批准失效",
            "packages/domain/behavior_change.py",
            '        "invalidates_approval": True,',
            '        "invalidates_approval": False,',
            "tests/domain",
        ),
        (
            "部署清单端口与代码漂移",
            "infra/deployment/manifest.yml",
            "    port: 8107",
            "    port: 9999",
            "tests/infra",
        ),
        (
            "部署清单 owner 与路由归属漂移",
            "infra/deployment/manifest.yml",
            "    owner: transaction",
            "    owner: security",
            "tests/infra",
        ),
        (
            "凭据允许非空默认值（忘记注入即用弱默认）",
            "infra/compose/docker-compose.dev.yml",
            "V3_PORT_SECURITY_GATEWAY: ${V3_PORT_SECURITY_GATEWAY:-8101}",
            "V3_PORT_SECURITY_GATEWAY: ${V3_PORT_SECURITY_GATEWAY:-8101}\n      SECRET_KEY: ${SECRET_KEY:-devsecret}",
            "tests/infra",
        ),
        (
            "服务间网络允许出网（绕过出域治理）",
            "infra/compose/docker-compose.dev.yml",
            "  internal:\n    driver: bridge\n    internal: true",
            "  internal:\n    driver: bridge",
            "tests/infra",
        ),
        (
            "内部接口被划进对外域",
            "infra/network/network-acl.yml",
            "  - path_prefix: /internal/v1\n    domain: internal",
            "  - path_prefix: /internal/v1\n    domain: frontdoor",
            "tests/infra",
        ),
        (
            "出域不再只允许安全网关",
            "infra/network/network-acl.yml",
            "  allowed_sources:\n    - security-gateway",
            "  allowed_sources:\n    - security-gateway\n    - control-plane",
            "tests/infra",
        ),
        (
            "后台允许无回执就显示已过账（UI 说成功而 ERP 未过账）",
            "apps/web-admin/src/status.js",
            "  if (!receipt || typeof receipt !== \"object\") {\n    return {\n      posted: false,\n      notApplicable: false,\n      reason: \"尚未取得执行回执\",",
            "  if (!receipt || typeof receipt !== \"object\") {\n    return {\n      posted: true,\n      notApplicable: false,\n      reason: \"尚未取得执行回执\",",
            "tests/web_admin",
        ),
        (
            "后台接受未经权威验证的回执（执行方自报即算）",
            "apps/web-admin/src/status.js",
            "  if (receipt.authority_verified !== true) {",
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "后台把未过账的回执也显示成已过账",
            "apps/web-admin/src/status.js",
            '  if (receipt.posting_status === \"POSTED\") {',
            '  if (receipt.posting_status !== \"\") {',
            "tests/web_admin",
        ),
        (
            "后台不再区分「不涉及过账」与「未过账」",
            "apps/web-admin/src/status.js",
            '  if (receipt.posting_status === \"NOT_APPLICABLE\") {',
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "后台把 UNKNOWN 含糊成处理中",
            "apps/web-admin/src/status.js",
            '  if (commandStatus === \"UNKNOWN\") {',
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "后台结论不再带状态来源与更新时间",
            "apps/web-admin/src/status.js",
            "  const sources = [\n    {\n      name: \"task\",",
            "  const sources = [];\n  const _unusedSources = [\n    {\n      name: \"task\",",
            "tests/web_admin",
        ),
        (
            "列表页自己按编排状态统计已过账（与逐行口径分叉）",
            "apps/web-admin/src/view.js",
            "  const summary = summarize(records);",
            "  const summary = { total: records.length, byOutcome: { POSTED: records.filter((r) => (r.taskStatus ?? r.status) === \"SUCCEEDED\").length } };",
            "tests/web_admin",
        ),
        (
            "列表页渲染不转义（注入风险）",
            "apps/web-admin/src/view.js",
            "  return String(value ?? \"\")\n    .replaceAll(\"&\", \"&amp;\")",
            "  return String(value ?? \"\")\n    .replaceAll(\"\\u0000\", \"\")",
            "tests/web_admin",
        ),
        (
            "后台接受自然语言「批准」作为授权",
            "apps/web-admin/src/approval.js",
            "  const natural = looksLikeNaturalLanguageApproval(comment);\n  if (natural.naturalLanguage) {",
            "  const natural = looksLikeNaturalLanguageApproval(comment);\n  if (false) {",
            "tests/web_admin",
        ),
        (
            "后台放宽决定类型（字符串 'true' 也算批准）",
            "apps/web-admin/src/approval.js",
            "  if (typeof approved !== \"boolean\") {",
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "缺角色/限额数据时不再记为 UNKNOWN（当成通过）",
            "apps/web-admin/src/approval.js",
            "    \"config_authority_separated\",\n    \"UNKNOWN\",",
            "    \"config_authority_separated\",\n    \"PASS\",",
            "tests/web_admin",
        ),
        (
            "缺数据时结论直接给 DECIDABLE",
            "apps/web-admin/src/approval.js",
            "  } else if (unknownChecks.length > 0) {",
            "  } else if (false) {",
            "tests/web_admin",
        ),
        (
            "已成立的批准仍显示为可决定",
            "apps/web-admin/src/approval.js",
            '        ? "该批准已成立；事实追加不可变，不能再改判"',
            '        ? "该批准已成立"',
            "tests/web_admin",
        ),
        (
            "明确阻塞不再压倒未知",
            "apps/web-admin/src/approval.js",
            "  if (blockReasons.length > 0) {",
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "未提供当前时刻时改用本机时钟判过期（结论不可复现）",
            "apps/web-admin/src/approval.js",
            "    push(\"validity\", \"UNKNOWN\", \"未提供当前时刻，无法判断是否过期（避免用本机时钟导致结论不可复现）\");",
            "    push(\"validity\", \"PASS\", \"未提供当前时刻，按本机时钟视为未过期\");",
            "tests/web_admin",
        ),
        (
            "现场路由不再挂到承载服务（注册了但打不到）",
            "services/_kernel/routes.py",
            '    if service_id == EDGE_OWNER_SERVICE:\n        routes.extend(ROUTES_BY_OWNER["edge"])',
            '    if False:\n        routes.extend(ROUTES_BY_OWNER["edge"])',
            "tests/e2e",
        ),
        (
            "影子报告读路径存在性判断失效（返回空对象当成功）",
            "services/_kernel/routes.py",
            '    if found is None:\n        raise errors.V3Error(errors.HTTP_NOT_FOUND, "SHADOW_REPORT_NOT_FOUND", "影子报告不存在或不可见")',
            "    if found is None:\n        found = {}",
            "tests/e2e",
        ),
        (
            "影子报告读路径忽略租户隔离",
            "services/_kernel/routes.py",
            '        "edge-shadow-reports", lambda item: (item["tenant_id"], item["report_id"])\n    ).get((context.tenant_id, report_id))',
            '        "edge-shadow-reports", lambda item: (item["tenant_id"], item["report_id"])\n    ).get(("tenant-demo", report_id))',
            "tests/e2e",
        ),
        (
            "ADR 状态写成自由文本（不再可机读）",
            "docs/adr/0005-authoritative-receipt-drives-posting.md",
            "- 状态：**已接受**",
            "- 状态：**大概吧**",
            "tests/infra",
        ),
        (
            "ADR 状态与索引分叉",
            "docs/adr/0001-journal-instead-of-database.md",
            "- 状态：**已接受**",
            "- 状态：**已接受（延后）**",
            "tests/infra",
        ),
        (
            "ADR 少了「备选方案与不选的理由」章节",
            "docs/adr/0003-three-domain-network-and-app-layer-egress.md",
            "## 备选方案与不选的理由",
            "## 备注",
            "tests/infra",
        ),
        (
            "新增 ADR 但未登记进索引",
            "docs/adr/README.md",
            "| [0006](0006-defer-edge-tree.md) | 延后 `edge/` 独立部署树 | 已接受（延后） | Edge 语义已在门禁里；部署形态随 P5 与现场许可一起定 |",
            "",
            "tests/infra",
        ),
        (
            "全链：过账状态被写死为 POSTED（界面会说已过账而实际未过账）",
            "services/_kernel/routes.py",
            '        "posting_status": (receipt or {}).get("posting_status") or "NOT_CONFIRMED",',
            '        "posting_status": "POSTED",',
            "tests/e2e",
        ),
        (
            "全链：权威回执不再驱动业务成功",
            "services/_kernel/routes.py",
            '        "business_success": bool(receipt and receipt.get("business_success") is True),',
            '        "business_success": False,',
            "tests/e2e",
        ),
        (
            "全链：已成立的批准可被改判",
            "services/_kernel/routes.py",
            '                errors.HTTP_CONFLICT, "APPROVAL_ALREADY_GRANTED",',
            '                errors.HTTP_CONFLICT, "APPROVAL_ALREADY_DENIED",',
            "tests/e2e",
        ),
        (
            "全链：命令权威视图的 status 被写死为 accepted",
            "services/_kernel/routes.py",
            '        "approval_ref": record.get("approval_ref"),\n        "status": record["status"],',
            '        "approval_ref": record.get("approval_ref"),\n        "status": "accepted",',
            "tests/e2e",
        ),
        (
            "缺口用例空转：断言不再真的检查 command_refs",
            "tests/e2e/test_chain_gap_task_command_link.py",
            '    assert fetched["command_refs"] == [], (',
            '    assert fetched["command_refs"] == ["not-a-real-command"], (',
            "tests/e2e",
        ),
        (
            "补偿重新伪造成功（未派发就声称成功且带权威回执）",
            "services/_kernel/routes.py",
            '        # **受理，不是成功**：与 `_p3_accepted` 同一口径。\n        "command_status": "AUTHORIZED",',
            '        # **受理，不是成功**：与 `_p3_accepted` 同一口径。\n        "command_status": "SUCCEEDED",',
            "tests/e2e",
        ),
        (
            "补偿判定不再要求回执（只看自报状态）",
            "services/_kernel/routes.py",
            '    return receipt.get("command_id") == compensation.get("command_id")',
            "    return True",
            "tests/p3_transactions",
        ),
        (
            "补偿不再校验回执与补偿命令是同一个（核不上账也算数）",
            "services/_kernel/routes.py",
            '    if receipt.get("authority_verified") is not True:\n        return False\n    return receipt.get("command_id") == compensation.get("command_id")',
            '    if receipt.get("authority_verified") is not True:\n        return False\n    return True',
            "tests/p3_transactions",
        ),
        (
            "Saga 不再投影补偿命令的实时状态（永远达不到已补偿）",
            "services/_kernel/routes.py",
            '        projected["command_status"] = live["status"]',
            "        projected[\"command_status\"] = compensation.get(\"command_status\")",
            "tests/p3_runtime",
        ),
        (
            "凭空写下权威成功断言（未被白名单登记的函数）",
            "services/_kernel/routes.py",
            'def _p3_accepted(record: dict[str, Any], context) -> dict[str, Any]:\n    """受理响应：**只表示已持久化受理**，绝不表示业务成功或已过账。"""',
            'def _p3_accepted(record: dict[str, Any], context) -> dict[str, Any]:\n    """受理响应：**只表示已持久化受理**，绝不表示业务成功或已过账。"""\n    _fabricated = {"authority_verified": True}',
            "tests/infra",
        ),
        (
            "控制台绕过「未配置决定人」检查（静默越权写批准）",
            "apps/web-admin/server.mjs",
            "  if (!APPROVER_ACTOR) {",
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "控制台跳过绑定完整性预检（残缺绑定也放行到后端）",
            "apps/web-admin/server.mjs",
            "  const missing = required.filter((key) => !binding[key]);",
            "  const missing = [];",
            "tests/web_admin",
        ),
        (
            "控制台决定时刻改用服务身份（不再记名）",
            "apps/web-admin/server.mjs",
            "    actor_id: APPROVER_ACTOR,",
            "    actor_id: SERVICE_IDENTITY,",
            "tests/web_admin",
        ),
        (
            "Guard 判据退回内联实现（共享判据不再被生产调用）",
            "services/_kernel/edge.py",
            "    top_stage = highest_priority_stage(reasons)",
            "    top_stage = failures[0][0] if failures else None",
            "tests/p4_edge",
        ),
        (
            "Guard 回退判据退回内联比对",
            "services/_kernel/edge.py",
            "    fallback_required = any(reason_requires_fallback(reason) for reason in reasons)",
            "    fallback_required = any(reason in GUARD_REASON_TO_STAGE for reason in reasons)",
            "tests/p4_edge",
        ),
        (
            "生产模块里新增一个从未被调用的公共判据（未登记）",
            "services/_kernel/edge.py",
            "def evaluate_guard(guard: GuardInput) -> dict[str, Any]:",
            'def unregistered_orphan_predicate(value: object) -> bool:\n    """一个从不被生产调用的判据（变异注入）。"""\n    return value is not None\n\n\ndef evaluate_guard(guard: GuardInput) -> dict[str, Any]:',
            "tests/infra",
        ),
        (
            "apps/ 导出探测器漏掉一种导出语法（守卫自己变瞎）",
            "tests/infra/test_apps_export_surface.py",
            '    for match in re.finditer(r"export\\s+(?:const|let|var)\\s+(\\w+)", text):\n        names.add(match.group(1))',
            '    for match in re.finditer(r"export\\s+const\\s+(\\w+)", text):\n        names.add(match.group(1))',
            "tests/infra",
        ),
        (
            "控制台页面丢掉关键元素（控制台白屏/不可用）",
            "apps/web-admin/public/index.html",
            '<div id="approvalResult"></div>',
            '<div id="approvalResultRenamed"></div>',
            "tests/infra",
        ),
        (
            "控制台页面内联脚本语法被破坏",
            "apps/web-admin/public/index.html",
            "async function loadHealth() {",
            "async function loadHealth( {",
            "tests/infra",
        ),
        (
            "断言清单改了但不重新生成覆盖报告（报告过期仍被当权威）",
            "tests/assertions/assertion_inventory.py",
            '"claim": "跨租户 task、Memory、RAG、object 和 command 读取均被拒绝"',
            '"claim": "跨租户读取被拒绝（已改措辞，但报告未重新生成）"',
            "tests/assertions",
        ),
        (
            "部署检查器把行尾注释当成取值（漏报未冻结）",
            "scripts/preflight_deploy.py",
            "    return value.split(\"#\", 1)[0].strip()",
            "    return value.strip()",
            "tests/infra",
        ),
        (
            "覆盖报告生成器变得不幂等（混入当天日期）",
            "scripts/render_assertion_report.py",
            '    lines: list[str] = [HEADER]',
            '    import datetime\n    lines: list[str] = [HEADER, datetime.datetime.now().strftime("%Y-%m-%d")]',
            "tests/infra",
        ),

        (
            "发布转换跳过本地合法性预检（跳态也发给后端）",
            "apps/web-admin/server.mjs",
            "  if (!legal.includes(target)) {",
            "  if (false) {",
            "tests/web_admin",
        ),
        (
            "发布状态机不再提供撤销选项（非终态无法停止使用）",
            "apps/web-admin/server.mjs",
            "  if (!next.includes(RELEASE_TERMINAL)) next.push(RELEASE_TERMINAL);",
            "  // 变异：不再提供撤销",
            "tests/web_admin",
        ),
        (
            "JS 侧发布判据漂移（给出重复的终态按钮）",
            "apps/web-admin/server.mjs",
            "  if (!next.includes(RELEASE_TERMINAL)) next.push(RELEASE_TERMINAL);",
            "  next.push(RELEASE_TERMINAL);",
            "tests/infra",
        ),
        (
            "Python 权威侧发布判据漂移（顺序改为非相邻）",
            "packages/domain/release.py",
            "    return EDGE_RELEASE_ORDER[target] == EDGE_RELEASE_ORDER[current] + 1",
            "    return EDGE_RELEASE_ORDER[target] >= EDGE_RELEASE_ORDER[current] + 1",
            "tests/infra",
        ),
        (
            "两份转义实现分叉（view.js 不再处理缺失值）",
            "apps/web-admin/src/view.js",
            '  return String(value ?? "")',
            "  return String(value)",
            "tests/infra",
        ),
        (
            "两份转义实现分叉（view.js 少转义单引号）",
            "apps/web-admin/src/view.js",
            '    .replaceAll("\'", "&#39;");',
            "    ;",
            "tests/infra",
        ),
        (
            "页面回退成真值判断（分支正确性依赖别处的类型保证）",
            "apps/web-admin/public/index.html",
            '${result.approved === true ? "通过" : "否决"}',
            '${result.approved ? "通过" : "否决"}',
            "tests/infra",
        ),
        (
            "页面新增一处未转义的 innerHTML 插值（注入面）",
            "apps/web-admin/public/index.html",
            '已记录：<strong>${result.approved === true ? "通过" : "否决"}</strong>',
            '已记录：<strong>${result.approved === true ? "通过" : "否决"}</strong>${result.code}',
            "tests/infra",
        ),
        (
            "对账页丢掉禁止动作清单（运维看不到防误操作约束）",
            "apps/web-admin/server.mjs",
            "    prohibited: Array.isArray(view.prohibited_actions) ? view.prohibited_actions : [],",
            "    prohibited: [],",
            "tests/web_admin",
        ),
        (
            "对账页把「没有收敛记录」当成错误（误报红）",
            "apps/web-admin/server.mjs",
            "  if (result.status === 404) {",
            "  if (false) {",
            "tests/web_admin",
        ),

        (
            "员工数据缺口登记失真（假装绑定已存在）",
            "services/_kernel/registry.mock.json",
            '"employees": ["employee-planning"],\n      "delegated_scopes": ["pmc.read", "pmc.dispatch.publish", "erp.inventory.reserve", "kb.read", "model.invoke"],\n      "max_data_class": "C2"',
            '"employees": [{"employee_id": "employee-planning"}],\n      "delegated_scopes": ["pmc.read", "pmc.dispatch.publish", "erp.inventory.reserve", "kb.read", "model.invoke"],\n      "max_data_class": "C2"',
            "tests/infra",
        ),
        (
            "两份契约哈希锁分叉（同一文件两把不同的锁）",
            "tests/contracts/test_p3_contracts.py",
            '"schemas/p3-employee.json": "a12faa7b4dacf9935bf4300ca52fae812259ac114027221f7fdfe7fb7430decc"',
            '"schemas/p3-employee.json": "b12faa7b4dacf9935bf4300ca52fae812259ac114027221f7fdfe7fb7430decc"',
            "tests/infra",
        ),
        (
            "登记处不再报告「存在但未登记」的契约文件",
            "tests/contracts/contract_inventory.py",
            "    return present() - all_registered()",
            "    return set()",
            "tests/contracts/test_contract_inventory_gates.py",
        ),
        (
            "登记处不再报告「已登记但磁盘缺失」的契约文件",
            "tests/contracts/contract_inventory.py",
            "    return all_registered() - present()",
            "    return set()",
            "tests/contracts/test_contract_inventory_gates.py",
        ),
        (
            "门禁覆盖面守卫失真（豁免表登记了不存在的文件）",
            "tests/infra/test_gate_coverage.py",
            '"backend/tests/test_marketplace.py": (',
            '"backend/tests/test_does_not_exist.py": (',
            "tests/infra",
        ),
        (
            "指标清单漏掉一个上游要求项",
            "services/_kernel/metrics.py",
            '        upstream_requirement="spool 容量",',
            '        upstream_requirement="容量",',
            "tests/p2_observability",
        ),
        (
            "测试脚本退回相对路径写库（重新污染仓库根）",
            "backend/tests/test_hermes_mcp_scheduler_audit.py",
            None,
            "**门禁结构上无法捕获**：legacy 的 `backend/tests/*.py` 是独立脚本，"
            "**不在 `scripts/gate.py` 的运行范围内**（门禁只跑 `tests/` 下的套件）。"
            "因此「它往仓库根写库」这个原始症状，在门禁里根本观察不到——"
            "这也是最初能攒下 45 个文件的原因。"
            "真实防线是 `backend/tests/_test_sqlite.py`（绝对临时路径 + atexit + 启动清扫）"
            "与 `test_repo_hygiene.py` 对**写法**的拦截；"
            "根治需要把 legacy 套件纳入门禁，属单独一步。",
            "tests/p2_runtime",
        ),
        (
            "隔离 helper 不再清理临时库（残留累积）",
            "backend/tests/_test_sqlite.py",
            None,
            "**门禁结构上无法捕获**：同上——门禁不运行该 helper 的使用方，"
            "所以「临时目录残留」不产生门禁可见的观测差异。"
            "该行为由 `_sweep_stale` 的启动清扫兜底（下次运行清掉过期残留），"
            "属**有界**而非无界泄漏。",
            "tests/p2_runtime",
        ),
    ],
    "p3": [
        (
            "命令视图对缺字段的记录硬取（P3 记录触发 500）",
            "services/_kernel/routes.py",
            '            "source_system": record.get("source_system"),\n            "receipt_refs": list(record.get("receipt_refs") or []),',
            '            "source_system": record["source_system"],\n            "receipt_refs": list(record["receipt_refs"]),',
            "tests/p3_runtime",
        ),
        (
            "租约过期判定改回读墙钟（传入的 now 失效）",
            "services/_kernel/transactions.py",
            "    return expiry > (moment if moment is not None else time.time())",
            "    return expiry > time.time()",
            "tests/p3_transactions",
        ),
        (
            "UNKNOWN 允许换新幂等键重发",
            "services/_kernel/transactions.py",
            '    "retry_with_new_idempotency_key",  # 换新键重发\n',
            "",
            "tests/p3_transactions",
        ),
        (
            "补偿顺序颠倒（账务先于实物）",
            "services/_kernel/transactions.py",
            "    ordered = list(reversed(physical)) + list(reversed(accounting))",
            "    ordered = list(reversed(accounting)) + list(reversed(physical))",
            "tests/p3_transactions",
        ),
        (
            "批准过期不再阻止执行",
            "services/_kernel/transactions.py",
            "        if expiry is None or (moment is not None and expiry <= moment):\n            refusal.add(\"APPROVAL_EXPIRED\")",
            "        if False:\n            refusal.add(\"APPROVAL_EXPIRED\")",
            "tests/p3_transactions",
        ),
        (
            "执行前不再复核对象版本（TOCTOU 放行）",
            "services/_kernel/transactions.py",
            "    if expected is not None and actual is not None and int(expected) != int(actual):\n        refusal.add(\"OBJECT_VERSION_CONFLICT\")",
            "    if False:\n        refusal.add(\"OBJECT_VERSION_CONFLICT\")",
            "tests/p3_transactions",
        ),
        (
            "补偿失败谎报已补偿",
            "services/_kernel/transactions.py",
            "    if failed:\n        return {\n            \"saga_status\": \"MANUAL_REVIEW\",",
            "    if False:\n        return {\n            \"saga_status\": \"MANUAL_REVIEW\",",
            "tests/p3_transactions",
        ),
        (
            "取消直接抹掉在途/已完成副作用",
            "services/_kernel/transactions.py",
            '        "task_status_may_be_set_cancelled": not (needs_confirm or needs_compensation),',
            '        "task_status_may_be_set_cancelled": True,',
            "tests/p3_transactions",
        ),
        (
            "收执不再复核规范化哈希（只信传来的值）",
            "services/_kernel/routes.py",
            '        if recomputed != record["payload_hash"]:',
            "        if False:",
            "tests/p3_runtime",
        ),
        (
            "规范化记录缺失时不再拒绝",
            "services/_kernel/routes.py",
            "        if normalized is None:",
            "        if False:",
            "tests/p3_runtime",
        ),
        (
            "只读工具可以进入写入规范化",
            "services/_kernel/routes.py",
            '    if entry["read_write"] != "write":',
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "能力位不足仍放行无人值守高风险写入",
            "services/_kernel/transactions.py",
            '    allowed = not missing',
            "    allowed = True",
            "tests/p3_transactions",
        ),
        (
            "同键异 payload 不再判冲突（幂等被绕过）",
            "services/_kernel/routes.py",
            # 必须带上 P3 处理器特有的上下文：P0 命令处理器有**形状相同**的判断，
            # 不带上下文会让 replace 命中 P0（实测把 P0 的幂等判断改坏过）。
            '            if existing["payload_hash"] != trusted_hash:\n                raise errors.V3Error(\n                    errors.HTTP_CONFLICT, errors.IDEMPOTENCY_CONFLICT,\n                    "同幂等键携带了不同 payload",\n                    details={"object_ref": "payload_hash"},\n                )',
            "            if False:\n                pass",
            "tests/p3_runtime",
        ),
        (
            "fencing token 不再校验（僵尸 worker 可提交）",
            "services/_kernel/routes.py",
            '        if lease is not None and int(lease["fencing_token"]) != body["fencing_token"]:',
            "        if False:",
            "tests/p3_runtime",
        ),
        (
            "执行不再复核批准/权限/版本（TOCTOU 端到端放行）",
            "services/_kernel/routes.py",
            "        if refusal.blocked:",
            "        if False:",
            "tests/p3_runtime",
        ),
        (
            "对象锁在有效期内不再独占（并发写同一对象）",
            "services/_kernel/routes.py",
            '        elif previous is not None and _lease_unexpired(previous["expires_at"]):',
            "        elif False:",
            "tests/p3_runtime",
        ),
        (
            "租约记录版本不再 CAS（并发取锁都能成功）",
            "services/_kernel/routes.py",
            # 带上下文区分：执行处理器里也有形状相同的 CAS 判断。
            # 两个处理器都校验 expected_record_version，只有紧跟的租约读取能区分开。
            '            raise errors.V3Error(\n                errors.HTTP_CONFLICT, "COMMAND_VERSION_CONFLICT", "命令记录版本冲突",\n                details={"expected_version": body["expected_record_version"], "actual_version": current_version},\n            )\n\n        previous = record.get("lease")',
            '            pass\n\n        previous = record.get("lease")',
            "tests/p3_runtime",
        ),
        (
            "取消标记不再阻止执行",
            "services/_kernel/routes.py",
            '        if record.get("cancelled_before_start"):',
            "        if False:",
            "tests/p3_runtime",
        ),
        (
            "账务补偿不再要求先恢复实物（顺序颠倒）",
            "services/_kernel/routes.py",
            '    if domain == transactions.DOMAIN_ACCOUNTING and not record.get("physical_verified_and_restored"):',
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "已发货/已过账不再走分支（直接普通冲销）",
            "services/_kernel/routes.py",
            "    if step.get(\"is_shipped\") or step.get(\"is_posted\"):",
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "不可补偿步骤谎报为已补偿",
            "services/_kernel/routes.py",
            "    if not step.get(\"compensation_supported\"):",
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "批准绑定不再逐维核对（payload 变化可放行）",
            "services/_kernel/transactions.py",
            "        if approval_binding_fingerprint(approval) != approval_binding_fingerprint(",
            "        if False and approval_binding_fingerprint(approval) != approval_binding_fingerprint(",
            "tests/p3_transactions",
        ),
        (
            "批准事实可被改判（append-only 被破坏）",
            "services/_kernel/routes.py",
            "            if body[\"approved\"] is True:\n                record = existing\n            else:",
            "            if True:\n                record = existing\n            else:",
            "tests/p3_runtime",
        ),
        (
            "取消后仍允许启动新步骤",
            "services/_kernel/routes.py",
            '        "prevents_new_steps": True,\n        "side_effects_reconciled": side_effects_reconciled,',
            '        "prevents_new_steps": False,\n        "side_effects_reconciled": side_effects_reconciled,',
            "tests/p3_runtime",
        ),
        (
            "有已开始副作用仍谎报任务已取消",
            "services/_kernel/routes.py",
            "    if started and request_record.get(\"manual_queue_ref\"):",
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "取消请求不再做任务版本 CAS",
            "services/_kernel/routes.py",
            '    if body["expected_task_version"] != int(task.get("record_version", 0)):',
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "受控派发不再限制 caller 身份",
            "services/_kernel/routes.py",
            "    if caller != CONTROLLED_EXECUTION_CALLER:",
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "派发入口不再复核执行凭证",
            "services/_kernel/routes.py",
            '    if not credential or credential.get("execution_ref") != body["execution_credential_ref"]:',
            "    if False:",
            "tests/p3_runtime",
        ),
        (
            "派发入口不再复核 fencing",
            "services/_kernel/routes.py",
            '    if int(lease.get("fencing_token", -1)) != body["fencing_token"]:',
            "    if False:",
            "tests/p3_runtime",
        ),
    ],
    "p4": [
        (
            "置信度排到最高优先级（可覆盖结构性问题）",
            "packages/domain/guard.py",
            '    "confidence",                  # 置信度（**最后**：不能覆盖以上任何一项）\n)',
            '    "confidence",                  # 置信度（**最后**：不能覆盖以上任何一项）\n)\nGUARD_STAGES = (GUARD_STAGES[-1],) + GUARD_STAGES[:-1]',
            "tests/p4_edge",
        ),
        (
            "硬互锁可被人工批准覆盖",
            "services/_kernel/edge.py",
            '    if not guard.interlock_passed:\n        failures.append(("interlock", "INTERLOCK_DENIED"))',
            '    if not guard.interlock_passed and not guard.approval_present:\n        failures.append(("interlock", "INTERLOCK_DENIED"))',
            "tests/p4_edge",
        ),
        (
            "容忍 LLM 自报置信度（不要求校准）",
            "services/_kernel/edge.py",
            "    if not guard.confidence_is_calibrated:\n        failures.append((\"confidence\", \"CONFIDENCE_NOT_CALIBRATED\"))",
            "    if False:\n        failures.append((\"confidence\", \"CONFIDENCE_NOT_CALIBRATED\"))",
            "tests/p4_edge",
        ),
        (
            "发布状态机允许跳态",
            "packages/domain/release.py",
            "    return EDGE_RELEASE_ORDER[target] == EDGE_RELEASE_ORDER[current] + 1",
            "    return EDGE_RELEASE_ORDER[target] >= EDGE_RELEASE_ORDER[current]",
            "tests/p4_edge",
        ),
        (
            "行为性变化不再使旧批准失效",
            "packages/domain/behavior_change.py",
            '        "invalidates_approval": True,\n',
            '        "invalidates_approval": False,\n        "changed_kinds": sorted(behavioral),',
            "tests/p4_edge",
        ),
        (
            "已执行动作被允许中心重放",
            "services/_kernel/edge.py",
            '        return {\n            "accepted": False,\n            "reason": "REPLAY_OF_EXECUTED_ACTION_FORBIDDEN",',
            '        return {\n            "accepted": True,\n            "reason": "REPLAY_OF_EXECUTED_ACTION_FORBIDDEN",',
            "tests/p4_edge",
        ),
        (
            "待执行动作过期 TTL 仍然执行",
            "services/_kernel/edge.py",
            "        if age is not None and age > entry.ttl_seconds:",
            "        if False:",
            "tests/p4_edge",
        ),
        (
            "重连不再按 event_id 去重",
            "services/_kernel/edge.py",
            "        if entry.event_id in seen:\n            duplicates.append({\"event_id\": entry.event_id, \"edge_id\": entry.edge_id})\n            continue",
            "        if False:\n            continue",
            "tests/p4_edge",
        ),
        (
            "断连时本地持久化不可用仍允许自治",
            "services/_kernel/edge.py",
            "        blockers.append(\"LOCAL_PERSISTENCE_UNAVAILABLE\")",
            "        pass",
            "tests/p4_edge",
        ),
        (
            "影子模式允许控制写入",
            "services/_kernel/edge.py",
            '        "control_write_enabled": False,\n        "physical_write_disabled": True,',
            '        "control_write_enabled": True,\n        "physical_write_disabled": True,',
            "tests/p4_edge",
        ),
        (
            "未登记回退动作时即兴采用模型建议",
            "services/_kernel/edge.py",
            "    if registered not in SAFE_FALLBACK_ACTIONS:",
            "    if False:",
            "tests/p4_edge",
        ),
        (
            "发布台账允许跳态推进",
            "services/_kernel/routes.py",
            "    if not edge.release_transition_allowed(current, target):",
            "    if False:",
            "tests/p4_runtime",
        ),
        (
            "行为性变化不再使旧批准失效",
            "services/_kernel/routes.py",
            '            "approval_current": False,\n            "approval_ref": None,\n            "status": "REVOKED",',
            '            "approval_current": True,\n            "approval_ref": None,\n            "status": "REVOKED",',
            "tests/p4_runtime",
        ),
        (
            "影子 Guard 允许执行",
            "services/_kernel/routes.py",
            '        "execution_allowed": False,\n        "comparison_only": True,\n        "comparison_record_ref": f"comparison-{decision_ref}",',
            '        "execution_allowed": True,\n        "comparison_only": True,\n        "comparison_record_ref": f"comparison-{decision_ref}",',
            "tests/p4_runtime",
        ),
        (
            "spool 允许中心执行已执行事实",
            "services/_kernel/routes.py",
            '            "center_execution_allowed": False,\n            "created_at": _utc_now(),\n        }\n        journal.append("edge-spool", existing)',
            '            "center_execution_allowed": True,\n            "created_at": _utc_now(),\n        }\n        journal.append("edge-spool", existing)',
            "tests/p4_runtime",
        ),
        (
            "影子门槛可低于 14 天",
            "services/_kernel/routes.py",
            "    if int(body[\"minimum_continuous_seconds\"]) < 14 * 24 * 3600:",
            "    if False:",
            "tests/p4_runtime",
        ),
        (
            "断连时本地持久化不可用仍判可自治",
            "services/_kernel/routes.py",
            '        local_persistence_available=bool(body.get("local_persistence_available", False)),',
            "        local_persistence_available=True,",
            "tests/p4_runtime",
        ),
    ],
}


def syntax_problems(relative: str, text: str) -> list[str]:
    """变异后源码的语法问题（空列表 = 合法）。

    - `.py` → 用 `ast.parse`；
    - `.js` / `.mjs` → 交给 `node --check`（本机 node 可用时；不可用则跳过，
      并在报告里体现为"未做语法校验"这件事本身由 `node_available` 决定）。

    **为什么必须有这一步**：变异的目标是"注入一个语义缺陷"，
    而不是"让文件无法解析"。若变异引入了语法错误，测试会因解析失败而失败，
    脚本却把它记成"门禁正确捕获"——**假证据**。本项目已经吃过
    "假绿/假红"的亏，所以这里把变异本身的有效性也纳入判定。
    """
    if relative.endswith(".py"):
        import ast

        try:
            ast.parse(text)
        except SyntaxError as error:
            return [f"{error.msg} (line {error.lineno})"]
        return []

    if relative.endswith((".js", ".mjs")):
        node = shutil.which("node")
        if node is None:
            return []
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as handle:
            handle.write(text)
            path = handle.name
        try:
            completed = subprocess.run(
                [node, "--check", path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0:
                first = (completed.stderr or "").strip().splitlines()
                return [first[-1] if first else "node --check 失败"]
        finally:
            pathlib.Path(path).unlink(missing_ok=True)
        return []

    return []


def apply_mutation(text: str, original: str, mutated: str) -> tuple[str, bool]:
    """按文件自身行尾风格做替换，避免"原文未命中"被误当成"已捕获"。

    实测坑（2026-10-04）：源文件是 LF，而脚本里手写的多行原文用了 `\\n` 之外的
    隐含行尾预期；一旦不匹配，`str.replace` 静默不生效，脚本却把"测试仍通过"
    当成"捕获"——产出**假证据**，比漏检更危险。

    **第二个实测坑（2026-10-05，更隐蔽）**：原文在文件里**出现多次**时，
    `replace(..., 1)` 只改第一处。当两处形态相似（例如 P0 与 P3 两个命令处理器
    都有幂等判断），注入会打到**错误的函数**上：被测路径没变，测试自然绿，
    变异被误报为"逃逸"；更糟的是**另一条**变异随后把这个改动当成"原文"，
    于是静默改坏了生产代码（P0 的幂等判断真的被删过一次）。
    因此这里**强制要求原文唯一**，不唯一即判失败。
    """
    normalized = text.replace("\r\n", "\n")
    occurrences = normalized.count(original)
    if occurrences == 0:
        return text, False
    if occurrences > 1:
        raise ValueError(f"变异原文在文件中出现 {occurrences} 次（必须唯一，否则会改错位置）")
    replaced = normalized.replace(original, mutated, 1)
    if "\r\n" in text:  # 原文件是 CRLF，写回时保持一致
        replaced = replaced.replace("\n", "\r\n")
    return replaced, True


def run(target: str) -> bool:
    """返回 True 表示测试通过（= 变异**未被**捕获）。"""
    command = [str(VENV_PYTHON), "-m", "pytest", target, "-q", "--no-header", "--tb=no"]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"},
    )
    return completed.returncode == 0


def preflight_tree_is_clean(mutations: list) -> list[str]:
    """**起始完整性预检**：工作树里不得残留上一次的变异。

    ## 为什么需要（一次真实事故，2026-10-05）

    还原靠的是 `try/finally`。但**硬杀进程（`Stop-Process -Force`）不会执行
    `finally`** —— 实测：我在变异跑动中强杀了它，于是
    `apps/web-admin/src/approval.js` 里留下了 `if (false) {`，
    把「说明文字不能当授权」这条判据**整条禁用**了。

    更糟的是它当时**没被门禁抓住**：
    - `apps/` 在本仓**未被 git 跟踪**，`git status` / `git diff` 都看不见；
    - 原有的 JS 用例测的是判据函数本身，而不是"调用方到底有没有用它"。

    也就是说：**一次中断可以悄无声息地关掉一条安全判据**。
    所以每次开跑前必须先把这件事查出来，而不是等下次偶然发现。

    判据是**双条件**：变异后的文本在场、且原文不在场。
    只查其一都会误报——有些变异串也出现在注释或文档里。
    """
    dirty: list[str] = []
    for _name, relative, _original, mutated, _target in mutations:
        if _original is None or mutated is None:
            continue  # 已接受缺口没有原文可查
        file = REPO_ROOT / relative
        if not file.exists():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        if mutated in text and _original not in text:
            dirty.append(f"{relative}: 仍含变异文本 {mutated.strip()[:60]!r}")
    return dirty


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="p15", choices=sorted(PHASES))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    mutations = PHASES[args.phase]
    if args.list:
        for name, path, _, _, target in mutations:
            print(f"- {name}  ({path} → {target})")
        return 0

    # **起始完整性预检**：脏树必须显式失败，不能带着上一次的残留开跑。
    dirty = preflight_tree_is_clean(mutations)
    if dirty:
        print(f"结论[{args.phase}]: 工作树不干净 —— 上一次变异运行被中断（硬杀不会执行 finally）")
        for item in dirty:
            print(f"  [残留] {item}")
        print()
        print("  → 这些文件里留着**变异后的代码**，必须先还原再跑。")
        print("    `apps/` 未被 git 跟踪，`git checkout` 救不回来；请按上行的变异文本手工还原。")
        return 1

    return run_one(args.phase, mutations)


def run_one(phase: str, mutations: list) -> int:
    """跑完一个阶段的所有变异，返回退出码（0 = 全部可捕获项都被捕获）。"""
    escaped: list[str] = []
    invalid: list[str] = []
    accepted: list[str] = []
    for name, relative, original, mutated, target in mutations:
        if original is None:
            # **显式声明的已接受缺口**：该变异在结构上无法被当前观测手段捕获
            # （例如它改变的是顺序，而现有文件名让两种顺序**结果完全相同**）。
            # 这里不删掉它、也不假装捕获，而是如实报出并说明理由。
            print(f"[已接受] {name}: {mutated}")
            accepted.append(name)
            continue
        file = REPO_ROOT / relative
        backup = file.read_text(encoding="utf-8")
        patched, applied = apply_mutation(backup, original, mutated)
        if not applied:
            # 未命中的变异**没有验证任何东西**，必须显式失败，不能算作通过。
            print(f"[无效] {name}: 原文未命中，本变异未被执行（不可当作已捕获）")
            invalid.append(name)
            continue
        if patched == backup:
            print(f"[无效] {name}: 替换未改变文件内容")
            invalid.append(name)
            continue
        # **变异后必须语法合法**：否则"测试挂掉"是**语法错误导致的**，
        # 会被误报成"门禁正确捕获"。那是最糟的假证据——
        # 报告说门禁有牙，实际只是文件根本跑不起来。
        problems = syntax_problems(relative, patched)
        if problems:
            print(f"[无效] {name}: 变异后语法不合法（{problems[0]}）——不可当作已捕获")
            invalid.append(name)
            continue
        try:
            file.write_text(patched, encoding="utf-8", newline="")
            passed = run(target)
        finally:
            file.write_text(backup, encoding="utf-8", newline="")
        if passed:
            print(f"[逃逸] {name}: 注入缺陷后门禁**仍然通过**")
            escaped.append(name)
        else:
            print(f"[捕获] {name}: 门禁正确报红")

    print()
    # `accepted` 不计入问题：它们不是"没抓到"，而是"已如实登记为抓不到"。
    problems = escaped + invalid
    if problems:
        detail = []
        if escaped:
            detail.append(f"{len(escaped)} 个变异未被捕获（门禁有盲区）: {', '.join(escaped)}")
        if invalid:
            detail.append(f"{len(invalid)} 个变异无效未执行（脚本/原文失配）: {', '.join(invalid)}")
        print(f"结论[{phase}]: " + "；".join(detail))
        return 1
    tail = f"（另有 {len(accepted)} 个已接受缺口，见上方 [已接受] 行）" if accepted else ""
    print(f"结论[{phase}]: 全部 {len(mutations) - len(accepted)} 个可捕获变异都被门禁捕获{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
