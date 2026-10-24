"""P1 门禁可信度验证：注入实现缺陷，门禁必须报红（变异测试）。

为什么需要：门禁"跑绿"只说明没发现已知模式的问题，不说明它能发现新问题。
P1 的核心断言是"C3/C4 无出域路径"，必须证明**一旦出域真的发生，门禁会失败**。

用法：
    python scripts/mutate_p1.py            # 依次注入全部变异并报告每个变异是否被捕获
    python scripts/mutate_p1.py --list     # 只列出变异

每个变异：备份原文件 → 注入 → 跑指定测试 → 记录是否失败 → 还原。
任何变异"未被捕获"都会让本脚本 exit 1（说明门禁有盲区）。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"

# (名称, 相对文件, 原文, 变异后, 目标测试节点)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "放开 C3/C4 出域（矩阵改成永远 allow）",
        "services/_kernel/governance.py",
        '"C3": {"local": "allow", "enterprise_cloud": "deny", "public": "deny"},',
        '"C3": {"local": "allow", "enterprise_cloud": "allow", "public": "allow"},',
        "tests/p1_runtime/test_p1_runtime.py::test_sensitive_classes_never_permitted_offsite",
    ),
    (
        "去掉租户等级上限（C4 数据可用低等级租户处理）",
        "services/_kernel/context.py",
        "    if _class_rank(data_class) > _class_rank(max_class):\n        raise errors.reject_unknown_data_class(data_class)",
        "    if False:\n        raise errors.reject_unknown_data_class(data_class)",
        "tests/p1_runtime/test_p1_runtime.py",
    ),
    (
        "取消检索前租户过滤（改成先全库召回）",
        "services/_kernel/knowledge.py",
        "        if document.get(\"tenant_id\") != context.tenant_id:\n            # 跨租户候选在召回阶段就被排除，绝不进入后续链路。\n            continue",
        "        if False:\n            continue",
        "tests/p1_runtime/test_p1_runtime.py::test_cross_tenant_candidates_are_filtered_before_retrieval",
    ),
    (
        "Vault 不校验有效期（只看格式）",
        "services/_kernel/vault.py",
        "    if expires_epoch is None or expires_epoch <= _now_epoch():",
        "    if expires_epoch is None:",
        "tests/p1_runtime/test_p1_runtime.py::test_expired_mapping_is_rejected_by_clock_not_string",
    ),
    (
        "审计不再记录被拒绝的出域尝试",
        "services/_kernel/routes.py",
        "    except errors.V3Error:\n        egress_audit.record(",
        "    except errors.V3Error:\n        _ = (lambda **kwargs: None)(\n            ",
        "tests/p1_runtime/test_p1_runtime.py",
    ),
]


def run(target: str) -> bool:
    """返回 True 表示测试通过（=变异**未被**捕获）。"""
    command = [str(VENV_PYTHON), "-m", "pytest", target, "-q", "--no-header", "--tb=no"]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"},
    )
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, path, _, _, target in MUTATIONS:
            print(f"- {name}  ({path} → {target})")
        return 0

    escaped: list[str] = []
    for name, relative, original, mutated, target in MUTATIONS:
        file = REPO_ROOT / relative
        backup = file.read_text(encoding="utf-8")
        if original not in backup:
            print(f"[skip] {name}: 原文未命中（实现可能已变化）")
            escaped.append(name)
            continue
        try:
            file.write_text(backup.replace(original, mutated, 1), encoding="utf-8")
            passed = run(target)
        finally:
            file.write_text(backup, encoding="utf-8")
        if passed:
            print(f"[逃逸] {name}: 注入缺陷后门禁**仍然通过**")
            escaped.append(name)
        else:
            print(f"[捕获] {name}: 门禁正确报红")

    print()
    if escaped:
        print(f"结论: {len(escaped)} 个变异未被捕获 → 门禁有盲区: {', '.join(escaped)}")
        return 1
    print(f"结论: 全部 {len(MUTATIONS)} 个变异都被门禁捕获")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
