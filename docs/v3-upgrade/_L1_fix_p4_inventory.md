# L1 修正请求 · 登记处门禁用例硬编码"未来阶段"（第 4 次同类缺陷）

## 【任务】
修正 `tests/contracts/test_contract_inventory_gates.py`：它把 `"p4"`、`"p5"` 当作
**尚未存在的未来阶段**硬编码在参数里。现在 **P4 已经是真实阶段**（契约已落地并登记），
于是这两个用例自相矛盾、门禁报红。

## 【失败测试】
```
tests/contracts/test_contract_inventory_gates.py
  - test_registered_future_stage_does_not_affect_earlier_gates[p4-p0/p1/p15/p3]   (4)
  - test_unregistered_contract_is_rejected[openapi/p4.openapi.json-p0/p1/p15/p3]  (4)
  → 8 failed
```

## 【错误栈】
```
tests/contracts/test_contract_inventory_gates.py:45: in test_registered_future_stage_does_not_affect_earlier_gates
    assert inventory.unregistered() == set()
E   AssertionError: assert {'schemas/p4-observation.json', 'schemas/p4-release.json',
                            'schemas/p4-autonomy.json', 'schemas/p4-scenario.json', ...} == set()
```
（`test_unregistered_contract_is_rejected` 同理：它预期 `p4.openapi.json` **未登记**，
但 P4 现在是已登记的阶段。）

## 【相关代码】
```python
# tests/contracts/test_contract_inventory_gates.py
36: @pytest.mark.parametrize("phase", ("p4", "p5"))        # ← 硬编码"未来阶段"
37: def test_registered_future_stage_does_not_affect_earlier_gates(...):

52: @pytest.mark.parametrize("relative", ("openapi/p4.openapi.json", "schemas/p4-future.json"))
53: def test_unregistered_contract_is_rejected(...):
```

## 【已试过的修复】
- 已把 P4 登记进 `contract_inventory.CONTRACT_INVENTORY`（`openapi/p4.openapi.json` +
  8 个 `p4-*.json`）——这是**正确**的一步，不是绕过。
- 未改这个测试文件（按纪律上报）。
- 已确认其余契约用例：`tests/contracts` 581 passed，仅此 8 条因该硬编码失败。

## 【明确问题】
"未来阶段"应当是**推导出来的**，不是字面量。请改成动态推导，例如：

```python
UNKNOWN_PHASE = next(name for name in ("p9", "p99") if name not in inventory.CONTRACT_INVENTORY)
```

即：取一个**确实不在**登记表里的阶段名。这样无论以后新增到 P5/P6/P7，
这些"隔离回归"用例都不会自相矛盾——它们验证的是"未登记的阶段不影响旧门禁"这一**机制**，
与"P4 是否已经存在"无关。

请同时检查该文件里是否还有其它"把当前阶段当未来阶段"的写法。

## 【这是第 4 次同类缺陷 —— 请一并给出根治办法】
前三次都是"阶段门禁里硬编码全局文件集合"：
1. P1 门禁 `existing - P1_FILES == set(P0_SHA256)`；
2. P0 门禁扫描整个 `openapi/`+`schemas/`；
3. P1.5 门禁 `schemas minus p15-*` 当作 P0+P1；
4. 本次：登记处门禁把 p4/p5 当"未来"。

共同点都是**把"稳定的机制断言"写成了"当时快照的字符串"**。
请在汇报里用 3 行以内说清：这个文件里还有哪些"快照式写法"，
以及是否需要在 `contract_inventory.py` 里提供一个
`assert_phase_is_registered(name)` / `unregistered_phase()` 之类的**机制化**辅助，
让后续阶段接入时**不需要再改任何门禁用例**。

## 【约束】
- 只改 `tests/contracts/*` 与（如需）`tests/contracts/contract_inventory.py`；
- **不得**修改 `packages/contracts/` 下任何文件；
- **不得**通过删除 P4 登记或放宽断言来让测试变绿。
