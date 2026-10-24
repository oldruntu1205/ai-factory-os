# L1 修正请求 · P3 契约测试夹具自相矛盾（最小失败包）

## 【任务】
修正 `tests/contracts/test_p3_contracts.py` 中与自身契约 pattern 矛盾的夹具值，
使 P3 契约静态门禁能跑绿（当前 33 failed / 62 passed）。

## 【SPEC 关键条款】
你在 `p3-approval.json` 里定义（另在 `p3-command.json`、`p3-tool-admission.json` 复用同一 `$defs`）：

```
schema_ref:
  type: string
  pattern: ^[a-z0-9][a-z0-9-]*\.json(?:#/[A-Za-z0-9_$~/-]+)?$
  description: 同一 schemas 目录内的 JSON Schema 相对引用；必须存在并校验 hash，禁止 URL。
```

即 `schema_ref` 必须是 `<文件名>.json`（可带 `#/指针`），**不能是标识符**。

## 【失败测试】
```
tests/contracts/test_p3_contracts.py
  33 个用例失败，全部同一根因，例如：
  - test_nested_context_required_and_sensitive_inline_payload_rejected[p3-approval.json-Fact-approval]
  - test_nested_context_required_and_sensitive_inline_payload_rejected[p3-execution.json-Credential-credential]
  - test_nested_context_required_and_sensitive_inline_payload_rejected[p3-command.json-View-command]
  - test_a06_review_fails_closed_on_any_failed_check[fencing_current]
  - test_a06_review_fails_closed_on_any_failed_check[object_lock_held]
  - test_a06_review_fails_closed_on_any_failed_check[not_cancelled_before_start]
  - test_lease_fencing_and_checkpoint_contract
  - test_compensation_failed_or_unknown_never_COMPENSATED
  - test_false_toolspec_capability_denies_unattended_high_risk[idempotency_support|status_query|compensation]
```

## 【错误栈】
```
E   AssertionError: 'task-demo' does not match '^[a-z0-9][a-z0-9-]*\.json(?:#/[A-Za-z0-9_$~/-]+)?$'

Failed validating 'pattern' in schema['properties']['binding']['properties']['payload_schema']['properties']['schema_ref']:
    {'type': 'string',
     'pattern': '^[a-z0-9][a-z0-9-]*\\.json(?:#/[A-Za-z0-9_$~/-]+)?$',
     'description': '同一 schemas 目录内的 JSON Schema 相对引用；必须存在并校验 hash，禁止 URL。'}

On instance['binding']['payload_schema']['schema_ref']:
    'task-demo'
tests/contracts/test_p3_contracts.py:140: AssertionError
```

## 【相关代码】`tests/contracts/test_p3_contracts.py`

```
155:            binding=dict(
156:                payload_hash=HASH,
157:                payload_schema=dict(schema_ref="task-demo", schema_version="3.0.0", schema_hash=HASH),
158:                ...
```

```
618:        tool[field] = dict(schema_ref="task-demo", schema_version="3.0.0", schema_hash=HASH)
620:        ...
```

## 【已试过的修复】
- 确认不是实现问题：P3 门禁只读 `packages/contracts/`，不加载 `services/`。
- 已核对：P0/P1/P1.5 契约未被改动（`test_p0_original_bytes_are_locked` 等 26 个基线锁定用例通过）。
- 已核对 P3 schema 文件确实存在且能解析（`test_p3_parse_dialects_and_local_references` 通过）。
- 已核对受影响的 33 个用例**全部**是同一条 `schema_ref` pattern 报错（对失败签名做了归类：32 条为同一签名，另 1 条经 `--tb=long` 展开确认是同一根因的另一处夹具）。
- 未修改测试文件（L2 不改 L1 测试，按纪律上报）。

## 【明确问题】
夹具把 `schema_ref` 填成了占位标识符 `"task-demo"`，而契约要求它是 schema 文件名。
请你定：`schema_ref` 应指向哪个真实契约文件（建议 `task.json`，它在 `schemas/` 目录内且
不参与 `p3-*.json` 文件集合断言），然后修正上述 **两处**（第 157、618 行）夹具值。

若你认为夹具值没错、应当放宽 pattern，请说明理由并直接改契约 —— 但**不要**因为夹具而为
"标识符也能当 schema_ref"放宽约束：那会让"必须存在并校验 hash"这条语义失效。
