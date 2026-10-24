"""P2 迁移生成脚本：从 `storage.py` 生成每 owner 的迁移文件与清单。

用法：
    python scripts/p2_migrations.py            # 生成（写盘）
    python scripts/p2_migrations.py --check    # 只校验是否漂移（门禁用）

为什么生成而不是手写：DDL 的唯一真相是 `storage.py`（它同时暴露可机检的隔离不变量）。
手写一份 SQL 会立刻产生第二个真相，两者必然漂移——而"漂移的 DDL"意味着
**你以为的隔离和实际执行的隔离不是一回事**。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services._kernel import migration_ledger, migration_runner, storage  # noqa: E402


def _apply(dsn_env: str) -> int:
    """对真实库应用待迁移。

    凭据**只从环境变量读**，不落盘、不打印、不进日志——连接串里含口令，
    一旦回显就可能留在终端历史或日志里。
    """
    dsn = os.environ.get(dsn_env)
    if not dsn:
        print(f"未设置环境变量 {dsn_env}：真实执行属外部依赖，拒绝猜测目标库。")
        print("用法：设置该变量为**可重建**库的连接串后重跑（例：postgresql://user@host/dbname）。")
        return 2

    try:
        adapter = migration_runner.Psycopg2Adapter(dsn)
    except ImportError:
        print("缺少 psycopg2：无法连接 PostgreSQL。")
        return 2
    except Exception as error:  # noqa: BLE001 - 连接失败只报类型与简述，不回显连接串
        print(f"连接失败：{type(error).__name__}（连接串已隐去）")
        return 2

    try:
        results = migration_runner.apply_all_owners(REPO_ROOT, adapter)
    except migration_runner.MigrationExecutionError as error:
        print(f"迁移执行失败：{error}")
        return 1
    finally:
        adapter.close()

    total = 0
    for result in results:
        if result.ledger_created:
            print(f"[{result.owner}] 已建账本表 {migration_ledger.LEDGER_TABLE}")
        for path in result.applied:
            print(f"[{result.owner}] 已应用 {path}")
            total += 1
        for path in result.skipped:
            print(f"[{result.owner}] 已是最新 {path}")
    print()
    print(f"本次应用 {total} 个迁移（其余已是最新）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 迁移生成 / 校验 / 应用")
    parser.add_argument("--check", action="store_true", help="只校验漂移，不写盘")
    parser.add_argument(
        "--apply",
        metavar="DSN_ENV",
        nargs="?",
        const="V3_DATABASE_URL",
        default=None,
        help=(
            "对真实 PostgreSQL 应用待迁移（默认从环境变量 V3_DATABASE_URL 取连接串）。"
            "需要 psycopg2；凭据只从该环境变量读，不落盘、不打印。"
        ),
    )
    parser.add_argument("--version", default=migration_ledger.INITIAL_MIGRATION_VERSION)
    args = parser.parse_args()

    if args.apply:
        return _apply(args.apply)

    if args.check:
        report = migration_ledger.drift_report(REPO_ROOT, version=args.version)
        offenders = migration_ledger.owner_directories_are_disjoint(REPO_ROOT)
        problems = 0
        for kind, items in report.items():
            if not items:
                continue
            problems += len(items)
            print(f"[漂移] {kind}:")
            for item in items:
                print(f"   - {item}")
        if offenders:
            problems += len(offenders)
            print("[漂移] owner 目录越界:")
            for item in offenders:
                print(f"   - {item}")
        print()
        if problems:
            print(f"迁移校验失败：{problems} 处漂移（§13：不改已应用迁移；DDL 唯一真相是 storage.py）")
            return 1
        owners = len(storage.OWNER_SCHEMAS)
        tables = sum(len(v) for v in storage.OWNER_TABLES.values())
        print(f"迁移校验通过：{owners} 个 owner / {tables} 张表，无漂移")
        return 0

    written = migration_ledger.write_migrations(REPO_ROOT, version=args.version)
    print(f"已生成 {len(written)} 个文件（version={args.version}）：")
    for path in written:
        print(f"   {path}")

    # 生成后立刻自检：不变量必须仍为空
    invariants = {
        "跨 owner 授权": storage.cross_owner_grants(),
        "租户表缺 tenant": storage.tenant_scoped_tables_missing_tenant(),
        "租户表缺 RLS": storage.tables_without_rls(),
        "角色过度权限": storage.roles_with_excessive_privileges(),
        "跨 owner 外键": storage.foreign_keys_across_owners(),
    }
    failed = {name: items for name, items in invariants.items() if items}
    if failed:
        print()
        print("生成后不变量校验失败（§13 硬要求）：")
        for name, items in failed.items():
            print(f"   [{name}] {len(items)} 项")
            for item in items[:3]:
                print(f"      - {item}")
        return 1
    print("不变量校验通过：跨 owner 授权/缺 tenant/缺 RLS/过度权限/跨 owner 外键 均为 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
