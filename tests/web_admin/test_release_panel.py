"""发布台账面板的守门行为（上游 §12 / §17 A09）。

## 这条套件防的是什么

控制台新增了"推进发布台账"的**写按钮**。七态状态机的规则是：

- **只能相邻推进一格**；
- 非终态可**直接撤销**到 `REVOKED`（撤销是"停止使用"，不该要求先推到某一步）；
- `REVOKED` 是终态，不能再出发。

而**请求体里的 `to` 是客户端可改的**。所以真正的安全边界必须在**后端**
（`edge.release_transition_allowed`）——本套件要证明的正是：

> **界面算错/请求被手改，都不会让非法转换通过。**

## 同时钉住两条恒定安全声明

`ai_new_actions_enabled` 恒 `false`、`control_write_disabled` 恒 `true` ——
上游把它们固定为 const，放开要等 P5 现场许可与互锁。
界面若把它们显示或传递成"已放开"，就是在**替上游承诺**它没承诺的事。
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


# ==========================================================================
# 自起控制台（**随机端口**，不依赖外部恰好有服务在 8300）
# ==========================================================================
#
# ## 为什么必须自起（一次真实误报）
#
# 最初这组用例直接打 `127.0.0.1:8300`，并用 `skipif(_admin_reachable())` 守卫。
# 那有**两个**问题：
#   1. 守卫在**导入时**判定，而门禁会先杀掉控制台 ⇒ 判定为"可达"、
#      实际跑的时候连不上 ⇒ 一堆 ConnectionRefused（**误报**）；
#   2. 依赖"外部恰好有人在 8300 跑着"，在干净环境里必然不可复现。
#
# 自起随机端口后：不依赖外部状态、不干扰用户在 8300 上看的那一份。

def _port_alive(port: int) -> bool:
    """集群是否在跑（决定要不要自起一套）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def admin_base():
    """起一个指向真实 dev 集群的控制台，返回 `http://127.0.0.1:<port>`。"""
    if NODE is None:
        pytest.skip("无 node：无法起控制台（明确 skip，**不算通过**）")

    ports = json.loads((REPO_ROOT / ".v3-dev/ports.json").read_text(encoding="utf-8"))
    env = dict(os.environ)
    env["V3_ADMIN_PORT"] = "0"  # 交给系统分配
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


def _request(base: str, path: str, payload: dict | None = None):
    url = f"{base}{path}"
    if payload is None:
        request = urllib.request.Request(url)
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _new_release() -> str:
    """造一条 DRAFT 发布台账（直接打 control-plane，与演示路径一致）。"""
    from tests.p0_runtime.conftest import Cluster, context_body
    import uuid

    cluster = Cluster(evidence_path=REPO_ROOT / ".v3-dev/evidence")
    ports = json.loads((REPO_ROOT / ".v3-dev/ports.json").read_text(encoding="utf-8"))
    if not _port_alive(ports["control-plane"]):
        cluster.start()
        ports = dict(cluster.ports)
    cluster.ports = ports
    cluster.port_overrides = {
        ("V3_PORT_" + key.upper().replace("-", "_")): str(value) for key, value in ports.items()
    }
    trace = f"trace-release-test-{uuid.uuid4().hex[:8]}"
    context = context_body(trace_id=trace)
    status, body = cluster.post(
        "control-plane",
        "/internal/v1/edge/releases",
        {
            **context,
            "context": context,
            "release_id": f"release-{uuid.uuid4().hex[:8]}",
            "edge_id": f"edge-{uuid.uuid4().hex[:8]}",
            "artifacts": [{"artifact_ref": "model-v1", "artifact_hash": "a" * 64}],
        },
        headers={"X-V3-Identity": "svc-hermes-runtime"},
    )
    assert status == 200, body
    return body["release_id"]


# ==========================================================================
# ① 恒定安全声明：界面不得把它们说成已放开
# ==========================================================================

def test_release_view_keeps_the_two_const_safety_declarations(admin_base):  # A09
    """`ai_new_actions_enabled` 恒 false、`control_write_disabled` 恒 true。

    而且**推进一步之后仍然是**——这正说明它们不随状态推进放开。
    """
    release_id = _new_release()

    status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert status == 200, view
    assert view["release"]["ai_new_actions_enabled"] is False, view
    assert view["release"]["control_write_disabled"] is True, view

    # 推进一步，恒定声明不得改变
    code, _moved = _request(
        admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "SCHEMA_FROZEN"}
    )
    assert code == 200, _moved
    status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert view["release"]["ai_new_actions_enabled"] is False, (
        f"推进状态后不得放开新动作：{view['release']}"
    )
    assert view["release"]["control_write_disabled"] is True, view["release"]


# ==========================================================================
# ② 只能相邻推进一格
# ==========================================================================

def test_adjacent_advance_is_allowed(admin_base):  # A09
    release_id = _new_release()
    status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert view["release"]["status"] == "DRAFT"
    assert view["legalNext"] == ["SCHEMA_FROZEN", "REVOKED"], view["legalNext"]

    code, moved = _request(
        admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "SCHEMA_FROZEN"}
    )
    assert code == 200, moved
    assert moved["ok"] is True
    assert moved["from"] == "DRAFT" and moved["to"] == "SCHEMA_FROZEN"


def test_skipping_a_state_is_rejected(admin_base):  # A09
    """**跳态必须被拒**——这是本套件最要紧的一条。

    即使请求里的 `to` 被手改成 `SHADOW`（跳过 `CALIBRATED`），
    后端也必须拒绝：`from` 由服务端取当前状态，不采信客户端。
    """
    release_id = _new_release()
    _request(admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "SCHEMA_FROZEN"})

    code, jumped = _request(
        admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "SHADOW"}
    )
    assert code == 409, jumped
    assert jumped["ok"] is False
    assert jumped["code"] == "EDGE_RELEASE_TRANSITION_NOT_ALLOWED"
    assert "合法下一步" in jumped["reason"], jumped

    # 状态没有被改动
    _status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert view["release"]["status"] == "SCHEMA_FROZEN", view["release"]


def test_multi_step_jump_from_draft_is_rejected(admin_base):  # A09
    """从 DRAFT 直接跳到 APPROVED 也不行。"""
    release_id = _new_release()
    code, jumped = _request(
        admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "APPROVED"}
    )
    assert code == 409, jumped
    assert jumped["code"] == "EDGE_RELEASE_TRANSITION_NOT_ALLOWED"


# ==========================================================================
# ③ 撤销：非终态可直接撤销；终态不能再出发
# ==========================================================================

def test_any_non_terminal_state_can_be_revoked_directly(admin_base):  # A09
    """撤销是"停止使用"，不该要求先把状态推进到某一步。"""
    release_id = _new_release()
    code, revoked = _request(
        admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "REVOKED"}
    )
    assert code == 200, revoked
    assert revoked["to"] == "REVOKED"

    _status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert view["isTerminal"] is True
    assert view["legalNext"] == [], "终态不得再有合法下一步"


def test_terminal_state_cannot_transition_again(admin_base):  # A09
    release_id = _new_release()
    _request(admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "REVOKED"})

    code, again = _request(
        admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "ACTIVE"}
    )
    assert code == 409, again
    assert again["code"] == "EDGE_RELEASE_TRANSITION_NOT_ALLOWED"


def test_revocation_invalidates_the_approval(admin_base):  # A09
    """撤销即失效：旧批准不再"当前"（上游 §12）。

    只用一条 DRAFT 台账验"撤销后 approval_current 不为真"；
    APPROVED 路径带批准引用需要批准备案，不在本套件范围内。
    """
    release_id = _new_release()
    _request(admin_base, "/api/v3/release-transition", {"release_id": release_id, "to": "REVOKED"})
    _status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert view["release"]["approval_current"] is False, view["release"]
    assert view["release"]["approval_ref"] is None, view["release"]


# ==========================================================================
# ④ 上下文：不得用假 task_id（一次真实缺陷）
# ==========================================================================

def test_release_read_uses_a_real_task_context(admin_base):  # A09
    """读取发布台账不得依赖硬编码的假 task_id。

    这里原本写死 `task_id: "task-demo"`——它不是真实任务，
    只是绕过了"工厂只读链路要求 task_id 非空"。演示环境每次重启都会产生
    新的 task_id，于是接口返回 400 而界面只看到"读不到"。
    """
    release_id = _new_release()
    status, view = _request(admin_base, f"/api/v3/release?release_id={release_id}")
    assert status == 200, (
        f"读取失败——上下文里的 task_id 可能不是真实任务：{view}"
    )
    assert "error" not in view, view
