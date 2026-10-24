"""运维指标（上游 §14）：从**已有 journal 派生**，不另立计数器。

## 为什么派生而不是累加

上游要求"就绪状态包含关键依赖、策略版本和积压"、指标覆盖任务成功率/审批等待/
UNKNOWN 命令/补偿失败/投影延迟/DLP 拒绝/模型耗时与成本/Edge 漂移与断连/spool 容量。

有两种实现方式：

1. 在每个业务动作旁边**累加计数器**；
2. 从已经 fsync 落盘的 journal **重新算**。

选 2。理由：计数器与事实是**两份真相**——一旦某个分支忘了累加，
指标就会长期偏离事实，而且**没有任何东西会发现**。
journal 是 append-only 的事实来源，从它派生的指标天然与事实一致，
并且可以随时复算核对。

## 硬约束（上游 §14 明文）

- **指标标签不得包含客户名称、Prompt 正文或密钥**。
  因此本模块的标签值是**封闭集合**（如结果枚举、风险级别），
  自由文本**根本进不来**——不是"检查有没有敏感词"，而是"没有位置可放"。
- 缺数据要如实表达为"无数据"，**不得**用 0 冒充（0 与"没测过"含义完全不同）。

## 诚实边界

本模块只对**真的存在对应 journal**的指标给出数值；没有事实来源的指标
一律标 `no_data` 并写明缺什么。`unwired_signals()` 把这件事变成可机检的清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from . import journal

# Guard 的阶段与原因码词表**唯一来源**在共享领域包（上游 §15）；
# 指标侧不再手抄一份（手抄的两份之间没有任何机制保证一致）。
from packages.domain.guard import (
    GUARD_REASON_TO_STAGE as _GUARD_REASON_TO_STAGE,
    GUARD_STAGES as _GUARD_STAGES,
    stage_of_reason as _stage_of_reason,
)

# Guard 原因码 → 判定阶段名（唯一来源在 domain）。
_REASON_TO_STAGE: dict[str, str] = dict(_GUARD_REASON_TO_STAGE)

# 指标可用的阶段标签：现场阶段 + 观测侧的"未识别"桶。
#
# 为什么要 +"unknown"：观测侧需要一个"未识别"落点，否则遇到未登记原因码时
# 整个指标计算会抛错（把观测能力打挂）。这与**现场判定阶段**是两回事——
# 现场那 9 个阶段必须与契约逐字一致，指标多一个桶不影响判定语义。
STAGE_LABELS: tuple[str, ...] = tuple(_GUARD_STAGES) + ("unknown",)

# 当前环境标注。上游要求送审包必须标明 mock / sandbox / 真实环境；
# 指标是"容易被误读成生产就绪"的东西，所以把环境**写进响应本身**而不是只写在文档里。
ENVIRONMENT = "mock"


@dataclass(frozen=True)
class Signal:
    """一个指标的定义。

    `sources` 是它能派生的 journal 名（空 = 当前还没有事实来源）。
    `labels` 是该指标允许的标签键——**只有这里列出的键**会被输出。
    `allowed_values` 是每个标签键的**封闭取值集合**：
    这是"标签不含客户名称/Prompt/密钥"的**真正防线**。

    为什么格式校验不够：`sk-live-abcdef123456` 恰好符合"小写字母数字连字符"
    的令牌格式（实测被放行）。所以封闭性不能靠格式，只能靠**取值范围是有限的**。
    没有声明取值集合的标签键，一律拒绝输出。
    """

    key: str
    unit: str
    description: str
    sources: tuple[str, ...]
    labels: tuple[str, ...] = ()
    allowed_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    upstream_requirement: str = ""


# 上游 §14 点名要求的指标（顺序与原文一致，便于逐条对照）
#
# `allowed_values` 是**封闭取值集合**——这是标签安全的真正防线。
# 每个标签键都必须给出有限取值；给不出就说明该维度不该做标签。
SIGNALS: tuple[Signal, ...] = (
    Signal(
        key="task_success_total",
        unit="count",
        description="任务成功计数（只由权威执行回执驱动）",
        sources=("tasks", "receipts"),
        labels=("outcome",),
        allowed_values={"outcome": ("succeeded", "accepted_only", "failed", "cancelled")},
        upstream_requirement="任务成功率",
    ),
    Signal(
        key="approval_wait_seconds",
        unit="seconds",
        description="审批等待时长（提交到决定）",
        sources=("approvals",),
        labels=("decision",),
        allowed_values={"decision": ("granted", "denied", "pending")},
        upstream_requirement="审批等待",
    ),
    Signal(
        key="unknown_command_total",
        unit="count",
        description="进入 UNKNOWN 的命令数（超时后待收敛）",
        sources=("commands", "reconciliations"),
        labels=("convergence",),
        allowed_values={
            "convergence": ("succeeded", "not_executed", "unknown", "failed", "accepted", "manual_review")
        },
        upstream_requirement="UNKNOWN 命令",
    ),
    Signal(
        key="compensation_failure_total",
        unit="count",
        description="补偿失败并转入人工队列的次数",
        sources=("sagas", "compensations"),
        labels=("outcome",),
        allowed_values={
            "outcome": ("compensated", "failed", "manual_review", "not_started", "in_progress", "unknown")
        },
        upstream_requirement="补偿失败",
    ),
    Signal(
        key="projection_lag_seconds",
        unit="seconds",
        description="投影相对事实的滞后",
        sources=("inbox", "outbox"),
        labels=("consumer_unit",),
        allowed_values={
            "consumer_unit": (
                "control-plane", "manufacturing-core", "transaction-service",
                "tool-gateway", "trajectory-eval", "security-gateway",
                "decision-service", "hermes-runtime", "unknown",
            )
        },
        upstream_requirement="投影延迟",
    ),
    Signal(
        key="dlp_rejection_total",
        unit="count",
        description="出域被拒次数（含敏感等级与密钥材料）",
        sources=("egress-audit",),
        labels=("reason",),
        allowed_values={"reason": ("c0", "c1", "c2", "c3", "c4", "unknown_class", "key_material", "unknown")},
        upstream_requirement="DLP 拒绝",
    ),
    Signal(
        key="model_invocation_total",
        unit="count",
        description="模型调用受理次数（按后端），**不代表已发生真实推理**",
        sources=("model-invocations",),
        labels=("backend",),
        allowed_values={"backend": ("local", "enterprise_cloud", "public", "unknown")},
        upstream_requirement="模型耗时/成本",
    ),
    Signal(
        key="governance_decision_seconds",
        unit="seconds",
        description=(
            "**治理判定路径**耗时（准入校验 + 登记表读取）。"
            "**不是**模型生成耗时——当前没有代码真正调用模型，"
            "把判定耗时冒充生成耗时会让运维误判容量"
        ),
        sources=("model-invocations",),
        labels=("backend",),
        allowed_values={"backend": ("local", "enterprise_cloud", "public", "unknown")},
        upstream_requirement="模型耗时/成本",
    ),
    Signal(
        key="model_cost_total",
        unit="cost_unit",
        description="模型调用成本累计（单位由计费口径决定，**待冻结**）",
        sources=("model-invocations",),
        labels=("backend",),
        allowed_values={"backend": ("local", "enterprise_cloud", "public", "unknown")},
        upstream_requirement="模型耗时/成本",
    ),
    Signal(
        key="edge_drift_total",
        unit="count",
        description="Edge 判定为漂移的次数",
        sources=("edge-guard-decisions",),
        labels=("stage",),
        allowed_values={
            "stage": (
                STAGE_LABELS
            )
        },
        upstream_requirement="Edge 漂移",
    ),
    Signal(
        key="edge_rejection_total",
        unit="count",
        description="Edge 判定被拒次数",
        sources=("edge-guard-decisions",),
        labels=("stage",),
        allowed_values={"stage": STAGE_LABELS},
        upstream_requirement="拒绝率",
    ),
    Signal(
        key="edge_disconnection_total",
        unit="count",
        description="Edge 断连判定次数",
        sources=("edge-autonomy-decisions",),
        labels=("outcome",),
        allowed_values={"outcome": ("autonomy_allowed", "blocked")},
        upstream_requirement="断连",
    ),
    Signal(
        key="edge_spool_entries",
        unit="count",
        description="本地 spool 条目数（按类型）",
        sources=("edge-spool",),
        labels=("kind",),
        allowed_values={"kind": ("comparison", "executed_fact", "pending_action")},
        upstream_requirement="spool 容量",
    ),
)

SIGNAL_BY_KEY: dict[str, Signal] = {signal.key: signal for signal in SIGNALS}

# 明令禁止出现在标签里的键——即便有人新增了带这些键的指标，也会被门禁拒绝。
FORBIDDEN_LABEL_KEYS = (
    "tenant_id",
    "tenant",
    "factory_id",
    "customer",
    "customer_name",
    "actor_id",
    "user",
    "employee_id",
    "task_id",
    "trace_id",
    "prompt",
    "prompt_text",
    "content",
    "body",
    "text",
    "secret",
    "token",
    "key",
    "password",
    "credential",
)

# 标签值必须是**受控令牌**：小写字母/数字/下划线/连字符，长度受限。
# 这样自由文本（正文、姓名、URL）在格式上就进不来。
import re

_LABEL_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")


def label_key_problems() -> list[str]:
    """检查指标定义里的标签键是否越界（含禁用键、缺封闭取值、标签过多）。"""
    problems: list[str] = []
    for signal in SIGNALS:
        for key in signal.labels:
            if key in FORBIDDEN_LABEL_KEYS:
                problems.append(f"{signal.key}: 标签键 {key} 在禁用名单里（§14 禁止客户名称/Prompt/密钥进标签）")
            allowed = signal.allowed_values.get(key)
            if not allowed:
                problems.append(f"{signal.key}.{key}: 缺封闭取值范围（必须给出有限取值集合）")
            elif len(allowed) > 24:
                problems.append(f"{signal.key}.{key}: 取值集合过大（{len(allowed)}），标签基数会爆炸")
        undeclared = set(signal.allowed_values) - set(signal.labels)
        if undeclared:
            problems.append(f"{signal.key}: 声明了未使用的标签取值 {sorted(undeclared)}")
        if len(signal.labels) > 3:
            problems.append(f"{signal.key}: 标签数 {len(signal.labels)} 过多（基数是标签值的乘积）")
    return problems


def validate_label(metric_key: str, label_key: str, value: Any) -> str:
    """校验并返回标签值；**不合规就抛错，不"清洗后放行"**。

    两层判定（顺序即优先级）：

    1. **取值范围封闭**：该标签键必须声明有限取值集合，且值必须在其中。
       这是真正的防线——格式合法的 `sk-live-abcdef123456` 就是靠这一层被挡住的；
    2. **格式受控**：值还需匹配令牌格式，防止今后有人放宽集合时放进自由文本。

    刻意不做"删掉敏感字符"这种清洗：清洗会让非法输入静默变成另一个合法标签，
    从而污染指标口径。宁可拒绝。
    """
    signal = SIGNAL_BY_KEY.get(metric_key)
    if signal is None:
        raise ValueError(f"未登记的指标: {metric_key}")
    if label_key not in signal.labels:
        raise ValueError(f"{metric_key} 不允许标签键 {label_key}（§14 标签面受控）")

    allowed = signal.allowed_values.get(label_key)
    if not allowed:
        raise ValueError(
            f"{metric_key}.{label_key} 未声明封闭取值范围——"
            "给不出有限取值就说明该维度不该做标签（否则等于允许自由文本进标签）"
        )

    text = str(value)
    if text not in allowed:
        raise ValueError(
            f"标签值不在受控集合内: {metric_key}.{label_key}={text[:40]!r}；"
            f"允许: {allowed}"
        )
    if not _LABEL_VALUE_RE.match(text):
        raise ValueError(f"标签值格式不合法: {text[:40]!r}")
    return text


def unwired_signals() -> list[str]:
    """哪些指标当前**没有事实来源**（对应 journal 尚不存在）。

    这是"如实表达缺数据"的机制：与其输出一个 0 让人误以为"健康"，
    不如显式列出"这个指标还没有数据来源"。
    """
    missing: list[str] = []
    for signal in SIGNALS:
        if not signal.sources:
            missing.append(signal.key)
            continue
        if not any(journal.journal_path(name).exists() for name in signal.sources):
            missing.append(signal.key)
    return missing


def pending_backlog() -> int:
    """当前**待推进**的工作量。

    上游 §14 要求就绪状态包含积压。此前 `/health/*` 把这个值硬编码为 0——
    那等于**永远报告无积压**，即使系统里堆满了未收敛的工作。

    四个来源，各自取自已有的 append-only journal：

    1. **未终结任务**（`tasks.jsonl`）——受理了但还没完成；
    2. **未终结命令**（`commands.jsonl`）——提交了但还没到终态；
    3. **待决策批准**（`approvals.jsonl`）——`approved is None`；
    4. **inbox 未消费条目**（`inbox.jsonl`）。

    **踩过的坑**：第一版漏掉了"未终结任务"——任务写在**独立的 `tasks` journal**
    里，不在 `commands` 里，于是"受理了一堆任务"这种情况完全不计入积压。
    实测：建一个 ACCEPTED 任务后 backlog 仍然是 0。
    """
    settled_task_states = {"SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "REJECTED"}
    pending_tasks = sum(
        1
        for record in journal.read_all("tasks")
        if str(record.get("status")) not in settled_task_states
    )
    terminal_command_states = {"SUCCEEDED", "FAILED", "CANCELLED", "COMPENSATED", "NOT_EXECUTED"}
    pending_commands = sum(
        1
        for record in journal.read_all("commands")
        if str(record.get("status")) not in terminal_command_states
    )
    pending_approvals = sum(
        1 for record in journal.read_all("approvals") if record.get("approved") is None
    )
    inbox_backlog = len(journal.read_all("inbox"))
    return pending_tasks + pending_commands + pending_approvals + inbox_backlog



# ==========================================================================
# 派生
# ==========================================================================

def _records(*names: str) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        for record in journal.read_all(name):
            rows.append((name, record))
    return rows


def _ratio(numerator: int, denominator: int) -> float | None:
    """比率：分母为 0 时返回 None（**不是 0**）——"没样本"与"成功率 0%"含义完全不同。"""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _labelled(metric_key: str, pairs: dict[str, str]) -> dict[str, str]:
    """构造**经过校验**的标签集合。

    这是本模块的安全咽喉：所有标签都必须从这里出去。
    如果 `compute_metrics()` 直接拼标签字典，`validate_label` 就成了一把**没人用的锁**——
    实测正是如此：变异把封闭取值检查去掉，输出照样通过（因为输出里根本没有标签）。
    """
    return {label_key: validate_label(metric_key, label_key, value) for label_key, value in pairs.items()}


def _labelled_by_value(metric_key: str, label_key: str, observed: dict[str, int]) -> dict[str, Any]:
    """把**观测到的**取值映射成受控标签。

    对每个观测值都调用 `validate_label`：取值必须在封闭集合内。

    **不做"静默过滤"**——第一版把集合外的值直接丢掉，结果是
    "去掉封闭取值检查"这个变异**照样通过**（过滤条件本身也在用同一个集合）。
    现在的做法：集合外的值**显式失败**——它意味着代码产出了一个不该出现的维度值，
    那是缺陷，不是需要被悄悄掩盖的噪声。
    """
    result: dict[str, Any] = {}
    for value, count in observed.items():
        result[value] = {
            "count": count,
            "labels": _labelled(metric_key, {label_key: value}),
        }
    return result


# Guard 原因码 → 判定阶段名。
#
# **词表来自 `packages/domain/guard.py`**（上游 §15：纯领域约束放共享包）。
# 这里曾经手抄了一份 22 条映射，与 `services/_kernel/edge.py` 的实际产出之间
# **没有任何机制保证一致**——`edge.py` 新增一个原因码时，指标侧不会报错，
# 只会静默把它归到 `unknown` 桶里，于是"某个阶段的拒绝率"长期偏低而无人发现。
#
# `_REASON_TO_STAGE` 与 `STAGE_LABELS` 已在导入区按 domain 词表定义好。


def _stage_of(reason: str) -> str:
    """把 Guard 原因码映射回阶段名；未知原因码归入 `unknown`（不猜）。"""
    return _stage_of_reason(reason)


def unregistered_reason_codes_from_implementation() -> list[str]:
    """**实现里出现、但 domain 未登记**的原因码。

    这是"两份词表漂移"的机械探针：从 `edge.py` 源码里抓出所有
    `failures.append((stage, "CODE:..."))` / `failures.append((stage, "CODE"))`
    形状的原因码，逐个问 domain 认不认识。

    漏登记 ⇒ 报红，而不是静默进 `unknown` 桶。
    """
    import pathlib
    import re as _re

    from packages.domain.guard import unregistered_reason_codes

    source = pathlib.Path(__file__).with_name("edge.py").read_text(encoding="utf-8")
    codes = _re.findall(r'failures\.append\(\s*\(\s*"[a-z_]+"\s*,\s*f?"([A-Z][A-Z0-9_]*)', source)
    return unregistered_reason_codes(codes)


def compute_metrics() -> dict[str, Any]:
    """从 journal 派生全部指标。返回结构可直接序列化。

    所有标签都经 `_labelled()` 出口——**不存在绕过校验的输出路径**。
    """
    available: dict[str, Any] = {}

    # ── 任务（成功只认权威回执） ──
    tasks = journal.read_all("tasks")
    receipts = journal.read_all("receipts")
    task_total = len(tasks)
    succeeded = sum(1 for record in tasks if record.get("status") == "SUCCEEDED")
    accepted_only = sum(1 for record in tasks if record.get("status") == "ACCEPTED")
    available["task_success_total"] = {
        "by_label": {
            outcome: count
            for outcome, count in (
                ("succeeded", succeeded),
                ("accepted_only", accepted_only),
                ("failed", sum(1 for r in tasks if r.get("status") == "FAILED")),
                ("cancelled", sum(1 for r in tasks if r.get("status") == "CANCELLED")),
            )
            if count
        },
        # 标签出口：值必须落在封闭集合内，否则这里就抛错（不产出脏标签）
        "label_sample": _labelled("task_success_total", {"outcome": "succeeded"}),
        "total": task_total,
        "succeeded": succeeded,
        "accepted_only": accepted_only,
        "success_ratio": _ratio(succeeded, task_total),
        "receipts_recorded": len(receipts),
    }

    # ── 审批等待 ──
    waits: list[float] = []
    decisions: dict[str, int] = {}
    for record in journal.read_all("approvals"):
        decision = record.get("approved")
        bucket = {True: "granted", False: "denied", None: "pending"}.get(decision, "pending")
        decisions[bucket] = decisions.get(bucket, 0) + 1
        decided_at, created_at = record.get("decided_at"), record.get("valid_from") or record.get("created_at")
        if decided_at and created_at:
            try:
                import time as _time

                def _parse(value: str) -> float:
                    return _time.mktime(_time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - _time.timezone

                waits.append(max(0.0, _parse(decided_at) - _parse(created_at)))
            except ValueError:
                continue
    available["approval_wait_seconds"] = {
        "samples": len(waits),
        # 标签值来自**真实出现的决定结果**，逐个过封闭集合校验
        "by_decision": {
            bucket: {"count": decisions.get(bucket, 0),
                     "labels": _labelled("approval_wait_seconds", {"decision": bucket})}
            for bucket in ("granted", "denied", "pending")
        },
        "decisions": decisions,
        "max": round(max(waits), 3) if waits else None,
        "avg": round(sum(waits) / len(waits), 3) if waits else None,
    }

    # ── UNKNOWN 命令 ──
    commands = journal.read_all("commands")
    unknown = [record for record in commands if record.get("status") == "UNKNOWN"]
    convergence: dict[str, int] = {}
    for record in journal.read_all("reconciliations"):
        key = str(record.get("source_state") or record.get("outcome") or "unknown").lower()
        convergence[key] = convergence.get(key, 0) + 1
    available["unknown_command_total"] = {
        "total": len(unknown),
        "commands_total": len(commands),
        "convergence": convergence,
        "by_convergence": _labelled_by_value("unknown_command_total", "convergence", convergence),
    }

    # ── 补偿失败 ──
    compensation: dict[str, int] = {}
    for record in journal.read_all("sagas"):
        status = str(record.get("compensation_status") or record.get("status") or "unknown").lower()
        compensation[status] = compensation.get(status, 0) + 1
    for record in journal.read_all("compensations"):
        status = str(record.get("outcome") or record.get("status") or "unknown").lower()
        compensation[status] = compensation.get(status, 0) + 1
    failed = sum(count for status, count in compensation.items() if status in ("failed", "manual_review"))
    available["compensation_failure_total"] = {
        "total": failed,
        "by_status": compensation,
        "by_label": _labelled_by_value("compensation_failure_total", "outcome", compensation),
    }

    # ── 投影延迟 ──
    inbox = journal.read_all("inbox")
    outbox = journal.read_all("outbox")
    consumer_units: dict[str, int] = {}
    for record in inbox:
        unit = str(record.get("consumer_unit") or "unknown")
        consumer_units[unit] = consumer_units.get(unit, 0) + 1
    available["projection_lag_seconds"] = {
        "inbox_entries": len(inbox),
        "outbox_entries": len(outbox),
        "lag_samples": 0,
        "by_consumer_unit": _labelled_by_value("projection_lag_seconds", "consumer_unit", consumer_units),
        "note": "尚无投影延迟样本（需要 outbox 发布时刻与 inbox 接收时刻配对）",
    }

    # ── DLP 拒绝 ──
    egress = journal.read_all("egress-audit")
    rejections: dict[str, int] = {}
    for record in egress:
        if record.get("invocation_performed") is False:
            key = str(record.get("highest_data_class") or "unknown").lower()
            rejections[key] = rejections.get(key, 0) + 1
    available["dlp_rejection_total"] = {
        "total": sum(rejections.values()),
        "by_class": rejections,
        "by_label": _labelled_by_value("dlp_rejection_total", "reason", rejections),
        "audit_entries": len(egress),
    }

    # ── 模型调用（计数 / 治理判定耗时 / 成本） ──
    #
    # 语义必须分清：当前**没有代码真正调用模型**，`/internal/v1/model-invocations`
    # 做的是准入治理判定。因此：
    #   - 计数 = 受理次数（能如实反映"谁在被请求"）；
    #   - 耗时 = **治理判定路径**耗时（label/描述里都写明），**不是**生成耗时；
    #   - 成本 = 无报价数据 ⇒ 保持 None。
    # 把判定耗时冒充生成耗时，会让运维按错误的容量模型做决策。
    invocations = journal.read_all("model-invocations")
    backends: dict[str, int] = {}
    decision_samples: list[float] = []
    decision_by_backend: dict[str, list[float]] = {}
    for record in invocations:
        backend = str(record.get("model_backend") or "unknown")
        backends[backend] = backends.get(backend, 0) + 1
        duration = record.get("decision_seconds")
        if isinstance(duration, (int, float)):
            decision_samples.append(float(duration))
            decision_by_backend.setdefault(backend, []).append(float(duration))

    available["model_invocation_total"] = {
        "total": len(invocations),
        "by_backend": backends,
        "by_label": _labelled_by_value("model_invocation_total", "backend", backends),
        "generation_latency_samples": 0,
        "note": "受理次数；**无生成耗时样本**（没有代码真正调用模型）",
    }
    available["governance_decision_seconds"] = {
        "samples": len(decision_samples),
        "max": round(max(decision_samples), 6) if decision_samples else None,
        "avg": round(sum(decision_samples) / len(decision_samples), 6) if decision_samples else None,
        "by_backend_avg": {
            backend: round(sum(values) / len(values), 6)
            for backend, values in decision_by_backend.items()
        },
        "by_label": _labelled_by_value("governance_decision_seconds", "backend", backends),
        "note": "**这是治理判定路径耗时，不是模型生成耗时**",
    }
    available["model_cost_total"] = {
        "total": None,
        "by_label": _labelled_by_value("model_cost_total", "backend", backends),
        "note": "**待冻结**：计费口径未定（真实模型报价与计入方式），不得用占位数值冒充",
    }

    # ── Edge ──
    guard_decisions = journal.read_all("edge-guard-decisions")
    drift = 0
    rejected = 0
    by_stage: dict[str, int] = {}
    for record in guard_decisions:
        reasons = record.get("reasons") or []
        stages = {
            # 原因码形如 `DRIFT_EXCEEDED` 或 `MISSING_FIELD:x`，取"阶段名"前缀
            _stage_of(str(reason)) for reason in reasons if isinstance(reason, str)
        }
        for stage in stages:
            by_stage[stage] = by_stage.get(stage, 0) + 1
        if any("DRIFT" in str(reason) for reason in reasons):
            drift += 1
        if record.get("verdict") != "ALLOW":
            rejected += 1
    available["edge_drift_total"] = {
        "total": drift,
        "decisions": len(guard_decisions),
        "by_label": _labelled_by_value("edge_drift_total", "stage", by_stage),
    }
    available["edge_rejection_total"] = {
        "total": rejected,
        "decisions": len(guard_decisions),
        "by_stage": by_stage,
        "by_label": _labelled_by_value("edge_rejection_total", "stage", by_stage),
    }

    autonomy = journal.read_all("edge-autonomy-decisions")
    disconnection = {"allowed": 0, "blocked": 0}
    for record in autonomy:
        key = "allowed" if record.get("autonomy_allowed") else "blocked"
        disconnection[key] += 1
    available["edge_disconnection_total"] = {"decisions": len(autonomy), **disconnection}

    spool = journal.read_all("edge-spool")
    by_kind: dict[str, int] = {}
    for record in spool:
        kind = str(record.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    available["edge_spool_entries"] = {"total": len(spool), "by_kind": by_kind}

    return {
        "generated_at": _utc_now(),
        "labels_policy": "标签值为受控令牌；不含客户名称、Prompt 正文或密钥（§14）",
        "cost_unit": "待冻结",
        "backlog": pending_backlog(),
        "signals": available,
        "no_data": unwired_signals(),
    }


def _utc_now() -> str:
    import time as _time

    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def render_prometheus(metrics: dict[str, Any] | None = None) -> str:
    """渲染为 Prometheus 文本格式（受控标签；无数据显式为 NaN 注释而非 0）。"""
    data = metrics or compute_metrics()
    lines: list[str] = [
        "# V3 运维指标（从 journal 派生；标签为受控令牌，不含客户/Prompt/密钥）",
    ]
    for signal in SIGNALS:
        value = data["signals"].get(signal.key)
        lines.append(f"# HELP v3_{signal.key} {signal.description}")
        lines.append(f"# TYPE v3_{signal.key} gauge")
        if value is None:
            lines.append(f"# {signal.key}: NO_DATA")
            continue
        if "total" in value:
            # total 为 None 表示"口径未定/无数据"——**不得**渲染成 0
            if value["total"] is None:
                lines.append(f"# {signal.key}: NO_DATA（{value.get('note', '无数据')}）")
            else:
                lines.append(f'v3_{signal.key}{{scope="total"}} {value["total"]}')
        elif "success_ratio" in value:
            ratio = value["success_ratio"]
            if ratio is None:
                lines.append(f"# {signal.key}: NO_DATA（无样本，分母为 0）")
            else:
                lines.append(f'v3_{signal.key}{{scope="ratio"}} {ratio}')
        elif "samples" in value and value["samples"] == 0:
            lines.append(f"# {signal.key}: NO_DATA（无样本）")
        elif "samples" in value:
            lines.append(f'v3_{signal.key}{{scope="count"}} {value["samples"]}')
            if value.get("avg") is not None:
                lines.append(f'v3_{signal.key}{{scope="avg"}} {value["avg"]}')
        else:
            lines.append(f"# {signal.key}: {value}")
    for key in data["no_data"]:
        lines.append(f"# {key}: NO_DATA（尚无对应 journal 事实来源）")
    return "\n".join(lines) + "\n"


def metric_inventory() -> list[dict[str, str]]:
    """指标清单（供运维端点与门禁共同使用）。"""
    return [
        {
            "key": signal.key,
            "unit": signal.unit,
            "description": signal.description,
            "sources": ",".join(signal.sources) or "(none)",
            "labels": ",".join(signal.labels) or "(none)",
            "upstream_requirement": signal.upstream_requirement,
        }
        for signal in SIGNALS
    ]


def signals_missing_upstream_requirement() -> Iterable[str]:
    """§14 点名要求但未在本模块登记的指标项（应恒为空）。"""
    required = (
        "任务成功率",
        "审批等待",
        "UNKNOWN 命令",
        "补偿失败",
        "投影延迟",
        "DLP 拒绝",
        "模型耗时/成本",
        "Edge 漂移",
        "拒绝率",
        "断连",
        "spool 容量",
    )
    covered = {signal.upstream_requirement for signal in SIGNALS}
    return [item for item in required if item not in covered]
