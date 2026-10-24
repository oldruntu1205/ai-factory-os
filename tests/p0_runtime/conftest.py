"""P0 运行验收夹具：八个运行单元的真实进程。

设计取舍：
- 用**真实子进程**而不是进程内对象，这样"独立进程"是实测事实而非假设；
- 每个服务只暴露自己 owner 的路由，"不属于本服务的路径 404"可直接验证；
- 证据走同一个 JSONL，闭环追踪按 tenant/task/trace 串联；
- **端口自适应**：首选登记表端口；若该端口在本机不可 bind（Windows 上
  winnat/Hyper-V 保留端口会给出 WinError 10013），改用临时端口并通过
  `V3_PORT_<SERVICE>` 覆盖。跨服务调用因此需要同步覆盖对方端口，见 `apply_env`。

L1 的静态契约门禁（tests/contracts/）与本文件互不依赖：
- 前者只读契约文件，证明契约自洽；
- 本文件证明实现行为符合契约。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES_PKG = REPO_ROOT / "services"
KERNEL = SERVICES_PKG / "_kernel"

SERVICE_IDS = (
    "security-gateway",
    "control-plane",
    "decision-service",
    "hermes-runtime",
    "manufacturing-core",
    "tool-gateway",
    "transaction-service",
    "trajectory-eval",
)
SERVICE_MODULES = {service_id: f"services.{service_id}" for service_id in SERVICE_IDS}

# 启动顺序：先起被依赖的（独立无依赖的优先），减少就绪等待时间。
BOOT_ORDER = (
    "trajectory-eval",
    "security-gateway",
    "control-plane",
    "decision-service",
    "hermes-runtime",
    "manufacturing-core",
    "tool-gateway",
    "transaction-service",
)

_PORT_ENV_RE = re.compile(r"^V3_PORT_")


def venv_python() -> str:
    return os.environ.get("V3_PYTHON") or str(REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe")


def registry_document() -> dict[str, Any]:
    return json.loads((KERNEL / "registry.mock.json").read_text(encoding="utf-8"))


def service_ports() -> dict[str, int]:
    deployment = registry_document()["runtime_deployment"]
    return {entry["service_id"]: entry["port"] for entry in deployment["services"]}


def port_env_name(service_id: str) -> str:
    return "V3_PORT_" + service_id.upper().replace("-", "_")


def can_bind(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pick_port(preferred: int, *, exclude: set[int] | None = None) -> int:
    """首选端口可用就用它；不可用则在高位段找一个本机可绑定端口。

    为什么不用临时端口：Windows 上 `winnat`/Hyper-V 会保留一段 TCP 端口，
    落在保留区内的端口 `bind` 可能成功但随后连接被拒（表现为服务起了却连不上）。
    固定高位段 + 连续编号能大幅降低踩中的概率，配合 `ServiceProcess.start`
    的重试兜底。
    """
    taken = exclude or set()
    if preferred and preferred not in taken and can_bind(preferred):
        return preferred
    for offset in range(300):
        candidate = 18100 + offset
        if candidate in taken:
            continue
        if can_bind(candidate):
            return candidate
    raise AssertionError(f"无法为 {preferred} 找到可绑定端口")


def wait_port(port: int, *, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def http(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    token: bool = True,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer p0-test")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": raw.decode("utf-8", errors="replace")[:400]}


def apply_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境。

    继承当前进程里已有的 `V3_PORT_*` 覆盖（保证跨服务调用打到同一套端口），
    再叠加调用方显式给出的覆盖。
    """
    env = dict(os.environ)
    # 清理上一次运行可能残留的覆盖，避免串场；只保留本次显式传入的。
    for key in [key for key in env if _PORT_ENV_RE.match(key)]:
        if not (extra and key in extra):
            del env[key]
    env["V3_REGISTRY_PATH"] = str(KERNEL / "registry.mock.json")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


class ServiceProcess:
    """一个真实的服务子进程。"""

    def __init__(self, service_id: str, *, port: int | None = None, env_extra: dict[str, str] | None = None) -> None:
        self.service_id = service_id
        self.port = port or service_ports()[service_id]
        self.env_extra = dict(env_extra or {})
        self.process: subprocess.Popen[bytes] | None = None
        self.stdout_path = SERVICES_PKG / ".evidence" / f"stdout-{service_id}.log"
        self._log = None
        # 生效端口以 env 覆盖为准，避免与 Cluster 分配的端口不一致。
        override = self.env_extra.get(port_env_name(service_id))
        if override:
            self.port = int(override)

    def start(self, *, attempts: int = 3) -> None:
        """启动并等待监听；bind 静默失败时换端口重试（Windows 保留端口）。"""
        last_detail = ""
        for attempt in range(attempts):
            env = apply_env(self.env_extra)
            self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.stdout_path.open("wb")
            self.process = subprocess.Popen(
                [venv_python(), "-m", SERVICE_MODULES[self.service_id]],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=self._log,
                stderr=subprocess.STDOUT,
            )
            if wait_port(self.port, timeout=20.0):
                return
            last_detail = self.stdout_text()[-1200:]
            exited = self.process.poll() is not None
            self.stop()
            # 进程已退出说明不是端口问题（如依赖/配置错误），重试没有意义。
            if exited:
                break
            if attempt + 1 < attempts:
                self.port = pick_port(0, exclude={self.port})
                self.env_extra[port_env_name(self.service_id)] = str(self.port)
        raise AssertionError(
            f"{self.service_id} 未能监听端口 {self.port}\n{last_detail}"
        )

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log:
            self._log.close()
            self._log = None

    def stdout_text(self) -> str:
        if not self.stdout_path.exists():
            return ""
        return self.stdout_path.read_text(encoding="utf-8", errors="replace")


def kill_leftovers() -> None:
    """清理本项目遗留的服务进程（只匹配本仓库路径 + `-m services.*`）。

    为什么需要：子进程启动失败或测试进程被中断时，已起的兄弟进程会继续占端口。
    """
    if os.name != "nt":
        return
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + REPO_ROOT.name + "*' -and $_.CommandLine -like '*-m services.*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


class Cluster:
    """八个运行单元的集合，用于整体启动/停止与 HTTP 便捷调用。"""

    def __init__(
        self, *, evidence_path: pathlib.Path, registry_path: pathlib.Path | None = None
    ) -> None:
        self.evidence_path = evidence_path
        self.processes: dict[str, ServiceProcess] = {}
        self.ports: dict[str, int] = {}
        self.port_overrides: dict[str, str] = {}
        # 所有子进程共用同一个状态目录，跨进程 journal（命令/回执/任务/审计/Vault）才可见。
        self.state_dir = evidence_path.parent / "state"
        #: 可选的登记表覆盖。**默认 None ⇒ 行为与以前一字不差**
        #: （`apply_env` 会填默认路径）。用途见
        #: `tests/decision/test_adapter_switch_runtime.py`：
        #: 验证"换实现不改内核"时，需要让服务读另一份登记表。
        self.registry_path = registry_path

    def base_env(self) -> dict[str, str]:
        env = {"V3_STATE_DIR": str(self.state_dir), "V3_EVIDENCE_PATH": str(self.evidence_path)}
        if self.registry_path is not None:
            # `apply_env` 先填默认路径、最后 `update(extra)`，所以这里能覆盖它。
            env["V3_REGISTRY_PATH"] = str(self.registry_path)
        return env

    def start(self, service_ids: tuple[str, ...] = SERVICE_IDS) -> None:
        kill_leftovers()
        selected: dict[str, int] = {}
        for service_id in SERVICE_IDS:
            selected[service_id] = pick_port(service_ports()[service_id], exclude=set(selected.values()))
        self.ports = selected
        self.port_overrides = {port_env_name(service_id): str(port) for service_id, port in selected.items()}
        env_extra = {**self.port_overrides, **self.base_env()}
        try:
            for service_id in BOOT_ORDER:
                if service_id not in service_ids:
                    continue
                process = ServiceProcess(service_id, port=selected[service_id], env_extra=env_extra)
                process.start()
                # start() 可能因保留端口换过端口，回写实际值保持跨服务调用一致。
                selected[service_id] = process.port
                env_extra[port_env_name(service_id)] = str(process.port)
                self.processes[service_id] = process
            self.wait_ready()
        except BaseException:
            # 启动半途失败时也必须清干净，否则残留进程会占住端口影响下一次运行。
            self.stop()
            raise

    def restart(self, service_id: str) -> None:
        """重启单个运行单元，**保持端口与状态目录不变**。

        用途：验证"重启恢复"这类断言（上游 P3 完成条件之一）。
        端口必须沿用原值：新端口会让其它服务登记表里的地址失效，
        变成"改了环境"而不是"重启了服务"，断言就失去意义。
        """
        existing = self.processes.get(service_id)
        if existing is None:
            raise KeyError(f"服务未在运行: {service_id}")
        port = self.ports[service_id]
        existing.stop()
        env_extra = {**self.port_overrides, **self.base_env()}
        process = ServiceProcess(service_id, port=port, env_extra=env_extra)
        process.start()
        self.ports[service_id] = process.port
        self.processes[service_id] = process
        self.wait_ready()

    def spawn_extra(self, service_id: str, *, env_extra: dict[str, str] | None = None) -> ServiceProcess:
        """再起一个同类服务实例（用于故障注入与并行验证）。

        继承已就绪集群的端口映射，保证它调用的兄弟服务与主集群一致。
        """
        port = pick_port(0, exclude=set(self.ports.values()))
        merged = {
            **self.port_overrides,
            **self.base_env(),
            port_env_name(service_id): str(port),
        }
        merged.update(env_extra or {})
        process = ServiceProcess(service_id, port=port, env_extra=merged)
        process.start()
        return process

    def write_registry_variant(self, name: str, mutate) -> pathlib.Path:
        """写一份登记表变体（用于故障注入）。

        关键：把本集群实际使用的端口写进变体，否则变体里的 `runtime_deployment`
        会指向未监听的端口，造成与注入目标无关的失败。

        文件名包含进程号与序号：同一测试进程里多次生成同名变体会**复用同一文件**，
        而后启动的服务读到的可能是上一份内容（实测过的假阴性来源）。
        """
        document = registry_document()
        for entry in document["runtime_deployment"]["services"]:
            actual = self.ports.get(entry["service_id"])
            if actual:
                entry["port"] = actual
        document = mutate(document)
        self._variant_seq = getattr(self, "_variant_seq", 0) + 1
        path = SERVICES_PKG / ".evidence" / f"registry-{name}-{os.getpid()}-{self._variant_seq}.json"
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def wait_ready(self, *, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        pending = set(self.processes)
        while time.time() < deadline:
            for service_id in list(pending):
                status, payload = self.get(service_id, "/health/ready")
                if status == 200 and payload.get("ready") is True:
                    pending.discard(service_id)
            if not pending:
                return
            time.sleep(0.4)
        raise AssertionError(f"以下服务未就绪: {sorted(pending)}")

    def stop(self) -> None:
        for process in reversed(list(self.processes.values())):
            process.stop()
        self.processes.clear()

    # ---- HTTP ------------------------------------------------------------

    def url(self, service_id: str, path: str) -> str:
        return f"http://127.0.0.1:{self.ports[service_id]}{path}"

    def get(self, service_id: str, path: str, **kwargs) -> tuple[int, dict[str, Any]]:
        return http("GET", self.url(service_id, path), **kwargs)

    def post(self, service_id: str, path: str, body: dict[str, Any], **kwargs) -> tuple[int, dict[str, Any]]:
        return http("POST", self.url(service_id, path), body=body, **kwargs)

    # ---- 证据 ------------------------------------------------------------

    def evidence(self) -> list[dict[str, Any]]:
        if not self.evidence_path.exists():
            return []
        records = []
        for line in self.evidence_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


# --------------------------------------------------------------------------
# 请求构造（占位值全部用 contract examples 的风格，不含真实凭据）
# --------------------------------------------------------------------------

def context_body(
    *,
    tenant: str = "tenant-demo",
    actor: str = "user-demo",
    employee: str | None = "employee-planning",
    task_id: str | None = "task-demo",
    trace_id: str = "trace-demo",
    data_class: str = "C2",
    schema_version: str = "1.0.0",
    deadline: str = "2026-10-05T00:00:00Z",
) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "factory_id": "factory-demo",
        "actor_id": actor,
        "employee_id": employee,
        "task_id": task_id,
        "trace_id": trace_id,
        "data_class": data_class,
        "schema_version": schema_version,
        "deadline_at": deadline,
    }


def schema_binding() -> dict[str, Any]:
    return {"schema_ref": "command.json", "schema_version": "1.0.0", "schema_hash": "sha256:mock"}


def tool_request_body(
    *,
    tenant: str = "tenant-demo",
    idempotency_key: str = "tenant-demo:task-demo:publish:1",
    tool_id: str = "pmc.dispatch.publish",
    expected_version: int = 6,
    data_class: str = "C2",
    trace_id: str = "trace-demo",
    task_id: str | None = "task-demo",
    object_id: str = "schedule-demo",
) -> dict[str, Any]:
    return {
        **context_body(tenant=tenant, trace_id=trace_id, data_class=data_class, task_id=task_id),
        "tool_id": tool_id,
        "tool_version": "1.0.0-mock",
        "input_ref": object_id,
        "input_schema": schema_binding(),
        "idempotency_key": idempotency_key,
        "expected_version": expected_version,
    }


def cleanup_pycache() -> None:
    """清理子进程留下的 __pycache__，避免污染工作区统计。"""
    for root, dirs, _ in os.walk(SERVICES_PKG):
        for name in list(dirs):
            if name == "__pycache__":
                path = pathlib.Path(root) / name
                for child in path.glob("*"):
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    path.rmdir()
                except OSError:
                    pass
                dirs.remove(name)


# --------------------------------------------------------------------------
# pytest 夹具
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def evidence_path() -> pathlib.Path:
    """会话级证据与状态文件。每轮验收从零开始，避免上一轮记录造成假串通。"""
    path = SERVICES_PKG / ".evidence" / "p0-runtime.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    # 清掉上一轮的 command/receipt 日志：否则幂等用例会命中旧命令。
    for name in ("commands", "receipts", "tasks", "approvals", "runs"):
        stale = SERVICES_PKG / ".evidence" / "state" / f"{name}.jsonl"
        if stale.exists():
            stale.unlink()
    return path


@pytest.fixture(scope="session")
def cluster(evidence_path: pathlib.Path):
    instance = Cluster(evidence_path=evidence_path)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()
        kill_leftovers()
        cleanup_pycache()
