# ADR-0008 · Jev 走适配层接入，内核不得内嵌决策逻辑

- 状态：**已接受**
- 日期：2026-10-05
- 上游依据：
  - §3「**Jev Decision Fabric** | Route/Classify/Score/Rank/Filter/Gate/Select/Escalate，
    **输出有限类型决策** | 不执行交易、不发布排程；**高置信不得提升权限**」
  - §3「**Jev 是逻辑角色及决策接口；真实 Jev 产品、兼容实现或本地决策模型
    均需实现同一契约。**」
  - §9「decision-service | Rules、**Jev 接口**、分类/排序/门禁决策 | decision |
    security 模型接口、批准的规则版本」
  - §13「实际版本与 Hermes/**Jev 适配方式需 P0 冻结**」
  - §16.1「外部组件适配 | **Hermes/OpenClaw/Jev 版本、许可证、授权与升级策略** |
    实际产品接入」

## 背景

上游把 Jev 定义为一个**逻辑角色及决策接口**，并明确列出三类实现者：
**真实 Jev 产品 / 兼容实现 / 本地决策模型**——三者**均需实现同一契约**。

而在本次决定之前，决策逻辑是**内嵌在 `services/_kernel/routes.py` 的
`_handler_decision` 里**的：

```python
route = (catalog.get("routes") or [None])[0]        # 永远取目录首项
action = (catalog.get("actions") or [None])[0]
category = (catalog.get("categories") or [None])[0]
confidence = 0.71 if calibration_version else None  # 硬编码
```

这造成一个**与上游直接冲突**的后果：**换任何一个真实 Jev 都必须改内核**。
上游说"均需实现同一契约"，隐含的是**契约是接缝**；
接缝若开在内核内部，契约就不再是接缝，而只是一段写在处理器里的约定。

同时 §13 要求"Jev 适配方式需 **P0 冻结**"——本仓当时**没有任何冻结点**：
既没有适配接口，也没有登记项，也没有 ADR。这是本次要补的三件事。

## 决策

### 1. 适配接口落在 `packages/decision/`（不落在内核，也不落在服务目录）

本仓约定 `services/<name>/` 是**零逻辑运行单元**（只有 `__main__.py` 调
`main(SERVICE_ID)`），逻辑在 `services/_kernel/`。而
`services/decision-service` **带连字符，无法作为 Python 包名 import**。

若把适配逻辑放进服务目录，只能靠 `importlib` 按路径加载——那会带来一个新的
"两套加载方式"，且 `decision-service` 将不再是零逻辑单元。

因此接口落在 `packages/decision/`：**纯逻辑，不依赖服务 DB 或运行时**，
与 `packages/domain/` 同一形态。内核与将来的 `decision-service` 都可 import 它，
不会出现"同一份适配逻辑两处各写一遍"。

### 2. 适配方式冻结在登记表 `decision_adapter` 节点

```json
"decision_adapter": {
  "implementation": "mock",
  "config": { "confidence_source": "fixed-mock-constant", "confidence_calibrated": false }
}
```

**未登记的实现 id ⇒ fail-closed 拒绝**，**不退回 Mock**。
退回 Mock 的后果是：生产配置写错时，系统用一个确定性 Mock 做决策，
而调用方与界面**看不出区别**——那是最难发现的一类故障。

### 3. 决策类型是上游的**封闭清单**，且逐个必须有归宿

上游 §3 列出 **8 种**：`Route / Classify / Score / Rank / Filter / Gate / Select / Escalate`。
本适配器实现 **4 种**（`Route / Classify / Gate / Escalate`），
其余 4 种（`Score / Rank / Filter / Select`）**显式拒绝**为
`DECISION_KIND_NOT_IMPLEMENTED`（HTTP 422）。

**为什么拒绝而不是凑一个结果**：这 4 种需要真实打分与排序依据，
而上游 §8.2 要求「置信度是经独立数据校准的任务级指标，**不能取 LLM 自报数字**」。
Mock 给不出有意义的数字——**宁可拒绝，也不假装**。

### 4. 三类字段同源，不许漂移

决策类型清单同时存在于三处：**契约**（`decision_kind` 枚举）、
**登记表**（`decision_catalog.decision_kinds`）、**实现**（`packages/decision/adapter.py`）。
`tests/decision/` 逐条比对三者，任一处漂移即报红。

理由：这类"多份名单"是两套真相的高产地，而漂移的表现是
**调用方拿到一个不属于任何已知类型的值**，且不报错。

### 5. 内核只做编排

抽取后 `_handler_decision` 只做四件事：**校验目录与身份 → 解析适配器 →
调用它 → 组装回执**。`tests/decision/` 有一条判据直接检查内核里**不再出现**
`catalog.get("routes")` 这类直取——**残留即意味着换实现仍要改内核**。

## 后果

**好处**

- 上游 §3「均需实现同一契约」**成立**了：换真实 Jev 只需 ① 写一个满足
  `DecisionAdapter` 的类 ② 在登记表改 `implementation` ③ **内核一行不改**；
- §13 的"P0 冻结适配方式"有了**落实点**（登记表节点 + 本 ADR）；
- 内核变薄：决策逻辑不再与 HTTP 编排混在一起；
- 未实现的决策类型**有名字地缺**（`DECISION_KIND_NOT_IMPLEMENTED`），
  而不是静默返回一个看起来成功的错误答案。

**代价与遗留**

- `services/decision-service/` 仍是**零逻辑**目录，真实实现（模型推理）尚未落地——
  本 ADR 只解决"怎么接"，不解决"接什么"；
- **8 种里只实现 4 种**，`Score/Rank/Filter/Select` 需真实校准数据；
- `confidence` 仍是**固定 Mock 常数**（0.71），`config.confidence_calibrated: false`
  **如实标注**了它不是校准值；
- **真实 Jev 产品的版本、许可证、授权与升级策略仍未定**（上游 §16.1 的待填依赖）。

## 什么情况会推翻本决定

1. **真实 Jev 产品提供了官方 SDK，且其接口无法用本适配层表达**
   （例如它要求双向流式或客户端持有的状态机）——
   那时需要重新评估"内核只做编排"是否仍成立；
2. **上游把 Jev 从"逻辑角色"改成"必须使用某个具体产品"**——
   契约不再有多个实现者，适配层的存在理由减弱；
3. **8 种类型的使用方式被上游收窄**（例如明确"首期只允许 Route"）——
   则未实现的 4 种应从契约中移除，而不是留在契约里拒绝。

## 备选方案与不选的理由

| 备选 | 为什么不选 |
| --- | --- |
| **A. 不改，保持决策逻辑在内核里** | 与上游 §3「真实 Jev 产品、兼容实现或本地决策模型**均需实现同一契约**」直接冲突——契约若开在内核内部，就只是"写死在处理器里的一段约定"。同时 §13 要求的"P0 冻结适配方式"无处落实。 |
| **B. 适配逻辑放进 `services/decision-service/`** | 该目录**带连字符，不能作为 Python 包名 import**，只能靠 `importlib` 按路径加载。那会引入第二种模块加载方式，并让 `decision-service` 不再是"零逻辑运行单元"（违反本仓 §15 约定）。 |
| **C. 适配逻辑放进 `services/_kernel/`（同层新模块）** | 表面上"能 import"了，但适配层与内核仍在同一包内：内核仍可随手直取目录，**接缝不成立**。`tests/decision` 里那条"内核不得残留决策逻辑"的判据就没法立。 |
| **D. 未实现的 4 种决策类型先返回 `Route` 结果、标个 `TODO`** | 调用方会拿到一个**看起来成功的错误答案**。这类"静默降级"是本项目一贯要防的（同 §5「禁止 UNKNOWN 状态换新键重发」的取向）。 |
| **E. `confidence` 在 Mock 下也给一个高值（如 0.95）"看着像样"** | 上游 §3「高置信**不得提升权限**」+ §8.2「不能取 LLM 自报数字」。0.95 落在自动候选区间（`>0.92`），会示范"高置信即可自动"——**Mock 更不该示范这件事**。 |
| **F. 换了实现但先退回 Mock（不 fail-closed）** | 生产配置写错时，系统用一个确定性 Mock 做决策而调用方看不出区别——**最难发现的一类故障**。 |

## 未纳入本决定的范围

- **决策质量**（Route 选得对不对）——本适配层只保证形状与边界；
- **执行授权**——上游 §0.1 原则 2 明写"Jev 的输出是**建议**"。
  能否执行由 Business Control 的 Policy 判定（`/internal/v1/policy/evaluate`），
  这是**两条不同的治理轴**，本 ADR 不越界；
- **Jev 与 Edge-RLCD 的关系**——上游 §8「两者可复用 Schema、校准和评测规范，
  但**不共享执行权限**」，属 P4 范围。
