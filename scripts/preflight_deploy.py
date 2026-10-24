"""上线前置检查：把"还差什么才能部署"变成**可机械判定的清单**。

## 为什么要这个

P5（受限生产）一直记着"阻塞在目标环境信息"。
但**"阻塞"不是一个可执行的结论** —— 它既不能验证、也无法知道进展。

本脚本把阻塞拆成两类：

| 类别 | 能否在仓库里判定 | 例子 |
| --- | --- | --- |
| **仓库内就绪度** | ✅ 能 | compose / manifest / 迁移 / 门禁证据 / 回滚与运维手册 |
| **目标环境事实** | ❌ 不能（必须由人提供） | 目标机器、生产库、停机窗口、凭据投放方式 |

于是它给出**两个互不混淆的输出**：
- `[就绪]` / `[缺失]`：仓库内该有的东西在不在（能当场修）；
- `[阻塞]`：必须在目标环境确认的事实，**逐条列名**，不假装能推断。

**关键设计**：`[阻塞]` 不是失败，而是**待办输入清单**。
退出码只在**仓库内缺失**时为 1 —— 因为那是我能修的部分。

## 用法

```
python scripts/preflight_deploy.py            # 人读
python scripts/preflight_deploy.py --json     # 机器读（可用于 CI / 部署前门禁）
```
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 仓库内**必须存在**的部署就绪件 → 说明。
REQUIRED_ARTIFACTS: dict[str, str] = {
    "infra/compose/docker-compose.dev.yml": "开发编排（8 个运行单元 + 三类网络域）",
    "infra/compose/Dockerfile.service": "统一下属镜像（非 root 运行）",
    "infra/deployment/manifest.yml": "机器可读部署清单（端口/环境变量/模块路径）",
    "infra/network/network-acl.yml": "网络域与出域规则（出域只从 security-gateway）",
    "services/_kernel/registry.mock.json": "Mock 登记表（生产登记表待冻结）",
    "docs/runbooks/01-本地起后台并查看.md": "本地起服务手册",
    "docs/runbooks/02-门禁红了怎么定位.md": "门禁红灯定位手册",
    "docs/runbooks/05-集群与端口规则.md": "集群与端口规则",
    "docs/v3-upgrade/evidence/gate-all.txt": "门禁一次性输出（阶段门禁证据）",
    "docs/v3-upgrade/evidence/mutation.txt": "变异测试输出（判据可信度证据）",
    "docs/v3-upgrade/evidence/A01-A14-断言覆盖报告.md": "A01–A14 覆盖报告（生成物）",
}

#: 目标环境必须提供的输入 → 为什么无法在仓库里判定。
ENVIRONMENT_INPUTS: list[tuple[str, str]] = [
    ("目标机器与网络位置", "决定部署形态（容器/裸进程）、网络域落地与防火墙策略"),
    ("生产数据库与账号", "决定迁移执行方式与备份策略；本仓运行期用 journal，但迁移链需真库"),
    ("域名与证书", "决定反向代理与暴露面；manifest 只声明端口，不含证书"),
    ("停机窗口与回滚时限", "决定上线方式（滚动/蓝绿）与回滚判据的严格程度"),
    ("凭据投放方式", "密钥只能从密钥管理/受限挂载注入，绝不进仓；投放路径必须由目标环境给出"),
    ("生产登记表（Release Registry）", "Mock 登记表含 tenants/tools/policies，生产登记表**待冻结**（见 manifest.frozen）"),
    ("监控与告警接收端", "决定 /health/* 与 /internal/v1/operations/metrics 的消费方"),
    ("现场验收人", "P3/P4 现场验收签署；无签署不得进入受限生产"),
]


def _check_artifacts() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    present: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for relative, note in REQUIRED_ARTIFACTS.items():
        path = REPO_ROOT / relative
        (present if path.exists() else missing).append((relative, note))
    return present, missing


def _strip_yaml_comment(value: str) -> str:
    """去掉行尾注释。

    本脚本刻意**零依赖**（不引 PyYAML），所以自己做最小解析：
    这一条是必须的 —— 清单里写的是
    `frozen: false          # 生产冻结状态：**未冻结**（…）`，
    不去注释就会把整段说明当成取值，于是误报「不是布尔字面量」。
    **实测踩过**：第一次运行就是被这行注释叫停的。
    """
    return value.split("#", 1)[0].strip()


def _check_manifest_frozen() -> dict:
    """清单里的 `frozen` 与 `blocked_by` 必须如实反映"未冻结"。"""
    path = REPO_ROOT / "infra/deployment/manifest.yml"
    if not path.exists():
        return {"present": False, "frozen": None, "blocked_by": None}
    text = path.read_text(encoding="utf-8")
    frozen = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("frozen:"):
            frozen = _strip_yaml_comment(stripped.split(":", 1)[1])
            break
    return {
        "present": True,
        "frozen": frozen,
        "blocked_by": "blocked_by:" in text,
        "declares_unfrozen": frozen in ("false", "False"),
    }


def collect() -> dict:
    present, missing = _check_artifacts()
    return {
        "artifacts_present": [item[0] for item in present],
        "artifacts_missing": [item[0] for item in missing],
        "manifest": _check_manifest_frozen(),
        "environment_inputs": [
            {"input": name, "why": why} for name, why in ENVIRONMENT_INPUTS
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="上线前置检查（仓库内就绪度 + 目标环境待办输入）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()

    result = collect()
    ready = not result["artifacts_missing"]

    if args.json:
        result["verdict"] = "ready_for_environment_inputs" if ready else "repo_incomplete"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ready else 1

    print("=" * 72)
    print("上线前置检查")
    print("=" * 72)
    print()
    print(f"[就绪] 仓库内部署就绪件 {len(result['artifacts_present'])} 项")
    for relative in result["artifacts_present"]:
        note = REQUIRED_ARTIFACTS[relative]
        print(f"    ✓ {relative}")
        print(f"      {note}")

    print()
    if result["artifacts_missing"]:
        print(f"[缺失] {len(result['artifacts_missing'])} 项（**这是我能修的部分**，退出码 1）")
        for relative in result["artifacts_missing"]:
            print(f"    ✗ {relative}")
            print(f"      {REQUIRED_ARTIFACTS[relative]}")
    else:
        print("[缺失] 无 —— 仓库侧就绪件齐备")

    manifest = result["manifest"]
    print()
    print("[清单] infra/deployment/manifest.yml")
    if not manifest["present"]:
        print("    ✗ 缺失")
    else:
        frozen = manifest["frozen"]
        print(f"    frozen = {frozen}（生产登记表未冻结时**必须**如实写 false）")
        if manifest["frozen"] not in ("false", "False", "true", "True"):
            print("    ✗ frozen 取值不是布尔字面量")
        if not manifest["blocked_by"]:
            print("    ✗ 未冻结却缺少 blocked_by —— 阻塞原因必须写下来")

    print()
    print("=" * 72)
    print(f"[阻塞] 目标环境必须提供的输入 {len(ENVIRONMENT_INPUTS)} 项")
    print("       **这些无法在仓库里判定**，只能由人确认；它们不是失败，是待办输入。")
    print("=" * 72)
    for index, (name, why) in enumerate(ENVIRONMENT_INPUTS, 1):
        print(f"  {index}. {name}")
        print(f"     为什么需要：{why}")

    print()
    if ready:
        print("结论：**仓库侧就绪**；部署阻塞已收敛为"
              f"{len(ENVIRONMENT_INPUTS)} 项目标环境输入（见上）。")
        return 0
    print(f"结论：**仓库侧不完整**（缺 {len(result['artifacts_missing'])} 项），"
          "先补这些再谈目标环境输入。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
