"""生成 `.v3-dev/user-facing-ids.json`：用户侧四页可直接粘贴的 id。

## 为什么单独一个文件（不写进 demo.json）

`tools/v3_dev_cluster.py demo` 会**整份覆盖** `.v3-dev/demo.json`，
于是之前造好的 `release_id` 会被冲掉——"复制下来去页面粘贴"这件事
就变得不可靠：刚拿到的 id，跑一次 demo 就失效了。

所以用户侧需要的 id 单独放一份，**由 `demo` 与用户侧脚本都能追加**，
而不是共用一个会被覆盖的文件。
"""

from __future__ import annotations

import json
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.p0_runtime.conftest import Cluster, context_body  # noqa: E402

IDS_PATH = ROOT / ".v3-dev" / "user-facing-ids.json"


def main() -> int:
    cluster = Cluster(evidence_path=ROOT / ".v3-dev/evidence")
    ports = json.loads((ROOT / ".v3-dev/ports.json").read_text(encoding="utf-8"))
    cluster.ports = ports
    cluster.port_overrides = {
        ("V3_PORT_" + key.upper().replace("-", "_")): str(value) for key, value in ports.items()
    }

    trace = f"trace-user-ids-{uuid.uuid4().hex[:8]}"
    context = context_body(trace_id=trace)

    release_id = f"release-demo-{uuid.uuid4().hex[:6]}"
    status, body = cluster.post(
        "control-plane",
        "/internal/v1/edge/releases",
        {
            **context,
            "context": context,
            "release_id": release_id,
            "edge_id": f"edge-demo-{uuid.uuid4().hex[:6]}",
            "artifacts": [{"artifact_ref": "model-v1", "artifact_hash": "a" * 64}],
        },
        headers={"X-V3-Identity": "svc-hermes-runtime"},
    )
    print("发布台账:", status, body.get("status"))

    # 任务与命令来自 demo.json（那个文件由 v3_dev_cluster 维护）
    demo = {}
    demo_path = ROOT / ".v3-dev" / "demo.json"
    if demo_path.is_file():
        demo = json.loads(demo_path.read_text(encoding="utf-8"))

    payload = {
        "_comment": "用户侧四页可直接粘贴的 id。本文件**不会被 v3_dev_cluster demo 覆盖**。",
        "task_id": demo.get("task_id"),
        "command_id": demo.get("command_id"),
        "approval_id": demo.get("approval_id"),
        "release_id": body.get("release_id", release_id),
    }
    IDS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写入", IDS_PATH.relative_to(ROOT))
    for key, value in payload.items():
        if not key.startswith("_"):
            print(f"  {key} = {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
