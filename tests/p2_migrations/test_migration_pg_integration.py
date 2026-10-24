"""真实 PostgreSQL 集成验收（**需要显式提供连接串才运行**）。

## 为什么默认 skip 而不是"尽力而为地跑一下"

真实执行需要目标 PG 实例与凭据（属外部依赖）。这里**不猜、不读环境里的默认库**：
只有调用方显式给出 `V3_TEST_DATABASE_URL` 才运行，否则**明确 skip 并说明原因**。

这样做的目的：让"未验证"与"验证通过"在报告里**永远可区分**——
一个"没跑就说通过"的集成测试，比没有这个测试更糟。

## 它验证什么（真实库上的执行层证据）

1. 8 个 owner 的迁移能在真库上**无错执行**；
2. 账本 `v3_migration.schema_migrations` 真的被写、且第二次运行**不重跑**；
3. §13 隔离在真库上成立：未设 `app.tenant_id` 时 app 角色查不到数据（fail-closed）、
   跨 owner 读被拒、app 角色无超级权限、表属主也受 FORCE RLS 约束。

**边界**：本测试会在给定库上**建 schema / 建角色 / 写表**。
请只对你愿意重建的库使用它（脚本收尾会 DROP 自己建的 schema 与角色）。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from services._kernel import migration_ledger as ledger
from services._kernel import migration_runner as runner
from services._kernel import storage

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

DSN_ENV = "V3_TEST_DATABASE_URL"


def _dsn() -> str | None:
    return os.environ.get(DSN_ENV)


pytestmark = pytest.mark.skipif(
    not _dsn(),
    reason=(
        f"未设置 {DSN_ENV}：真实 PostgreSQL 执行属外部依赖。"
        "请显式提供**可重建**的库连接串后重跑，以取得执行层证据。"
    ),
)


@pytest.fixture(scope="module")
def adapter():
    instance = runner.Psycopg2Adapter(_dsn())
    try:
        yield instance
    finally:
        # 收尾：清掉本次建立的 schema、角色与账本，避免污染目标库
        cleanup = [
            *[f"DROP SCHEMA IF EXISTS {schema} CASCADE;" for schema in storage.OWNER_SCHEMAS.values()],
            f"DROP SCHEMA IF EXISTS {ledger.LEDGER_SCHEMA} CASCADE;",
            *[
                f"DROP ROLE IF EXISTS {name};"
                for owner in storage.OWNER_SCHEMAS
                for name in (storage.role_name(owner), storage.app_role_name(owner))
            ],
        ]
        for statement in cleanup:
            try:
                instance.execute(statement)
            except Exception:  # noqa: BLE001 - 清理尽力而为，不影响测试结论
                pass
        instance.close()


def test_all_owner_migrations_execute_on_real_postgres(adapter):  # A01
    results = runner.apply_all_owners(REPO_ROOT, adapter)
    for result in results:
        assert result.applied, f"{result.owner} 未应用任何迁移"
        assert result.error is None, f"{result.owner} 执行失败：{result.error}"


def test_second_run_applies_nothing_on_real_postgres(adapter):  # A01
    """真库上第二次运行必须**零应用**（幂等）。"""
    runner.apply_all_owners(REPO_ROOT, adapter)
    second = runner.apply_all_owners(REPO_ROOT, adapter)
    for result in second:
        assert result.applied == [], f"{result.owner} 第二次运行仍应用了 {result.applied}"
        assert result.skipped, f"{result.owner} 第二次运行应当把迁移记为 skipped"


def test_ledger_records_every_version_with_hash(adapter):  # A01
    runner.apply_all_owners(REPO_ROOT, adapter)
    rows = adapter.fetch_all(f"SELECT owner, version, sha256 FROM {ledger.LEDGER_TABLE}")
    recorded = {(owner, version): digest for owner, version, digest in rows}
    for owner in sorted(storage.OWNER_SCHEMAS):
        for item in ledger.discover_migrations(REPO_ROOT, owner):
            key = (owner, item.version)
            assert key in recorded, f"账本缺 {key}"
            assert recorded[key] == ledger.sha256_of_migration(REPO_ROOT, item), f"{key} 哈希不符"


def test_real_postgres_enforces_row_level_security_fail_closed(adapter):  # A01
    """§13 的执行层证据：**未设置 `app.tenant_id` 时 app 角色查不到任何数据**。"""
    runner.apply_all_owners(REPO_ROOT, adapter)
    adapter.execute(
        'INSERT INTO "control-plane".tasks (tenant_id, task_id, payload_json) '
        "VALUES ('tenant-demo','task-a','{}'::jsonb), ('tenant-c3','task-b','{}'::jsonb);"
    )

    adapter.execute("SET ROLE control_app;")
    try:
        # ① 未设 tenant → 0 行（fail-closed）
        assert adapter.fetch_all('SELECT count(*) FROM "control-plane".tasks;')[0][0] == 0

        # ② 设了 tenant → 只看得到本租户
        adapter.execute("SET app.tenant_id = 'tenant-demo';")
        assert adapter.fetch_all('SELECT count(*) FROM "control-plane".tasks;')[0][0] == 1
    finally:
        adapter.execute("RESET ROLE;")


def test_real_postgres_rejects_cross_owner_read(adapter):  # A01
    """§13 的执行层证据：`control_app` 读 `transaction-service` 的表必须被拒。"""
    runner.apply_all_owners(REPO_ROOT, adapter)
    adapter.execute("SET ROLE control_app;")
    try:
        with pytest.raises(Exception) as caught:
            adapter.fetch_all('SELECT count(*) FROM "transaction-service".commands;')
        assert "permission denied" in str(caught.value).lower(), str(caught.value)
    finally:
        adapter.execute("RESET ROLE;")


def test_real_postgres_app_roles_have_no_super_privileges(adapter):  # A01
    runner.apply_all_owners(REPO_ROOT, adapter)
    rows = adapter.fetch_all(
        "SELECT rolname FROM pg_roles "
        "WHERE rolname LIKE '%\\_app' "
        "AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls);"
    )
    assert rows == [], f"以下 app 角色带超级权限：{rows}"


def test_real_postgres_enforces_forced_rls_for_table_owner(adapter):  # A01
    """FORCE RLS 的含义：**表属主也绕不过策略**（执行层证据）。"""
    runner.apply_all_owners(REPO_ROOT, adapter)
    adapter.execute("SET ROLE control_owner;")
    try:
        adapter.execute("SET app.tenant_id = 'tenant-demo';")
        assert adapter.fetch_all('SELECT count(*) FROM "control-plane".tasks;')[0][0] == 1
        adapter.execute("RESET app.tenant_id;")
        assert adapter.fetch_all('SELECT count(*) FROM "control-plane".tasks;')[0][0] == 0, (
            "FORCE RLS 未生效：表属主在未设 tenant 时仍看到了数据"
        )
    finally:
        adapter.execute("RESET ROLE;")
