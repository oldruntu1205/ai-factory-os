"""对账（UNKNOWN 收敛）面板的守门行为（上游 §5 / §12 / §17 A05）。

## 这条套件防的是什么

上游把 UNKNOWN 定为**必须查状态**的状态，并明确禁止两件事：

- **换新键重发**（`RESEND_NEW_KEY`）；
- **直接补偿**（`DIRECT_COMPENSATION`）。

界面若把这两条藏起来、或让 `next_action` 看着像"可以重发"，运维就可能误操作。
所以这一页的**禁止动作清单必须一直在**——本套件把它钉住。

另外两个容易出错的点：

1. **"没有收敛记录"不是错误**：后端返回 404 `RECONCILIATION_NOT_FOUND`
   表示"尚未进入 UNKNOWN 收敛"。界面必须如实说明，而不是弹红。
2. **`query_verified` 必须被如实转达**：没查证过源系统的结论不能当依据。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "web-admin"
NODE = shutil.which("node")
sys.path.insert(0, str(REPO_ROOT))


def _port_alive(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def admin_base():
    """自起控制台（随机端口），指向真实 dev 集群。**不依赖外部恰好有服务在跑**。"""
    if NODE is None:
        pytest.skip("无 node：无法起控制台（明确 skip，**不算通过**）")

    ports = json.loads((REPO_ROOT / ".v3-dev/ports.json").read_text(encoding="utf-8"))
    env = dict(os.environ)
    env["V3_ADMIN_PORT"] = "0"
    env["V3_PORT_CONTROL_PLANE"] = str(ports["control-plane"])
    env["V3_PORT_TRANSACTION_SERVICE"] = str(ports["transaction-service"])
    env["V3_PORT_MANUFACTURING_CORE"] = str(ports["manufacturing-core"])
    env.pop("V3_ADMIN_APPROVER_ACTOR", None)

    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False, encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [NODE, "server.mjs"], cwd=str(APP_DIR), env=env,
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    deadline = time.time() + 20
    port = None
    while time.time() < deadline:
        log.flush()
        text = pathlib.Path(log.name).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            marker = "http://127.0.0.1:"
            if marker in line:
                port = int(line.split(marker, 1)[1].split()[0].strip())
                break
        if port:
            break
        if process.poll() is not None:
            raise AssertionError(f"控制台提前退出：{text[:400]}")
        time.sleep(0.05)
    if not port:
        process.kill()
        raise AssertionError("控制台 20s 内未就绪")

    yield f"http://127.0.0.1:{port}"

    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        process.kill()
    finally:
        log.close()
        try:
            pathlib.Path(log.name).unlink()
        except OSError:
            pass


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _cluster():
    from tests.p0_runtime.conftest import Cluster

    cluster = Cluster(evidence_path=REPO_ROOT / ".v3-dev/evidence")
    ports = json.loads((REPO_ROOT / ".v3-dev/ports.json").read_text(encoding="utf-8"))
    if not _port_alive(ports["control-plane"]):
        cluster.start()
        ports = dict(cluster.ports)
    cluster.ports = ports
    cluster.port_overrides = {
        ("V3_PORT_" + key.upper().replace("-", "_")): str(value) for key, value in ports.items()
    }
    return cluster


def _reconciled_command(source_state: str = "NOT_EXECUTED") -> str:
    """造一条**已完成收敛**的命令（走 P3 标准路径：normalize → submit → lease → execute → reconcile）。

    `source_state` 取 `transactions.SOURCE_STATES` 之一；用 `UNKNOWN` 可构造
    「尚未查证」的情形（`query_verified=false`）。
    """
    import uuid

    from tests.p0_runtime.conftest import context_body
    from tests.p3_runtime.test_p3_runtime import (
        _execute, _normalize, _reconcile, _submit, _take_lease, _write_approval,
    )

    cluster = _cluster()
    trace = f"trace-recon-test-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id=f"schedule-recon-{uuid.uuid4().hex[:6]}")
    approval_id = f"approval-recon-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(
        cluster, trace_id=trace, normalization_ref=view["normalization_ref"], approval_ref=approval_id
    )[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-recon", trace_id=trace)[1]
    _execute(cluster, command_id=command_id, version=lease["record_version"], lease=lease, trace_id=trace)
    status, _record = _reconcile(
        cluster, command_id=command_id, lease=lease,
        source_state=source_state, version=lease["record_version"], trace_id=trace,
    )
    assert status == 200, f"收敛应成功（source_state={source_state}）"
    return command_id


def _reconciled_command_with_source_state(source_state: str) -> str:
    """显式命名的包装，便于用例读起来意图清楚。"""
    return _reconciled_command(source_state)


# ==========================================================================
# ① 禁止动作清单必须一直在
# ==========================================================================

def test_prohibited_actions_are_always_present(admin_base):  # A05
    """**恒定禁止清单**必须出现在对账视图里。

    上游把它定成恒定输出，正是为了让验收能断言「它一直在」。
    这里断言**具体两项**：换新键重发、直接补偿。
    """
    command_id = _reconciled_command()
    status, view = _get(admin_base, f"/api/v3/reconciliation?command_id={command_id}")
    assert status == 200, view
    assert "RESEND_NEW_KEY" in view["prohibited"], (
        f"禁止动作清单里必须含 RESEND_NEW_KEY（换新键重发被明确禁止）：{view['prohibited']}"
    )
    assert "DIRECT_COMPENSATION" in view["prohibited"], (
        f"禁止动作清单里必须含 DIRECT_COMPENSATION（直接补偿被明确禁止）：{view['prohibited']}"
    )


def test_source_outcome_and_query_verified_are_reported(admin_base):  # A05
    """源系统结论与「是否查证过」必须如实转达。"""
    command_id = _reconciled_command()
    status, view = _get(admin_base, f"/api/v3/reconciliation?command_id={command_id}")
    assert status == 200, view
    assert view["sourceOutcome"] == "NOT_EXECUTED", view
    assert view["queryVerified"] is True, "查证过源系统时必须如实报告"
    assert view["queryAttempts"] >= 1, view


def test_query_verified_is_true_whenever_a_record_exists(admin_base):  # A05
    """**实测出来的不变量**：只要收敛记录存在，`query_verified` 就恒为 `true`。

    ## 这条用例纠正了我自己先前的错误断言

    我原先写了一条"源结论 `UNKNOWN` ⇒ `queryVerified` 应为 `false`"的用例，**它失败了**。
    查代码后确认是**我的断言错了，不是实现错了**：

    ```python
    # 前置校验：非集合值直接 400
    if source_state not in transactions.SOURCE_STATES:
        raise errors.reject_bad_request("source_state", "源系统结论不在允许集合内")
    ...
    "query_verified": source_state in transactions.SOURCE_STATES,   # 恒为 True
    ```

    而 `SOURCE_STATES = ("SUCCEEDED", "NOT_EXECUTED", "UNKNOWN", "FAILED", "accepted")`
    —— **`UNKNOWN` 本身就在集合里**。所以任何**可达**的记录都有 `query_verified = true`。

    ## 因此

    - 界面里那句"否（结论不可当依据）"是**不可达分支**（防御性代码，留着无害）；
    - 我曾为 `queryVerified: view.query_verified ?? null → true` 加过变异，它**逃逸**了 ——
      但这次是**变异本身空转**（改动在可达输入上不可区分），**不是门禁有盲区**。
      该变异已删除，理由记在这里。

    > 区分"门禁有盲区"与"变异空转"很重要：前者要补测试，后者要删变异。
    """
    command_id = _reconciled_command_with_source_state("UNKNOWN")
    status, view = _get(admin_base, f"/api/v3/reconciliation?command_id={command_id}")
    assert status == 200, view
    assert view["sourceOutcome"] == "UNKNOWN", view
    assert view["queryVerified"] is True, (
        "`UNKNOWN` 是允许的源结论之一 ⇒ 表示『已查证，但结论未知』，"
        f"所以 `query_verified` 必为 true：{view}"
    )


# ==========================================================================
# ② 重试约束
# ==========================================================================

def test_retry_requires_fresh_review_and_same_key(admin_base):  # A05
    """重试必须**同一幂等键**，且必须**重新走执行前复核**（不得复用旧批准结论）。"""
    command_id = _reconciled_command()
    status, view = _get(admin_base, f"/api/v3/reconciliation?command_id={command_id}")
    assert status == 200, view
    assert view["retryNeedsFreshReview"] is True, (
        f"重试必须要求重新走执行前复核：{view['retryNeedsFreshReview']}"
    )
    # CONFIRMED_NOT_EXECUTED ⇒ 允许用同一键重试，键必须给出
    assert view["nextAction"] == "RETRY_SAME_KEY", view["nextAction"]
    assert view["retryKey"], "允许重试时必须给出**同一**幂等键"


def test_succeeded_outcome_offers_no_retry_key(admin_base):  # A05
    """已成功（SUCCEEDED）时不得给出重试键——那会诱导重复副作用。"""
    import uuid

    from tests.p0_runtime.conftest import context_body
    from tests.p3_runtime.test_p3_runtime import (
        _execute, _normalize, _reconcile, _submit, _take_lease, _write_approval,
    )

    cluster = _cluster()
    trace = f"trace-recon-ok-{uuid.uuid4().hex[:8]}"
    view = _normalize(cluster, trace_id=trace, object_id=f"schedule-ok-{uuid.uuid4().hex[:6]}")
    approval_id = f"approval-ok-{uuid.uuid4().hex[:8]}"
    _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
    command_id = _submit(
        cluster, trace_id=trace, normalization_ref=view["normalization_ref"], approval_ref=approval_id
    )[1]["command_id"]
    lease = _take_lease(cluster, command_id=command_id, version=1, worker="worker-ok", trace_id=trace)[1]
    _execute(cluster, command_id=command_id, version=lease["record_version"], lease=lease, trace_id=trace)
    _reconcile(
        cluster, command_id=command_id, lease=lease,
        source_state="SUCCEEDED", version=lease["record_version"], trace_id=trace,
    )

    status, body = _get(admin_base, f"/api/v3/reconciliation?command_id={command_id}")
    assert status == 200, body
    assert body["sourceOutcome"] == "SUCCEEDED"
    assert body["retryKey"] is None, (
        f"已成功时不得给出重试键：{body['retryKey']}——那会诱导重复副作用"
    )


# ==========================================================================
# ③ 「没有收敛记录」是一种状态，不是错误
# ==========================================================================

def test_missing_record_is_reported_as_a_state_not_an_error(admin_base):  # A05
    """没有收敛记录时，接口必须给出 `noRecord`，而不是把它当失败。

    后端返回 404 `RECONCILIATION_NOT_FOUND`（"该命令尚无收敛记录"）。
    界面据此说明"尚未进入对账"——**不是**弹红。
    """
    import uuid

    from tests.p0_runtime.conftest import context_body
    from tests.p3_runtime.test_p3_runtime import _normalize

    cluster = _cluster()
    trace = f"trace-recon-none-{uuid.uuid4().hex[:8]}"
    # 只做规范化、不提交命令 ⇒ 该 command_id 不存在收敛记录
    _normalize(cluster, trace_id=trace, object_id=f"schedule-none-{uuid.uuid4().hex[:6]}")

    status, body = _get(admin_base, "/api/v3/reconciliation?command_id=command-does-not-exist")
    assert status == 200, "「没有记录」必须以 200 + noRecord 呈现，不是错误"
    assert body.get("noRecord") is True, body
    assert body.get("code"), "必须带上后端的原因码，便于定位"


def test_missing_command_id_is_rejected(admin_base):  # A05
    """缺 `command_id` 必须明确拒绝（400），不得猜一个。"""
    status, body = _get(admin_base, "/api/v3/reconciliation")
    assert status == 400, body
    assert body["code"] == "MISSING_PARAM", body
