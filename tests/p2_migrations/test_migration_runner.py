"""迁移执行器门禁：锁定执行序、幂等与账本语义（§13）。

这一组用例的**价值定位**：真实执行需要目标 PostgreSQL（外部依赖），
但"按什么顺序执行、会不会重复执行、失败了会不会继续"这些**最容易出错**的部分
与驱动无关。用可注入的适配器把它们钉死，真实驱动就只剩一层薄壳。
"""

from __future__ import annotations

import pathlib

import pytest

from services._kernel import migration_ledger as ledger
from services._kernel import migration_runner as runner
from services._kernel import storage

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ==========================================================================
# 一、语句拆分（SQL 语义正确性的前提）
# ==========================================================================

def test_statement_splitter_keeps_dollar_quoted_block_as_one_statement():  # A01
    """`DO $$ ... $$;` 必须当作**一条**语句——里面的分号不是分隔符。

    这不是细节：角色的最小权限属性全靠 `DO $$ IF NOT EXISTS ... CREATE ROLE ... $$;`，
    若被误拆，真库上会直接语法报错。
    """
    sql = "\n".join(
        [
            "-- 注释行",
            "CREATE SCHEMA IF NOT EXISTS demo;",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1) THEN CREATE ROLE r NOLOGIN; END IF; END $$;",
            "ALTER ROLE r NOLOGIN NOBYPASSRLS;",
        ]
    )
    statements = runner._split_statements(sql)
    assert len(statements) == 3, statements
    assert statements[0].startswith("CREATE SCHEMA")
    assert statements[1].startswith("DO $$") and statements[1].rstrip().endswith("$$;")
    assert statements[2].startswith("ALTER ROLE")


def test_statement_splitter_drops_comments_but_keeps_content():  # A01
    sql = "-- 只有注释\n\n-- 还是注释\nSELECT 1;\n"
    assert runner._split_statements(sql) == ["SELECT 1;"]


def test_real_migrations_split_into_executable_statements():  # A01
    """真实迁移文件拆出来的每条语句都必须非空且带结束分号（DO 块以 `$$;` 结束）。"""
    for owner in sorted(storage.OWNER_SCHEMAS):
        item = ledger.discover_migrations(REPO_ROOT, owner)[0]
        sql = (ledger.migrations_root(REPO_ROOT) / item.relative_path).read_text(encoding="utf-8")
        statements = runner._split_statements(sql)
        assert statements, f"{item.relative_path} 拆不出任何语句"
        for statement in statements:
            assert statement.strip(), f"{item.relative_path} 有空语句"
            assert statement.rstrip().endswith(";"), f"{item.relative_path} 语句未以分号结束: {statement[:70]}"


# ==========================================================================
# 二、执行序与账本
# ==========================================================================

def test_applies_only_pending_and_records_ledger(tmp_path, monkeypatch):  # A01
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    (directory / "0001_schema.sql").write_text("CREATE SCHEMA IF NOT EXISTS s;\n", encoding="utf-8")
    (directory / "0002_more.sql").write_text("CREATE TABLE IF NOT EXISTS s.t (a text);\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    adapter = runner.InMemoryAdapter()
    result = runner.apply_owner_migrations(REPO_ROOT, "control", adapter, applied={})

    assert result.applied == ["control/0001_schema.sql", "control/0002_more.sql"]
    assert result.skipped == []
    body = [
        s for s in adapter.executed
        if not s.startswith("INSERT INTO") and not s.startswith(f"CREATE SCHEMA IF NOT EXISTS {ledger.LEDGER_SCHEMA}")
    ]
    assert body == [
        "CREATE SCHEMA IF NOT EXISTS s;",
        "CREATE TABLE IF NOT EXISTS s.t (a text);",
    ], body


def test_second_run_is_noop(tmp_path, monkeypatch):  # A01
    """**幂等**：第二次执行不得重跑任何迁移（§13）。"""
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    (directory / "0001_schema.sql").write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    adapter = runner.InMemoryAdapter()
    first = runner.apply_owner_migrations(REPO_ROOT, "control", adapter, applied={})
    assert first.applied == ["control/0001_schema.sql"]

    executed_before = list(adapter.executed)
    second = runner.apply_owner_migrations(REPO_ROOT, "control", adapter)
    assert second.applied == [], "第二次执行不得重跑"
    assert second.skipped == ["control/0001_schema.sql"]
    assert adapter.executed == executed_before, "第二次执行不得产生任何新语句"


def test_execution_order_follows_version_not_file_name(tmp_path, monkeypatch):  # A01
    """顺序由**版本号**决定。"""
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    for name in ("0002_b.sql", "0001_a.sql", "0003_c.sql"):
        (directory / name).write_text(f"SELECT '{name}';\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    adapter = runner.InMemoryAdapter()
    result = runner.apply_owner_migrations(REPO_ROOT, "control", adapter, applied={})
    order = [item for item in adapter.executed if item.startswith("SELECT")]
    assert order == ["SELECT '0001_a.sql';", "SELECT '0002_b.sql';", "SELECT '0003_c.sql';"], order
    assert result.applied == ["control/0001_a.sql", "control/0002_b.sql", "control/0003_c.sql"]


def test_failed_migration_stops_and_does_not_apply_later_versions(tmp_path, monkeypatch):  # A01
    """**失败即停**：单条迁移失败后不得继续应用后续版本（避免半截状态）。"""
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    (directory / "0001_ok.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (directory / "0002_boom.sql").write_text("SELECT 'BOOM';\n", encoding="utf-8")
    (directory / "0003_later.sql").write_text("SELECT 3;\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    adapter = runner.InMemoryAdapter(fail_on="BOOM")
    with pytest.raises(runner.MigrationExecutionError, match="0002_boom.sql"):
        runner.apply_owner_migrations(REPO_ROOT, "control", adapter, applied={})

    assert "SELECT 3;" not in adapter.executed, "失败后不得继续应用 0003"
    # 0001 的账本必须已写（它成功了）；0002/0003 不得写
    ledger_versions = sorted(
        version for _owner, version in adapter.ledger_rows
    )
    assert ledger_versions == ["0001"], ledger_versions


def test_apply_refuses_when_applied_migration_was_modified(tmp_path, monkeypatch):  # A01
    """已应用迁移被改过 ⇒ 执行器必须**拒绝继续**，而不是照跑。"""
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    target = directory / "0001_schema.sql"
    target.write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    recorded = ledger.sha256_text("SELECT 1;\n")
    target.write_text("SELECT 999;\n", encoding="utf-8")

    adapter = runner.InMemoryAdapter()
    with pytest.raises(runner.MigrationExecutionError, match="不得修改"):
        runner.apply_owner_migrations(REPO_ROOT, "control", adapter, applied={"0001": recorded})
    assert adapter.executed == [], "拒绝时不得执行任何语句"


def test_apply_refuses_when_applied_migration_disappeared(tmp_path, monkeypatch):  # A01
    root = tmp_path / "migrations"
    (root / "control").mkdir(parents=True)
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    adapter = runner.InMemoryAdapter()
    with pytest.raises(runner.MigrationExecutionError, match="已不存在"):
        runner.apply_owner_migrations(REPO_ROOT, "control", adapter, applied={"0001": "a" * 64})


def test_unknown_owner_is_rejected():  # A01
    adapter = runner.InMemoryAdapter()
    with pytest.raises(runner.MigrationExecutionError, match="未登记的 owner"):
        runner.apply_owner_migrations(REPO_ROOT, "not-an-owner", adapter, applied={})


# ==========================================================================
# 三、账本表本身
# ==========================================================================

def test_ledger_ddl_is_executed_before_first_migration():  # A01
    """账本表必须先于任何迁移被建出来，否则记账 INSERT 会失败。"""
    adapter = runner.InMemoryAdapter()
    result = runner.apply_owner_migrations(REPO_ROOT, "control", adapter)
    assert result.ledger_created is True
    assert adapter.executed[0].startswith(f"CREATE SCHEMA IF NOT EXISTS {ledger.LEDGER_SCHEMA}")
    # 账本 DDL 出现在第一条业务迁移语句之前
    first_migration_index = next(
        index for index, sql in enumerate(adapter.executed) if "CREATE SCHEMA IF NOT EXISTS control-plane" in sql
    )
    assert first_migration_index > 0


def test_every_applied_migration_is_recorded_with_its_hash():  # A01
    """每条应用过的迁移都要在账本里留下**哈希**——这是"不改已应用迁移"的凭据。"""
    adapter = runner.InMemoryAdapter()
    runner.apply_owner_migrations(REPO_ROOT, "transaction", adapter)
    inserts = [s for s in adapter.executed if s.startswith("INSERT INTO")]
    assert len(inserts) == 1, inserts
    assert "transaction" in inserts[0] and "0001" in inserts[0]
    # 记录中的哈希必须等于磁盘文件的真实哈希
    item = ledger.discover_migrations(REPO_ROOT, "transaction")[0]
    assert ledger.sha256_of_migration(REPO_ROOT, item) in inserts[0]


def test_apply_all_owners_covers_every_registered_owner():  # A01
    adapter = runner.InMemoryAdapter()
    results = runner.apply_all_owners(REPO_ROOT, adapter)
    assert [r.owner for r in results] == sorted(storage.OWNER_SCHEMAS)
    for result in results:
        assert result.applied, f"{result.owner} 没有应用任何迁移"
