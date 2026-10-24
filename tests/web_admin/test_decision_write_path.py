"""控制台**批准决定写路径**的门禁（上游 §6 / §7 / §17 A14）。

## 这条套件防的是什么

界面能"做决定"意味着它拿到了**写权限**。而本项目已把两条判死：

1. **授权必须记名**（不能是"某个服务顺手批的"）；
2. **不把自然语言"批准"当作授权**。

所以这里逐条钉住：未配置决定人 ⇒ 拒绝写；说明里写「同意/批准」⇒ 拒绝；
决定时刻用**配置的那个身份**提交；后端拒绝**不得谎报成功**。

## ⚠️ 为什么是 Python 而不是 `node --test`

我最初把它写成 `apps/web-admin/test/decision.test.mjs`，结果**整个套件挂死**
（不是失败，是永不返回）。根因是**嵌套 `node --test`**：

- 门禁走 `tests/web_admin/test_status_contract.py` → `node --test`（父）；
- 父用**管道** spawn 每个测试文件作为子进程；
- 我的用例内部又 `spawn` 管理后台并把它的 stdout 用管道读回端口号。

父与子互相等管道 flush ⇒ 死锁。这与本机沙箱对**管道捕获**的限制同源
（"children cannot open named pipes"这条边界的实际表现）。

修法不是绕过沙箱，而是**去掉这层嵌套**：门禁本来就跑 Python 包装层，
所以直接在这里用 Python 起进程、**stdout 走临时文件**（不用管道读端口），
行为完全一样，且不再有死锁面。

## 为什么用假后端

这几条全是**管理后台自己的守门行为**（拒绝什么、以谁的身份提交、怎么透出拒绝）。
真集群会把这些性质埋在"集群恰好可用"的依赖里，而且**不能稳定构造"后端拒绝"这一支**。
假后端只做一件事：把收到的请求原样记下，并按要求拒绝或接受。
真实端到端已另行手工验证（见交付文档）。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "web-admin"
SERVER = APP_DIR / "server.mjs"

NODE = shutil.which("node")

VALID_BINDING = {
    "object_id": "schedule-1",
    "expected_version": 1,
    "tool_id": "pmc.dispatch.publish",
    "tool_version": "1.0.0-mock",
    "command_type": "pmc.dispatch.publish",
    "action_scope": ["pmc.dispatch.publish"],
    "payload_ref": "schedule-1",
    "payload_hash": "a" * 64,
    "payload_schema": {"schema_ref": "task.json", "schema_version": "1.0.0", "schema_hash": "sha256:mock"},
    "normalization_ref": "norm-1",
    "policy_version": "policy-v1",
}


# ==========================================================================
# 假后端：记录收到的请求，按要求接受或拒绝
# ==========================================================================

class _FakeBackend:
    def __init__(self, behave: str = "ok") -> None:
        self.behave = behave
        self.received: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _make_handler(self):
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # 静音
                return

            def _read_body(self) -> object:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw.decode("utf-8")) if raw else None
                except Exception:
                    return None

            def do_GET(self) -> None:  # noqa: N802
                recorder.received.append({"method": "GET", "url": self.path, "body": None})
                self._respond(200, {})

            def do_POST(self) -> None:  # noqa: N802
                body = self._read_body()
                recorder.received.append({"method": "POST", "url": self.path, "body": body})
                if recorder.behave == "binding_changed" and "/decisions" in self.path:
                    self._respond(
                        409,
                        {
                            "code": "APPROVAL_BINDING_CHANGED",
                            "message": "批准已绑定其他 payload/版本，需重新审批",
                            "retryable": False,
                        },
                    )
                    return
                self._respond(200, {"approval_id": "approval-x", "approved": (body or {}).get("approved")})

            def _respond(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def decisions(self) -> list[dict]:
        """只看**真实写请求**。

        后台起服务后会对后端做健康探测（`GET /health/live` × 3），那不是"写"。
        实测踩过：把 `received` 整体当"有没有写"，断言会被这三次探测污染。
        """
        return [r for r in self.received if "/decisions" in r["url"]]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ==========================================================================
# 起管理后台：stdout 走**临时文件**（不用管道，避开嵌套/管道死锁）
# ==========================================================================

class _Admin:
    def __init__(self, *, approver_actor: str | None, backend_port: int) -> None:
        env = dict(os.environ)
        env["V3_ADMIN_PORT"] = "0"  # 交给系统分配，避免与开发中的 8300 冲突
        env["V3_PORT_CONTROL_PLANE"] = str(backend_port)
        env["V3_PORT_TRANSACTION_SERVICE"] = str(backend_port)
        env["V3_PORT_MANUFACTURING_CORE"] = str(backend_port)
        if approver_actor:
            env["V3_ADMIN_APPROVER_ACTOR"] = approver_actor
        else:
            env.pop("V3_ADMIN_APPROVER_ACTOR", None)  # **确保默认关闭**

        self._log = tempfile.NamedTemporaryFile(
            "w+", suffix=".log", delete=False, encoding="utf-8", errors="replace"
        )
        self.proc = subprocess.Popen(
            [NODE, str(SERVER)], cwd=str(APP_DIR), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,  # 都进文件，不占管道
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.port = self._await_port()

    def _await_port(self) -> int:
        deadline = time.time() + 20
        while time.time() < deadline:
            self._log.flush()
            text = pathlib.Path(self._log.name).read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                marker = "http://127.0.0.1:"
                if marker in line:
                    try:
                        return int(line.split(marker, 1)[1].split()[0].strip())
                    except ValueError:
                        pass
            if self.proc.poll() is not None:
                raise AssertionError(f"管理后台提前退出：{text[:400]}")
            time.sleep(0.05)
        raise AssertionError(
            f"管理后台 20s 内未就绪；日志：{pathlib.Path(self._log.name).read_text(encoding='utf-8', errors='replace')[:400]}"
        )

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            self._log.close()
            try:
                pathlib.Path(self._log.name).unlink()
            except OSError:
                pass


@pytest.fixture
def admin_factory():
    """按需起后台；收尾统一清理，不留孤儿进程。"""
    started: list[_Admin] = []
    backends: list[_FakeBackend] = []

    def make(*, approver_actor: str | None = None, behave: str = "ok"):
        backend = _FakeBackend(behave)
        backends.append(backend)
        admin = _Admin(approver_actor=approver_actor, backend_port=backend.port)
        started.append(admin)
        return admin, backend

    yield make

    for admin in started:
        admin.close()
    for backend in backends:
        backend.close()


pytestmark = pytest.mark.skipif(NODE is None, reason="无 node：无法起管理后台（明确 skip，**不算通过**）")


# ==========================================================================
# ① 未配置决定人 ⇒ 拒绝写
# ==========================================================================

def test_write_path_is_closed_without_a_configured_approver(admin_factory):  # A14
    """未配置决定人时**拒绝写**，且不得静默借用别的身份。"""
    admin, backend = admin_factory(approver_actor=None)

    status, health = admin.get("/api/v3/health")
    assert status == 200, health
    assert health["admin"]["writeEnabled"] is False, "未配置决定人时必须报 writeEnabled=false"
    assert health["admin"]["approverActor"] is None, "不得凭空给出一个决定人"

    status, body = admin.post(
        "/api/v3/approval-decision",
        {"approval_id": "approval-x", "approved": True, "binding": VALID_BINDING},
    )
    assert status == 403, body
    assert body["code"] == "APPROVER_NOT_CONFIGURED"
    assert body.get("how"), "拒绝时必须告诉使用者怎么开启"
    # **关键**：被拦在本地 ⇒ 后端不得收到任何**写**请求（不是"写了被后端拒"）
    assert backend.decisions() == [], "写路径关闭时不得向后端发出任何写请求"


# ==========================================================================
# ② 自然语言"批准"不构成授权
# ==========================================================================

@pytest.mark.parametrize("comment", ["同意", "批准执行", "我同意这个方案", "OK to approve"])
def test_natural_language_approval_is_not_authorization(admin_factory, comment):  # A14
    """说明文字里的「同意/批准」不构成授权（上游明文）。"""
    admin, backend = admin_factory(approver_actor="user-approver")

    status, body = admin.post(
        "/api/v3/approval-decision",
        {"approval_id": "approval-x", "approved": True, "comment": comment, "binding": VALID_BINDING},
    )
    assert status == 400, f"「{comment}」应被拒绝，实际 {status} {body}"
    assert body["code"] == "NATURAL_LANGUAGE_APPROVAL_REJECTED"
    assert backend.decisions() == [], "被本地判据拦下的请求不得发往后端"


# ==========================================================================
# ③ 决定以**配置的身份**记名提交
# ==========================================================================

def test_decision_is_submitted_under_the_configured_named_actor(admin_factory):  # A14
    """决定必须**记名**：后端收到的 `context.actor_id` 就是配置的决定人。"""
    admin, backend = admin_factory(approver_actor="user-approver")

    status, body = admin.post(
        "/api/v3/approval-decision",
        {"approval_id": "approval-1", "approved": True, "comment": "已核对排程版本",
         "binding": VALID_BINDING},
    )
    assert status == 200, body
    assert body["ok"] is True
    assert body["actorId"] == "user-approver", "响应必须回报实际决定人（界面要能显示是谁批的）"

    decisions = backend.decisions()
    assert len(decisions) == 1, f"必须恰好发出一次写请求：{backend.received}"
    call = decisions[0]
    assert call["method"] == "POST"
    # 记名：context.actor_id 就是配置的决定人，而不是服务身份
    assert call["body"]["context"]["actor_id"] == "user-approver"
    assert call["body"]["approved"] is True
    # 绑定逐项透传，没有被后台悄悄替换
    assert call["body"]["payload_hash"] == VALID_BINDING["payload_hash"]
    assert call["body"]["object_id"] == VALID_BINDING["object_id"]


# ==========================================================================
# ④ 后端拒绝 ⇒ 不得谎报成功
# ==========================================================================

def test_backend_rejection_is_not_reported_as_success(admin_factory):  # A14
    """后端拒绝时必须透出可定位错误，**不得**报成功。"""
    admin, _backend = admin_factory(approver_actor="user-approver", behave="binding_changed")

    status, body = admin.post(
        "/api/v3/approval-decision",
        {"approval_id": "approval-1", "approved": True, "binding": VALID_BINDING},
    )
    assert status == 409, body
    assert body["ok"] is False, "后端拒绝时绝不能报 ok"
    assert body["approved"] is None, "被拒绝时不得声称已批准"
    assert body["code"] == "APPROVAL_BINDING_CHANGED"
    assert body.get("detail"), "必须透出后端原始响应以便定位"


# ==========================================================================
# ⑤ 绑定缺失/残缺 ⇒ 本地拒绝，不替调用方猜
# ==========================================================================

def test_binding_must_be_supplied_and_complete(admin_factory):  # A14
    """绑定缺失或残缺时本地拒绝——**不替调用方猜一份绑定**。

    平台没有「读取待决批准绑定」的接口（`/fact` 对未决定的批准返回
    `409 APPROVAL_NOT_DECIDED` 且不含绑定），所以绑定只能由知情操作方提供。
    """
    admin, backend = admin_factory(approver_actor="user-approver")

    status, body = admin.post("/api/v3/approval-decision", {"approval_id": "approval-1", "approved": True})
    assert status == 400
    assert body["code"] == "BINDING_REQUIRED"
    assert isinstance(body.get("required"), list) and body["required"], "必须列出必需字段"

    status, body = admin.post(
        "/api/v3/approval-decision",
        {"approval_id": "approval-1", "approved": True, "binding": {"object_id": "schedule-1"}},
    )
    assert status == 400
    assert body["code"] == "BINDING_INCOMPLETE"

    assert backend.decisions() == [], "本地被拒的请求不得发往后端"
