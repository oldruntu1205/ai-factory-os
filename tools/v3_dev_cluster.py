"""开发用 V3 集群：在**标准端口**上起全部 8 个服务单元（上游 §15 本地验证）。

## 与测试夹具的区别

测试用 `tests/p0_runtime/conftest.Cluster` 会挑**随机空闲端口**（多个测试进程互不干扰）。
但要让浏览器里的管理后台连上，端口必须**可预期**——否则每跑一次 UI 都要改配置。

所以这里是一个"固定端口 + 可执行"的启动器，供本地人工验收使用。
它**不参与门禁**：门禁仍走随机端口，两者互不影响。

用法：
    python tools/v3_dev_cluster.py start      # 起全部单元（后台）
    python tools/v3_dev_cluster.py status     # 看谁活着
    python tools/v3_dev_cluster.py stop       # 停掉
    python tools/v3_dev_cluster.py demo       # 造一份演示数据（含权威回执）
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
KERNEL = REPO_ROOT / "services" / "_kernel"
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
STATE_DIR = REPO_ROOT / ".v3-dev"

SERVICE_MODULES = {
    "security-gateway": "services.security-gateway",
    "control-plane": "services.control-plane",
    "decision-service": "services.decision-service",
    "hermes-runtime": "services.hermes-runtime",
    "manufacturing-core": "services.manufacturing-core",
    "tool-gateway": "services.tool-gateway",
    "transaction-service": "services.transaction-service",
    "trajectory-eval": "services.trajectory-eval",
}

SERVICE_PORTS = {
    "security-gateway": 8101,
    "control-plane": 8102,
    "decision-service": 8103,
    "hermes-runtime": 8104,
    "manufacturing-core": 8105,
    "tool-gateway": 8106,
    "transaction-service": 8107,
    "trajectory-eval": 8108,
}


def port_env_name(service_id: str) -> str:
    return "V3_PORT_" + service_id.upper().replace("-", "_")


def python_executable() -> str:
    """优先用仓库 venv；没有就退回当前解释器。"""
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    return sys.executable


def base_env() -> dict[str, str]:
    env = dict(os.environ)
    # 清掉端口覆盖，保证用的是本文件的固定端口
    for key in [k for k in env if k.startswith("V3_PORT_")]:
        del env[key]
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env["V3_REGISTRY_PATH"] = str(KERNEL / "registry.mock.json")
    # 固定状态目录：让演示数据在重启后仍在
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env["V3_STATE_DIR"] = str(STATE_DIR / "state")
    env["V3_EVIDENCE_DIR"] = str(STATE_DIR / "evidence")
    return env


def pid_file(service_id: str) -> pathlib.Path:
    return STATE_DIR / f"{service_id}.pid"


def alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1.5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def ports_file() -> pathlib.Path:
    return STATE_DIR / "ports.json"


def write_ports() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ports_file().write_text(
        json.dumps(SERVICE_PORTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cmd_start() -> int:
    env = base_env()
    started: list[str] = []
    for service_id, module in SERVICE_MODULES.items():
        port = SERVICE_PORTS[service_id]
        if alive(port):
            print(f"  已在运行：{service_id}:{port}")
            continue
        child_env = dict(env)
        child_env[port_env_name(service_id)] = str(port)
        log = (STATE_DIR / f"{service_id}.log").open("ab")
        # `CREATE_NEW_PROCESS_GROUP` + 分离：父进程退出后子进程继续服务，
        # 这样"启动脚本跑完就返回"不会把集群带走。
        process = subprocess.Popen(
            [python_executable(), "-m", module],
            cwd=str(REPO_ROOT),
            env=child_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        pid_file(service_id).write_text(str(process.pid), encoding="utf-8")
        started.append(service_id)
        print(f"  启动：{service_id}:{port} (pid {process.pid})")

    if started:
        print("等待就绪…")
        for _ in range(40):
            if all(alive(SERVICE_PORTS[s]) for s in started):
                break
            time.sleep(0.5)
    write_ports()
    return cmd_status()


def cmd_status() -> int:
    ready = 0
    for service_id, port in SERVICE_PORTS.items():
        ok = alive(port)
        ready += ok
        print(f"  {'●' if ok else '○'} {service_id:<22} {port}")
    print(f"\n{ready}/{len(SERVICE_PORTS)} 个单元就绪")
    if ready < len(SERVICE_PORTS):
        print(f"日志在 {STATE_DIR}")
    return 0 if ready == len(SERVICE_PORTS) else 1


def cmd_stop() -> int:
    stopped = 0
    for service_id in SERVICE_MODULES:
        path = pid_file(service_id)
        if not path.exists():
            continue
        pid = path.read_text(encoding="utf-8").strip()
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=15)
            stopped += 1
        except (subprocess.SubprocessError, OSError):
            pass
        path.unlink(missing_ok=True)
    print(f"已停止 {stopped} 个单元")
    return 0


def cmd_demo() -> int:
    """造演示数据：一条走完整链路的命令，带**真实权威回执**。

    用测试模块里已验证过的 helper（`_normalize` / `_write_approval` / `_submit` /
    `_take_lease` / `_execute` / `_reconcile`），不复制一份编排逻辑——
    复制出来的第二份迟早与真实链路分叉，那样演示的就是假东西。

    ## 一个必须知道的事实（联调实测）

    **`posting_status` 在当前实现里恒为 `NOT_CONFIRMED`**：
    全仓 5 处回执构造（routes.py 的 1624 / 2240 / 2560 / 2930 等）都写死它，
    `posting_evidence_ref` 也恒为 `None`。所以**今天任何输入都产生不出"已过账"**。

    这不是缺陷：真实过账凭证（ERP 的过账确认）是上游明列的外部依赖，尚未接入。
    界面对一条刚跑完的命令显示「未过账」是**正确**且刻意的——
    它正是上游 §6「防止 UI 显示成功而 ERP 未过账」要的效果。
    """
    sys.path.insert(0, str(REPO_ROOT))
    # helper 住在测试模块里；这里只读复用，不改测试。
    from tests.p3_runtime.test_p3_runtime import (  # noqa: PLC0415
        _execute,
        _normalize,
        _reconcile,
        _submit,
        _take_lease,
        _write_approval,
    )
    from tests.p0_runtime.conftest import Cluster, context_body  # noqa: PLC0415
    import uuid

    cluster = Cluster(evidence_path=STATE_DIR / "evidence")

    # **不要调 `cluster.start()`**：它内部会 `kill_leftovers()`，把"所有
    # `-m services.*` 进程"全局杀掉——那会顺手杀了我们自己起的 dev 集群，
    # 然后改用随机端口，于是后台连不上、演示也没了对象。
    #
    # 所以：如果 dev 集群已经在跑，就**把端口直接填进夹具对象**（只连不起）；
    # 只有它没跑时才由夹具自己起一套（此时随机端口无所谓，因为不面向后台）。
    if all(alive(port) for port in SERVICE_PORTS.values()):
        cluster.ports = dict(SERVICE_PORTS)
        cluster.port_overrides = {
            port_env_name(service_id): str(port) for service_id, port in SERVICE_PORTS.items()
        }
        print("  复用已在运行的 dev 集群（标准端口）")
    else:
        print("  dev 集群未运行，由夹具临时启动（随机端口）")
        cluster.start()
    try:
        created: dict[str, str] = {}
        trace = f"trace-demo-{uuid.uuid4().hex[:8]}"

        # **先造一条真实任务**：P1 Context 的"工厂只读链路"要求 `task_id` 非空
        # （见 `_p3_get_context`）。控制台读命令 / 批准 / 发布都需要它。
        #
        # 必须用**真实任务**，不能让控制台硬编码一个看着合理的值 ——
        # 演示环境每次重启都会产生新的 task_id，假值会让读取接口返回 400，
        # 而界面只看到"读不到"（2026-10-05 实测踩过）。
        demo_context = context_body(trace_id=trace)
        task_status, task_body = cluster.post(
            "control-plane",
            "/api/v1/tasks",
            {
                **demo_context,
                "context": demo_context,
                "idempotency_key": f"demo-task-{uuid.uuid4().hex[:12]}",
                "input_ref": "schedule-input-demo",
                "input_schema": {"type": "object"},
            },
            headers={"X-V3-Identity": "svc-hermes-runtime"},
        )
        if task_status == 202 and task_body.get("task_id"):
            created["task_id"] = task_body["task_id"]
            print(f"  演示任务：{task_body['task_id']}")
        else:
            print(f"  [警告] 演示任务创建失败：{task_status} {task_body}")

        view = _normalize(cluster, trace_id=trace, object_id="schedule-demo")
        approval_id = f"approval-demo-{uuid.uuid4().hex[:8]}"
        _write_approval(cluster, approval_id=approval_id, binding=view["binding"])
        created["approval_id"] = approval_id

        command_id = _submit(
            cluster,
            trace_id=trace,
            normalization_ref=view["normalization_ref"],
            approval_ref=approval_id,
        )[1]["command_id"]
        created["command_id"] = command_id

        lease = _take_lease(
            cluster, command_id=command_id, version=1, worker="worker-demo", trace_id=trace
        )[1]
        execute_status, execute_body = _execute(
            cluster, command_id=command_id, version=lease["record_version"],
            lease=lease, trace_id=trace,
        )
        print(f"  执行受理：HTTP {execute_status}")

        # 用**标准收敛路径**确认执行成功并落权威回执
        # （这是测试里 `test_reconcile_confirmed_success_records_receipt_and_succeeds` 用的那条）。
        status, record = _reconcile(
            cluster,
            command_id=command_id,
            lease=lease,
            source_state="SUCCEEDED",
            version=lease["record_version"],
            trace_id=trace,
        )
        if status == 200:
            print(f"  权威回执已落：business_success={record.get('business_success')}"
                  f"  posting_status={record.get('posting_status')}")
            print(f"  authority_verified="
                  f"{(record.get('recorded_receipt') or {}).get('authority_verified')}")
        else:
            print(f"  [警告] 收敛未成功：{status} {record}")

        (STATE_DIR / "demo.json").write_text(
            json.dumps(created, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n演示数据已写入 {STATE_DIR / 'demo.json'}")
        print("在控制台「命令与权威回执」里粘贴上面的 command_id：")
        print("  —— 该命令**已有权威回执**（authority_verified=true、business_success=true），")
        print("     但 posting_status 仍是 NOT_CONFIRMED ⇒ 界面显示「未过账」。")
        print("  这正是上游 §6 要防的那件事：**编排成功 ≠ 账务已过账**。")
        return 0
    finally:
        # 只清理**本次自己起的**进程；复用已有集群时不能把它带走。
        for process in cluster.processes.values():
            process.stop()




def main() -> int:
    parser = argparse.ArgumentParser(description="开发用 V3 集群")
    parser.add_argument("action", choices=["start", "status", "stop", "demo"])
    args = parser.parse_args()
    return {"start": cmd_start, "status": cmd_status, "stop": cmd_stop, "demo": cmd_demo}[
        args.action
    ]()


if __name__ == "__main__":
    raise SystemExit(main())
