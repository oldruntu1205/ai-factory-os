# Guard 判据接线 + "未接线判据"机械守卫

- 日期：2026-10-05
- 依据：上游 §9（Edge Guard 判定优先级）、§17 A09
- 判据：`scripts/gate.py --stages p0,p1,p15,p2,p3,p4,assertions` **exit 0**（1316 用例）；
  变异 **139 个可捕获项全捕获**

---

## 一、动机：上一轮事故讲的是"判据存在 ≠ 判据被使用"

上一轮硬杀变异脚本，`approval.js` 留下 `if (false) {`，
把一条安全判据**整条禁用**——测试测的是判据函数，而失效的是**调用点**。

本轮用**同一视角**审计全仓，找到同形态的存量缺陷。

## 二、找到的缺陷：Guard 的共享判据从未被生产调用

`packages/domain/guard.py` 里有两个判据，**有单测、生产引用 = 0**：

| 判据 | 生产引用 |
| --- | --- |
| `highest_priority_stage(reasons)` | **0** |
| `reason_requires_fallback(reason)` | **0** |

原因：`services/_kernel/edge.py::evaluate_guard` **内联重写**了同一套判定：

```python
order = {stage: i for i, stage in enumerate(GUARD_STAGES)}   # = highest_priority_stage 已实现
failures.sort(key=...); top_stage = failures[0][0]
fallback_required = any(r in REASONS_REQUIRING_FALLBACK ...) # = reason_requires_fallback 已实现
```

**后果不是功能坏了**（两份当时等价），而是：

- **两份判据会各自演进**，单测绿不代表生产跟着变；
- 有人改 `guard.py`（那份**有测试、看起来是权威**的）时，生产行为**纹丝不动**
  —— 最坏的一种"改对了没生效"。

### 修法

1. `evaluate_guard` 改为**调用**共享判据（`sort` 保留，用于稳定顺序）：
   ```python
   failures.sort(key=lambda item: GUARD_STAGE_INDEX[item[0]])
   top_stage = highest_priority_stage(reasons)
   fallback_required = any(reason_requires_fallback(reason) for reason in reasons)
   ```
2. 去掉 `edge` 对 `REASONS_REQUIRING_FALLBACK` 的**死再导出**
   （留着会让下一个读者以为判据在这里比对）。

**实测无回归**：p4_edge + 契约 + 断言 **670 passed**。

### 顺带修强了一条既有测试

`test_metrics_and_guard_share_one_vocabulary` 原来靠**再导出**验证词表共用：

```python
assert edge.REASONS_REQUIRING_FALLBACK == guard.REASONS_REQUIRING_FALLBACK
```

那是死代码路径。改成验**行为**：要求回退的原因码（`INTERLOCK_DENIED`）⇒ `edge` 必须给
`FALLBACK`；不要求回退的（`CONFIDENCE_NOT_CALIBRATED`）⇒ 必须给 `DENY`。
**这比比较两个常量强**——常量相等不保证生效的那份在用它。

---

## 三、机械化：`tests/infra/test_unwired_guards.py`

把"审计发现"变成**持久守卫**：生产模块的公共函数**必须真的被生产代码调用**，
否则必须登记。

### 但是——不能让守卫退化成误报机器

全仓 **123 个公共函数，24 个生产未调用**。绝大多数**不是缺陷**
（静态审计函数、测试夹具、对外库 API）。所以判据是
**"默认要求接线，例外必须登记理由"**，分三类：

| 类别 | 特征 | 例 |
| --- | --- | --- |
| `audit` | 由静态审计/门禁消费，不参与营业 | `tables_without_rls`、`label_key_problems` |
| `fixture` | 为测试构造场景 | `write_migrations`、`acquire_lease` |
| `library` | 对外库 API，语义由调用方定 | `plan_compensation` |

**`library` 最容易用来掩盖"平行实现"**，所以登记时必须写清**为什么不能接线**。

### 四条断言（含能力自检）

| 断言 | 防什么 |
| --- | --- |
| 未登记项 ⇒ 报红 | 核心判据 |
| 登记项不得**已接线/已删除** | 防清单退化成"没人维护的历史账" |
| 每条必须写类别 + 具体理由 | 无理由的登记等于没有约束 |
| **能力自检**：注入孤儿函数必须被识别 | 干净树上"没有未登记"与"根本不会识别"无法区分 |

**能力自检是真的**（实测输出）：

```
注入后是否识别到该函数: True
未登记项: ['unregistered_orphan_predicate']
已还原: True
```

外加一条**反身自检**：`highest_priority_stage` / `reason_requires_fallback`
**不得**再出现在未接线集合里——有人退回内联实现就报红。

### 守卫当场抓到了我自己的偷懒

我初版把这两个函数也**登记**进了允许表（理由写"已于本轮接线"）。
守卫立刻报红：**已接线的项不得留在登记表里**。我照做移除了。

> 这是守卫有效性的一个真实证据：**它拦住了作者本人的自我开脱。**

### 一处诚实标注

`plan_compensation` / `plan_cancellation` / `compensation_outcome`
（`transactions.py`）仍**未接线**，登记为 `library` 并写明原因：
营业侧是**另一处实现**（`_handler_p3_saga_compensate` 用 dict 形状，
本函数用 `SagaStep` 形状，接线要一层转换——而转换正是出 bug 的地方）。
当前由 `tests/p3_transactions` 的**防漂移断言**把两处钉住。
彻底统一需要单独切片，**不在本轮范围内**（动已验证路径的收益不抵风险）。

---

## 四、测试与变异

| 套件 | 条数 | 内容 |
| --- | --- | --- |
| `tests/p4_edge/test_guard_wiring.py` | 7 | 接线是结构事实 + 行为事实（阶段顺序、置信度最后、互锁不可覆盖、全通过 ⇒ ALLOW） |
| `tests/infra/test_unwired_guards.py` | 5 | 未登记报红、登记不过期、理由必填、能力自检、反身自检 |

变异（新增 3 条，均验证有害）：
- Guard 判据退回内联实现；
- Guard 回退判据退回内联比对；
- 生产模块里新增一个从未被调用的公共判据（未登记）。

## 五、已知限制

1. **未接线守卫是启发式**：靠"文本引用次数 ≤ 1"判断，会有边界情况
   （例如入口函数天然没人调用）。所以它**报出疑似项由清单裁决**，
   而不是自动判定——这正是需要 `ALLOWED_UNWIRED` 的原因。
2. **只覆盖 `services/` 与 `packages/`**：`apps/`（JS）未纳入同类检测。
   上一轮事故正发生在 `apps/`，但 JS 侧的"未使用导出"需要另一套工具（AST 也要换），
   本轮未做。
3. **`plan_*` 三组仍是平行实现**：只加了防漂移断言，未统一成单一来源。
4. 守卫不保证登记的理由**正确**——它只保证"必须写下来"。
