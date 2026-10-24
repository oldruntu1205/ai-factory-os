# L1 修正请求 · P1.5 门禁的全局清单耦合（第 3 次同源缺陷）

## 【任务】
修正 `tests/contracts/test_p15_contracts.py::test_locked_inventory_is_complete`，
消除它对"契约目录里有哪些文件"的全局假设，改用**唯一登记处** `contract_inventory.py`。

## 【SPEC 关键条款】
你此前（P1.5 阶段）为本项目新建的唯一登记处：
`tests/contracts/contract_inventory.py`，提供 `files_of(phase)` / `all_registered()` /
`present()` / `unregistered()` / `missing()` / `phase_of(rel)`。

它存在的**唯一理由**就是：新增阶段的契约不该让旧阶段的门禁报红，
同时"目录里出现的任何契约文件都已登记"这条判据必须仍然成立。

## 【失败测试】
```
tests/contracts/test_p15_contracts.py::test_locked_inventory_is_complete
```

## 【错误栈】
```
    def test_locked_inventory_is_complete():
        assert len(LOCKED_SHA256) == 19  # P0 12 文件 + P1 7 文件
        assert {p for p in LOCKED_SHA256 if p.startswith("openapi/")} == {
            "openapi/p0.openapi.json", "openapi/p1.openapi.json"}
        schemas = {p.name for p in (CONTRACT / "schemas").glob("*.json") if not p.name.startswith("p15-")}
>       assert schemas == {Path(p).name for p in LOCKED_SHA256 if p.startswith("schemas/")}
E       AssertionError: ...
E         Extra items in the left set:
E         'p3-saga.json' / 'p3-approval.json' / 'p3-execution.json'
E         'p3-tool-admission.json' / 'p3-cancellation.json' ...
tests/contracts/test_p15_contracts.py:187: AssertionError
```

## 【相关代码】
```
184:        schemas = {p.name for p in (CONTRACT / "schemas").glob("*.json") if not p.name.startswith("p15-")}
185:        assert schemas == {Path(p).name for p in LOCKED_SHA256 if p.startswith("schemas/")}
```

## 【已试过的修复】
- 未改任何测试（按纪律上报）。
- 已确认根因单一：P3 新增 7 个 `p3-*.json` 落进"非 `p15-` 前缀"集合，
  该集合被当作"应当等于 P0+P1 的 19 个文件"，于是失败。
- 已确认 P3 契约本身没问题：`test_p3_contracts.py` **95 passed / exit 0**。

## 【明确问题】
这是**同一类缺陷的第 3 次出现**（前两次：P1 门禁的 `existing - P1_FILES == P0_SHA256`；
P0 门禁扫描整个 `openapi/`+`schemas/`）。每次都是"阶段门禁里硬编码了全局文件集合"。

请你一次性把它变成**登记处驱动**的写法，例如：

```python
from .contract_inventory import files_of, missing, unregistered

def test_locked_inventory_is_complete():
    assert files_of("p15") == set(LOCKED_SHA256)            # 本阶段 = 本阶段锁定的文件
    assert files_of("p0") | files_of("p1") <= files_of("p15") | files_of("p0") | files_of("p1")
    assert unregistered() == set(), f"存在未经阶段登记的契约文件: {sorted(unregistered())}"
    assert missing() == set()
```

（上面只是方向示意，**具体写法由你定**；关键判据是两条：
① 本阶段锁定的文件集合 == 本阶段登记的文件集合；
② 「目录里没有未登记的契约文件」由登记处裁决，而不是由"前缀排除法"裁决。）

同时请**检查 P0/P1/P3 契约门禁里是否还有同类写法**（`glob("*.json")` 之后再与前缀/硬编码集合比较），
一并改为登记处驱动 —— 否则 P4/P5 会第 4、第 5 次踩同一个坑。

若确实需要在某阶段固定"当时锁定的字节"，保留 sha256 常量是对的；
**要改的是"目录里有哪些文件"这条判据的来源**。

## 【约束】
- 只改 `tests/contracts/*` 与（如需）`tests/contracts/contract_inventory.py`；
- **不得**修改任何 `packages/contracts/` 下的契约文件；
- **不得**通过删除 P3 契约文件或放宽断言来让测试变绿。
