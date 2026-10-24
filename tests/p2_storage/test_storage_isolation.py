"""存储所有权与隔离的验收（上游 §13）。

这些断言**不需要数据库**：它们检查生成的 DDL 是否满足 §13 的硬不变量。
真正执行 DDL 需要目标 PG 实例（外部依赖，待冻结）。

价值：把"每 owner 独立 schema/role + RLS + 无跨 owner 授权"从"人工 review 一段 SQL"
变成**门禁可执行的断言**——这是 §13 里最容易在实现中被悄悄破坏的部分。
"""

from __future__ import annotations

import pytest

from services._kernel import storage


def test_every_owner_has_schema_and_roles():  # A01
    for owner, schema in storage.OWNER_SCHEMAS.items():
        ddl = storage.generate_owner_ddl(owner)
        assert f"CREATE SCHEMA IF NOT EXISTS {schema};" in ddl.statements
        assert any("CREATE ROLE" in s and storage.role_name(owner) in s for s in ddl.statements)
        assert any("CREATE ROLE" in s and storage.app_role_name(owner) in s for s in ddl.statements)
        assert f"ALTER SCHEMA {schema} OWNER TO {storage.role_name(owner)};" in ddl.statements


def test_no_cross_owner_grants():  # A01
    """§13：严禁跨 owner 直接写表。隔离靠"谁都不给别人权限"，不是"给了再限制"。"""
    assert storage.cross_owner_grants() == []


def test_no_foreign_keys_across_owners():  # A01
    """§13：禁止跨 owner 外键（跨边界用 ID 与 API/事件引用）。"""
    assert storage.foreign_keys_across_owners() == []


def test_roles_have_no_excessive_privileges():  # A01
    """无超级用户、无 CREATEDB/CREATEROLE/REPLICATION、BYPASSRLS 必须关闭。"""
    assert storage.roles_with_excessive_privileges() == []


def test_tenant_tables_all_carry_tenant_in_isolation_domain():  # A01
    """§13：所有业务主键、唯一索引、幂等域必须包含 tenant。"""
    assert storage.tenant_scoped_tables_missing_tenant() == []


def test_tenant_tables_force_rls():  # A01
    """RLS 是第二道隔离，且必须 FORCE（表所有者也不能绕过）。"""
    assert storage.tables_without_rls() == []


def test_global_tables_are_explicitly_registered():  # A01
    """全局目录表必须**显式登记**，不能靠"忘了加 tenant"混过去。

    登记键是 `owner.table`（不是 `schema.table`——schema 名与 owner 名不同）。
    """
    for qualified in storage.GLOBAL_TABLES:
        owner, _, table = qualified.partition(".")
        assert owner in storage.OWNER_SCHEMAS, f"未登记的 owner: {owner}"
        assert table in storage.OWNER_TABLES[owner], f"登记的全局表不存在: {qualified}"
    # 反向：非全局表不得被排除 RLS
    for owner, tables in storage.OWNER_TABLES.items():
        for table in tables:
            if f"{owner}.{table}" in storage.NO_RLS_TABLES:
                assert f"{owner}.{table}" in storage.GLOBAL_TABLES, f"{owner}.{table} 豁免 RLS 但未登记为全局表"


def test_every_tenant_table_has_a_tenant_isolation_policy():  # A01
    for owner, ddl in storage.generate_all_ddl().items():
        schema = storage.OWNER_SCHEMAS[owner]
        for table in storage.OWNER_TABLES[owner]:
            if f"{owner}.{table}" in storage.NO_RLS_TABLES:
                continue
            policy = f"CREATE POLICY {table}_tenant_isolation ON {schema}.{table}"
            assert policy in ddl.text, f"{schema}.{table} 缺少租户隔离策略"
            assert "current_setting('app.tenant_id'" in ddl.text


def test_public_privileges_are_revoked():  # A01
    """显式撤销 PUBLIC 的默认权限，避免"新建 schema 默认全局可读"。"""
    for _, ddl in storage.generate_all_ddl().items():
        assert any(s.startswith("REVOKE ALL ON SCHEMA") for s in ddl.statements)
        assert any("REVOKE ALL ON ALL TABLES IN SCHEMA" in s for s in ddl.statements)


def test_cross_owner_grant_detector_actually_detects():  # A01
    """防回归：跨 owner 授权检测必须是**确定性解析**，不能靠启发式。

    实测坑（2026-10-04）：原实现写成"语句里出现别人的 schema 名 + 出现自己的角色名"，
    结果**最典型的越权形态恰好漏检**——
    `GRANT USAGE ON SCHEMA <别人的 schema> TO <自己的 app 角色>`：
    被授权对象是别人的、被授权者是自己的，两个条件分开看都"正常"。
    变异测试把这个盲区暴露出来后，改成按正则拆出「被授权对象/被授权者」再比对 owner。
    """
    from services._kernel import storage as s

    real = s.generate_all_ddl

    def patched():
        ddl = real()
        # 注入一条典型的跨 owner 越权授权
        ddl["tools"].statements.insert(0, "GRANT USAGE ON SCHEMA transaction TO tools_app;")
        return ddl

    s.generate_all_ddl = patched
    try:
        offenders = s.cross_owner_grants()
    finally:
        s.generate_all_ddl = real
    assert offenders, "跨 owner 授权必须被检出（否则该门禁形同虚设）"
    assert any("GRANT USAGE ON SCHEMA transaction TO tools_app;" in item for item in offenders)


def test_unknown_owner_is_rejected():  # A01
    with pytest.raises(KeyError):
        storage.generate_owner_ddl("not-an-owner")


def test_regression_guard_privilege_check_is_not_naive_substring():  # A01
    """防回归：权限判定不能靠朴素子串。

    判定必须是**逐 token** 的：`NOBYPASSRLS` 与 `NOSUPERUSER` 是**正确**的关闭形态；
    若写成 `if "BYPASSRLS" in stmt: bad`，健康的 DDL 就会被误判为过度权限。

    **同时修正一个此前被当成"正确形态"的写法**：曾经的断言要求 DDL 含
    `BYPASSRLS OFF`，但 PostgreSQL 的 `CREATE ROLE` / `ALTER ROLE` **没有**
    `BYPASSRLS OFF` 这种语法（合法写法只有 `BYPASSRLS | NOBYPASSRLS`，
    见官方 CREATE ROLE 文档）。也就是说：那条断言在"防朴素子串回归"的同时，
    把一个**会导致真库语法错误的写法**固化了。现在断言正确的 `NOBYPASSRLS`。
    """
    ddl = storage.generate_owner_ddl("transaction")
    text = ddl.text
    assert "NOBYPASSRLS" in text, "关闭绕过必须用 PostgreSQL 合法写法 NOBYPASSRLS"
    assert "BYPASSRLS OFF" not in text, "BYPASSRLS OFF 不是合法语法，不得出现在 DDL 里"
    assert "NOSUPERUSER" in text
    assert storage.roles_with_excessive_privileges() == []
