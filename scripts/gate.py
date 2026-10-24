#!/usr/bin/env python
"""阶段门禁（P0 / P1 …）：契约静态门禁 + 运行验收 + V2.1 回归。

判据唯一：**本脚本 exit 0**。任何一项失败即 exit 1。

用法（从仓库根运行）：
    backend\\.venv\\Scripts\\python.exe scripts\\gate.py --stages p0
    backend\\.venv\\Scripts\\python.exe scripts\\gate.py --stages p0,p1
    backend\\.venv\\Scripts\\python.exe scripts\\gate.py --stages p0,p1 --evidence docs\\v3-upgrade\\evidence\\gate.txt

设计取舍：
- **阶段可分离**：P0 门禁只看 P0 契约集合（阶段分域），新增 P1 契约不会污染它；
  反之亦然。混合阶段时按 p0 → p1 顺序执行，任一失败即整体失败。
- 不引入新依赖（标准库 + 已装 pytest），避免"门禁本身成为新风险"。
- 逐项打印命令、耗时与退出码，便于把输出原样贴进送审包。
- 进程与端口收尾由 tests/*_runtime 的夹具负责（含残留进程清理）。

兼容：`scripts/gate_p0.py` 保留为 `--stages p0` 的等价入口。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 阶段 → 步骤列表。步骤 = (名称, argv, 说明)
STAGE_STEPS: dict[str, list[tuple[str, list[str], str]]] = {
    "p0": [
        (
            "P0 契约静态门禁",
            ["-m", "pytest", "tests/contracts/test_p0_contracts.py", "-q", "--no-header"],
            "只读 P0 契约集合：接口目录、引用闭合、状态机、枚举封闭性、fail-closed 声明",
        ),
        (
            "契约元模式校验",
            ["-m", "pytest", "tests/contracts/test_contract_meta_validation.py", "-q", "--no-header"],
            "用 $schema 声明的元模式校验契约本身；拒绝 $ref 同级关键字这类静默失效",
        ),
        (
            "P0 运行验收",
            ["-m", "pytest", "tests/p0_runtime", "-q", "--no-header"],
            "八个真实进程：可启动、拒绝未知 tenant/工具、Mock 闭环可追踪、敏感数据不出域",
        ),
        (
            "V2.1 回归基线",
            ["backend/tests/test_hermes_mcp_scheduler_audit.py"],
            "既有在产能力的 64 项脚本式用例，防止 V3.0 改动破坏旧系统",
        ),
        (
            "跨租户隔离（§15 tests/isolation）",
            ["-m", "pytest", "tests/isolation", "-q", "--no-header"],
            "A01：别租户的 task 读不到（且先证明本租户读得到，避免假绿）；"
            "不存在的 command 在两个租户下回答同构（不泄露存在性）；未登记租户在控制面与决策面都 fail-closed",
        ),
    ],
    "p1": [
        (
            "P1 契约静态门禁",
            ["-m", "pytest", "tests/contracts/test_p1_contracts.py", "-q", "--no-header"],
            "P1 新增 6 接口 + 分级/治理/审计/Vault/来源契约；并锁定 P0 原字节 SHA-256",
        ),
        (
            "P1 运行验收",
            ["-m", "pytest", "tests/p1_runtime", "-q", "--no-header"],
            "C3/C4 无出域路径、跨租户检索隔离、只读问答来源可追溯、Vault 授权恢复",
        ),
    ],
    "p15": [
        (
            "P1.5 契约静态门禁",
            ["-m", "pytest", "tests/contracts/test_p15_contracts.py", "-q", "--no-header"],
            "P1.5 新增 6 接口 + outbox/inbox/dlq/receipt/lifecycle 契约；锁定 P0+P1 原字节",
        ),
        (
            "事件一致性语义",
            ["-m", "pytest", "tests/p15_events", "-q", "--no-header"],
            "outbox 原子提交、inbox 去重与版本收敛、迟到保护、DLQ 重放不走旁路",
        ),
        (
            "P1.5 运行验收",
            ["-m", "pytest", "tests/p15_runtime", "-q", "--no-header"],
            "跨 owner 权威读、事件投递去重/缺版本/迟到、DLQ 处置、权威回执驱动成功",
        ),
    ],
    "p2": [
        (
            "存储所有权与隔离",
            ["-m", "pytest", "tests/p2_storage", "-q", "--no-header"],
            "§13 硬不变量：每 owner 独立 schema/role、无跨 owner 授权与外键、tenant 进隔离域、RLS FORCE",
        ),
        (
            "P2 迁移台账与冻结",
            ["-m", "pytest", "tests/p2_migrations", "-q", "--no-header"],
            "每 owner 独立迁移 + 内容哈希冻结（防手改已发布迁移）、owner 目录边界、租户表强制 RLS 落在 SQL 文本上、执行器（顺序/幂等/失败即停）",
        ),
        (
            "运维指标（§14）",
            ["-m", "pytest", "tests/p2_observability", "-q", "--no-header"],
            "§14 点名指标全部有定义；标签值取封闭集合（客户名称/Prompt/密钥在格式与取值上双重进不来）；缺数据如实标 NO_DATA 不用 0 冒充",
        ),
        (
            "P2 运行期验收",
            ["-m", "pytest", "tests/p2_runtime", "-q", "--no-header"],
            "真实进程：请求必须落指标事实（否则指标永远空）、运维端点可读出、就绪 backlog 反映真实积压、缺必填字段报 400 而非 503",
        ),
        (
            "外部系统接入层（§15）",
            ["-m", "pytest", "tests/p2_integrations", "-q", "--no-header"],
            "ERP/PMC/MES/WMS 原生记录 → V3 事件：产出的信封必须通过 kernel 契约校验；缺字段/单位不符/时区不明一律拒绝而非编造；版本原样取自权威系统",
        ),
        (
            "共享领域包（§15）",
            ["-m", "pytest", "tests/domain", "-q", "--no-header"],
            "纯领域约束：阶段名与契约逐字对齐、阶段表由原因码派生、观测侧与现场侧共用同一份词表（漂移探针）、本包不依赖文件/网络/服务",
        ),
        (
            "Jev 适配层（§3/§13）",
            ["-m", "pytest", "tests/decision", "-q", "--no-header"],
            "决策类型与上游 §3 的封闭清单逐字一致且三处同源（契约/登记表/实现）；8 种类型逐个有明确归宿"
            "（实现或显式拒绝，不许静默返回）；内核不再残留决策逻辑；适配器解析对每种坏输入 fail-closed"
            "（不退回 mock）；无校准时置信度为 null 且 Mock 不落在自动候选区间（>0.92）",
        ),
        (
            "基础设施清单（§15）",
            ["-m", "pytest", "tests/infra", "-q", "--no-header"],
            "部署清单与代码不漂移（服务 id/owner/端口/端口变量名逐项核对）；编排与清单一致；凭据只允许变量引用且默认值为空；内部接口不得从对外域可达",
        ),
        (
            "管理后台状态契约（§6/A14）",
            [
                "-m", "pytest", "tests/web_admin", "-q", "--no-header",
            ],
            "后台显示契约：只有权威回执能说已过账；编排成功但无回执不得显示成功；UNKNOWN 单独显示需对账；结论必带来源与更新时间；汇总与逐行同源",
        ),
        (
            "端到端路由面（§15 tests/e2e）",
            ["-m", "pytest", "tests/e2e", "-q", "--no-header"],
            "每条注册路由都真的可达（不得 404/方法不匹配）；探针自身覆盖全部注册路由；影子报告读写往返一致、缺失明确 404、跨租户不可见",
        ),
    ],
    "p3": [
        (
            "P3 契约静态门禁",
            ["-m", "pytest", "tests/contracts/test_p3_contracts.py", "-q", "--no-header"],
            "P3 新增 17 接口 + 批准/命令/执行/UNKNOWN/Saga/取消/准入契约；锁定 P0+P1+P1.5 原字节",
        ),
        (
            "受控写入语义",
            ["-m", "pytest", "tests/p3_transactions", "-q", "--no-header"],
            "批准绑定与 TOCTOU、UNKNOWN 收敛（禁换键/禁直接补偿）、Saga 实物先于账务、取消、fencing、无人值守准入",
        ),
        (
            "P3 运行验收",
            ["-m", "pytest", "tests/p3_runtime", "-q", "--no-header"],
            "受控规范化与绑定哈希（可信服务计算、可重算）、只读/未知工具拒绝、无人值守写入准入",
        ),
        (
            "事务边界（§15 tests/transactions）",
            ["-m", "pytest", "tests/transactions", "-q", "--no-header"],
            "A05/A06 纯函数判据：基线放行（其余拒绝才可归因）、旧审批/payload 变更/权限撤销/对象版本变动、"
            "风险等级不得自报、R2 无状态查询不得无人值守写入、已过账须分支处置；"
            "UNKNOWN 收敛遍历全部源状态：换新键重发与直接补偿恒定禁止、动作恒在白名单、两表不相交、词表以契约为准",
        ),
    ],
    "p4": [
        (
            "P4 契约静态门禁",
            ["-m", "pytest", "tests/contracts/test_p4_contracts.py", "-q", "--no-header"],
            "P4 新增 17 接口 + 七态发布/冻结门槛/Guard 判定顺序/spool/自治/逐设备回退契约；锁定既有契约原字节",
        ),
        (
            "Edge 现场判定语义",
            ["-m", "pytest", "tests/p4_edge", "-q", "--no-header"],
            "Guard 判定优先级（置信度最后、硬互锁不可被批准覆盖）、发布状态机与行为性变化重开门禁、spool 去重/TTL/只补报事实、断连自治、逐设备回退、影子禁写",
        ),
        (
            "P4 运行验收",
            ["-m", "pytest", "tests/p4_runtime", "-q", "--no-header"],
            "真实进程：七态相邻推进、行为性变化使旧批准失效、影子推理与 Guard **永不产生动作**、spool 只补报事实、断连自治 fail-closed；并自检契约路径可达性",
        ),
    ],
    "assertions": [
        (
            "A01–A14 断言覆盖门禁",
            ["-m", "pytest", "tests/assertions", "-q", "--no-header"],
            "每条上游断言必须指向真实存在的测试；关键套件每个用例必须声明所支撑的断言；partial/未覆盖必须写明缺口",
        ),
    ],
    # ── 前端静态检查 ────────────────────────────────────────────────
    #
    # 2026-10-06 新增。缘由是一次真事故：我们在 `V3AIEmployees.jsx` 里用了
    # `plain()` 却忘了 import，页面直接崩（plain is not defined），
    # **而 `vite build` 是通过的**——Vite 只转译、不解析标识符，
    # 所以"构建通过 + dev server 返回 200"这类验证**从原理上查不出**这类错。
    #
    # 结论：把这条检查放进门禁。它必须能**负向验证**（故意删掉 import 要能报出来），
    # 否则"报 0 个问题"可能只是检查器坏了——本脚本第一版正是如此
    # （括号不配对时把整个函数体当成参数列表，于是所有名字都"已定义"）。
    "web": [
        (
            "前端 JS/JSX 未定义调用检查",
            ["tools/check_jsx_undefined.py"],
            "JSX 里调用了但没定义也没导入的标识符——`vite build` 查不出这类错（它只转译不解析标识符），"
            "而它会让页面整页崩掉。先剥字符串与注释，再收集定义/解构/函数参数/import，报出其余；"
            "对项目自身工具函数（v3Terms 等）额外标注「未导入」",
        ),
        (
            "BFF 判定与回执取值守卫",
            ["prefix:node", "--test", "cwd:apps/web-admin"],
            "BFF 的判定逻辑与字段取值：结论推导（只有权威回执能说已过账）、批准可决定性、"
            "以及**回执必须从 `authority_receipt` 取**——读错键不会报错，只会让「已过账」永远显示不出来"
            "（实测踩过：命令已 SUCCEEDED 且 posting_status=POSTED，任务结论页仍报「未过账」）",
        ),
    ],
}


def _unreferenced_contract_tests() -> list[str]:
    """找出 `tests/contracts` 下**没有被任何步骤引用**的测试文件。

    为什么需要它：步骤里写死文件名的做法有个致命盲区——
    新增一个契约测试文件但忘了加进步骤，它就**永远不会执行**。
    实测代价：`test_contract_inventory_gates.py` 写好后长期没跑，
    等到跑全量才发现它一直红着（28 项失败），而门禁始终"全绿"。

    因此这里反向扫描：目录里有、步骤里没有 → 必须补跑。
    """
    referenced: set[str] = set()
    for steps in STAGE_STEPS.values():
        for _name, argv, _note in steps:
            for token in argv:
                if token.startswith("tests/"):
                    referenced.add(token.rstrip("/"))

    directory = REPO_ROOT / "tests" / "contracts"
    if not directory.exists():
        return []

    missing: list[str] = []
    for path in sorted(directory.glob("test_*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative not in referenced and relative not in {
            "tests/contracts/test_contract_meta_validation.py",
        }:
            missing.append(relative)
    return missing


# 契约目录里未被显式引用的测试，统一在 assertions 阶段补跑
STAGE_STEPS["assertions"].extend(
    (
        f"契约未引用测试：{path}",
        ["-m", "pytest", path, "-q", "--no-header"],
        "反向覆盖：目录里存在但未被任何步骤引用的契约测试，防止「写了却永不执行」",
    )
    for path in _unreferenced_contract_tests()
)


def _unreferenced_test_files() -> list[str]:
    """找出 `tests/` 下**所有套件**里没有被任何步骤引用的测试文件。

    上一版只查了 `tests/contracts`；后来发现新阶段的业务套件同样可能"写了不跑"
    （例如 P4 的 `tests/p4_edge` 在加进步骤之前不会被执行）。
    这里把检查面扩到整个 `tests/`，并在 assertions 阶段统一补跑。

    判定口径：步骤里出现 `tests/<dir>`（目录）或 `tests/<dir>/<file>.py`（单文件）
    都算引用；两者皆无 → 必须补跑。
    """
    referenced: set[str] = set()
    for steps in STAGE_STEPS.values():
        for _name, argv, _note in steps:
            for token in argv:
                if token.startswith("tests/"):
                    referenced.add(token.rstrip("/"))

    tests_root = REPO_ROOT / "tests"
    missing: list[str] = []
    for path in sorted(tests_root.rglob("test_*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        directory = path.parent.relative_to(REPO_ROOT).as_posix()
        if relative in referenced or directory in referenced:
            continue
        missing.append(relative)
    return missing


# 反向覆盖：把"没被任何步骤引用"的测试文件补进 assertions 阶段
_already_added = {
    argv[0] for _name, argv, _note in STAGE_STEPS["assertions"] if argv and argv[0].startswith("tests/")
}
STAGE_STEPS["assertions"].extend(
    (
        f"未引用测试：{path}",
        ["-m", "pytest", path, "-q", "--no-header"],
        "反向覆盖：该文件存在但未被任何步骤引用；不补跑就永远不会执行",
    )
    for path in _unreferenced_test_files()
    if path not in _already_added
)


def venv_python() -> str:
    explicit = os.environ.get("V3_PYTHON")
    if explicit:
        return explicit
    candidate = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stages", default="p0,p1,p15,p2,p3", help="逗号分隔的阶段（默认全部）")
    parser.add_argument("--evidence", help="把本次运行输出写入该文件")
    args = parser.parse_args()

    requested = [item.strip().lower() for item in args.stages.split(",") if item.strip()]
    unknown = [item for item in requested if item not in STAGE_STEPS]
    if unknown:
        print(f"未登记的阶段: {unknown}（可选: {', '.join(STAGE_STEPS)}）")
        return 2

    python = venv_python()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"

    lines: list[str] = []
    failed: list[str] = []

    def emit(text: str) -> None:
        print(text)
        lines.append(text)

    emit(f"# 阶段门禁 {','.join(requested)}  {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    emit(f"# python: {python}")
    emit("")

    for stage in requested:
        emit(f"===== 阶段 {stage.upper()} =====")
        for name, argv, note in STAGE_STEPS[stage]:
            # 两个**极小**的 step 约定，避免为"跑非 Python 的检查"新增机制：
            #   `prefix:<可执行文件>` —— 用它替代默认的 python 解释器（如 `prefix:node`）
            #   `cwd:<相对路径>`       —— 在别处运行（如 `cwd:apps/web-admin`）
            # 之所以要它们：前端检查必须用 node、且必须在它自己的目录下跑。
            runner = python
            step_cwd = REPO_ROOT
            args_out: list[str] = []
            for item in argv:
                if item.startswith("prefix:"):
                    runner = item.split(":", 1)[1]
                elif item.startswith("cwd:"):
                    step_cwd = REPO_ROOT / item.split(":", 1)[1]
                else:
                    args_out.append(item)
            command = [runner, *args_out]
            emit(f"## {name}")
            emit(f"# 覆盖: {note}")
            emit(f"# 命令: {' '.join(command)}")
            started = time.time()
            completed = subprocess.run(
                command,
                cwd=str(step_cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = time.time() - started
            for line in (completed.stdout or "").strip().splitlines()[-6:]:
                emit(f"#   {line}")
            if completed.stderr and completed.stderr.strip():
                for line in completed.stderr.strip().splitlines()[-4:]:
                    emit(f"#   [stderr] {line}")
            emit(f"# 退出码: {completed.returncode}  (耗时 {elapsed:.1f}s)")
            emit("")
            if completed.returncode != 0:
                failed.append(name)
        emit("")

    if failed:
        emit(f"结论: 失败 —— {', '.join(failed)}")
    else:
        emit("结论: 全部通过（exit 0）")

    if args.evidence:
        target = pathlib.Path(args.evidence)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[evidence] 已写入 {target}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
