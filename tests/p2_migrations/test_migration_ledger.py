"""P2 迁移台账门禁：把 §13 的隔离要求钉在**已发布迁移文本**上。

三层判据（任一不成立即失败）：

1. **清单完整**：8 个 owner 都有迁移，且没有清单未登记的多余 `.sql`；
2. **无漂移**：从 `storage.py` 重新生成后与磁盘逐字节一致
   （DDL 唯一真相是生成器；手改 SQL 必然报红）；
3. **owner 边界**：目录名必须是已登记 owner，且文件头部声明的 owner 与目录名一致
   （§13：不得写别人的迁移目录）。

第 2 条是这套门禁的核心价值：**迁移是"已执行就不可改"的**，
没有内容哈希冻结，就没有任何东西能阻止有人悄悄改一个已发布的迁移。
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from services._kernel import migration_ledger as ledger
from services._kernel import storage

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ==========================================================================
# 一、清单完整
# ==========================================================================

def test_manifest_lists_every_owner_exactly_once():  # A01
    manifest = ledger.build_manifest(REPO_ROOT)
    owners = [entry["owner"] for entry in manifest["owners"]]
    assert sorted(owners) == sorted(storage.OWNER_SCHEMAS), "清单必须恰好覆盖全部 owner"
    assert len(owners) == len(set(owners)), "清单里 owner 不得重复"


def test_every_owner_has_its_migration_on_disk():  # A01
    manifest = ledger.build_manifest(REPO_ROOT)
    root = ledger.migrations_root(REPO_ROOT)
    missing = [entry["path"] for entry in manifest["owners"] if not (root / entry["path"]).exists()]
    assert not missing, f"以下迁移文件缺失：{missing}"


def test_no_unregistered_sql_file_exists():  # A01
    """磁盘上不得有清单未登记的 `.sql`——新增迁移必须先登记进清单。"""
    report = ledger.drift_report(REPO_ROOT)
    assert not report["unexpected"], f"存在未登记迁移文件：{report['unexpected']}"


def test_manifest_tables_and_statements_counts_are_positive():  # A01
    manifest = ledger.build_manifest(REPO_ROOT)
    for entry in manifest["owners"]:
        assert entry["table_count"] >= 1, f"{entry['owner']} 没有任何表"
        assert entry["statement_count"] >= 5, f"{entry['owner']} 的语句数异常少"


# ==========================================================================
# 二、无漂移（防手改）
# ==========================================================================

def test_no_content_drift_against_generator():  # A01
    """已发布迁移必须与生成器输出**逐字节一致**。

    这条挡住的是：有人直接改 `migrations/**/*.sql` 而不改生成器。
    那样会让"实际执行的 DDL"与"经过不变量校验的 DDL"分家——
    你以为隔离成立，执行的却是另一份。
    """
    report = ledger.drift_report(REPO_ROOT)
    assert not report["content_drift"], (
        "以下迁移与生成器输出不一致（被手改或生成器改后未重新生成）：\n  "
        + "\n  ".join(report["content_drift"])
    )


def test_manifest_json_matches_generator():  # A01
    report = ledger.drift_report(REPO_ROOT)
    assert not report["manifest_drift"], report["manifest_drift"]


def test_generator_output_is_deterministic():  # A01
    """生成器必须是确定性的：同样输入两次生成必须完全一致。

    不确定性（如集合遍历顺序、时间戳）会让"冻结哈希"失去意义——
    每次校验都报红，于是门禁会被当成噪声关掉。
    """
    first = ledger.render_manifest(ledger.build_manifest(REPO_ROOT))
    second = ledger.render_manifest(ledger.build_manifest(REPO_ROOT))
    assert first == second, "清单生成不确定"


# ==========================================================================
# 三、owner 边界与 §13 硬要求
# ==========================================================================

def test_owner_directories_are_disjoint():  # A01
    offenders = ledger.owner_directories_are_disjoint(REPO_ROOT)
    assert not offenders, "迁移目录边界被破坏：\n  " + "\n  ".join(offenders)


def test_boundary_check_detects_file_declared_under_wrong_owner(tmp_path, monkeypatch):  # A01
    """把 A 的迁移塞进 B 的目录，必须被检出。

    **为什么单独一条**：`owner_directories_are_disjoint` 有两个分支——
    "目录名不是已登记 owner" 与 "文件声明的 owner 与目录名不符"。
    只在真实仓库上断言前者（后者需要构造），会让第二个分支**永远不被执行**：
    变异测试实测发现"把该分支关掉门禁照样全绿"，正是这个盲区。
    这里用临时目录把两个分支都真正跑一遍。
    """
    root = tmp_path / "migrations"
    (root / "control").mkdir(parents=True)
    (root / "transaction").mkdir(parents=True)

    # 分支 1：目录名不是已登记 owner
    (root / "not-an-owner").mkdir(parents=True)

    # 分支 2：文件声明的 owner 与目录名不符（把 transaction 的迁移放进 control）
    (root / "control" / "0002_wrong.sql").write_text(
        "-- owner: transaction\n-- schema: transaction-service\nSELECT 1;\n", encoding="utf-8"
    )

    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)
    offenders = ledger.owner_directories_are_disjoint(REPO_ROOT)

    assert any("not-an-owner" in item for item in offenders), f"未检出未登记目录：{offenders}"
    assert any("control/0002_wrong.sql" in item for item in offenders), (
        f"未检出「声明 owner 与目录不符」：{offenders}"
    )


@pytest.mark.parametrize("owner", sorted(storage.OWNER_SCHEMAS))
def test_each_migration_only_touches_its_own_schema(owner):  # A01
    """单 owner 迁移里只能出现**自己的** schema。

    这是"跨 owner 直接写表"在迁移文本层面的兜底：即便生成器被改坏，
    这条也会在已发布文本上立刻发现。
    """
    text = (ledger.migrations_root(REPO_ROOT) / ledger.owner_dir_name(owner)
            / ledger.migration_file_name(ledger.INITIAL_MIGRATION_VERSION)).read_text(encoding="utf-8")
    own = storage.OWNER_SCHEMAS[owner]
    others = [schema for name, schema in storage.OWNER_SCHEMAS.items() if name != owner]
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    leaked = [schema for schema in others if schema in body]
    assert not leaked, f"{owner} 的迁移里出现了别的 owner 的 schema：{leaked}"


@pytest.mark.parametrize("owner", sorted(storage.OWNER_SCHEMAS))
def test_each_migration_grants_only_to_own_roles(owner):  # A01
    """单 owner 迁移里的所有 GRANT，被授权者只能是自己的两个角色。"""
    text = (ledger.migrations_root(REPO_ROOT) / ledger.owner_dir_name(owner)
            / ledger.migration_file_name(ledger.INITIAL_MIGRATION_VERSION)).read_text(encoding="utf-8")
    allowed = {storage.role_name(owner), storage.app_role_name(owner)}
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    grantees = set(re.findall(r"\bTO\s+([A-Za-z_][\w-]*)", body))
    stray = grantees - allowed
    assert not stray, f"{owner} 的迁移把权限授给了非本 owner 角色：{stray}"


@pytest.mark.parametrize("owner", sorted(storage.OWNER_SCHEMAS))
def test_tenant_tables_have_forced_rls_in_migration(owner):  # A01
    """每个租户表在**已发布迁移文本**里都必须有 ENABLE + FORCE RLS。

    注意：这里查的是 SQL 文本而不是生成器对象——两者都要查，
    因为它们的失效方式不同（前者防手改，后者防生成逻辑写错）。
    """
    text = (ledger.migrations_root(REPO_ROOT) / ledger.owner_dir_name(owner)
            / ledger.migration_file_name(ledger.INITIAL_MIGRATION_VERSION)).read_text(encoding="utf-8")
    for table, primary_key in storage.OWNER_TABLES[owner].items():
        if f"{owner}.{table}" in storage.NO_RLS_TABLES:
            continue
        assert "tenant_id" in primary_key, f"{owner}.{table} 缺 tenant 隔离域"
        qualified = f"{storage.OWNER_SCHEMAS[owner]}.{table}"
        assert f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY;" in text, qualified
        assert f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY;" in text, qualified
        assert f"CREATE POLICY {table}_tenant_isolation ON {qualified}" in text, qualified


def test_migrations_do_not_touch_public_or_other_databases():  # A01
    """迁移不得下发到 public schema 或其他库。"""
    root = ledger.migrations_root(REPO_ROOT)
    offenders: list[str] = []
    for path in sorted(root.rglob("*.sql")):
        body = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for pattern in (r"\bpublic\.", r"\bCREATE DATABASE\b", r"\bDROP DATABASE\b", r"\\c\s"):
            if re.search(pattern, body, re.IGNORECASE):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {pattern}")
    assert not offenders, "迁移越界：\n  " + "\n  ".join(offenders)


def test_manifest_records_sha256_for_every_owner():  # A01
    manifest = json.loads((ledger.migrations_root(REPO_ROOT) / ledger.MANIFEST_NAME).read_text(encoding="utf-8"))
    for entry in manifest["owners"]:
        assert re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]), f"{entry['owner']} 的哈希格式非法"


# ==========================================================================
# 四、多版本递增机制（§13：不改已应用迁移，只能新增）
# ==========================================================================

def test_manifest_freezes_every_published_version_not_just_the_latest():  # A01
    """清单必须冻结该 owner 的**全部已发布版本**。

    为什么单独一条：如果清单只冻结"当前生成的那一版"，
    那么 `0002` 出现后 `0001` 被动过就再也查不出来——
    而 §13 要求的恰恰是"已应用迁移（含全部历史版本）永不可改"。

    **注意**：这里走 `manifest_version_problems` 这个**独立判据**，
    而不是用 `discover_migrations` 去比——否则两边共用同一套排序/过滤逻辑，
    一起坏就会互相印证"通过"（实测踩过）。
    """
    manifest = ledger.build_manifest(REPO_ROOT)
    problems = ledger.manifest_version_problems(REPO_ROOT, manifest)
    assert not problems, "清单与磁盘不一致：\n  " + "\n  ".join(problems)

    for entry in manifest["owners"]:
        versions = [item["version"] for item in entry["migrations"]]
        assert versions, f"{entry['owner']} 的清单没有登记任何版本"
        assert ledger.INITIAL_MIGRATION_VERSION in versions, f"{entry['owner']} 缺初始版本"


def test_manifest_validation_rejects_scrambled_version_order(tmp_path, monkeypatch):  # A01
    """独立判据必须能识破**版本顺序被打乱**的清单。

    这条是上一版的补洞：早先的断言拿 `discover_migrations` 的输出当基准，
    而同一个函数既是"被测对象"又是"基准"——把它的排序改坏后门禁仍然全绿。
    现在用构造好的乱序清单直接考 `manifest_version_problems`。
    """
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    for name in ("0001_schema.sql", "0002_more.sql"):
        (directory / name).write_text(f"-- {name}\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    (directory / "0001_schema.sql").write_text("-- 改过\n", encoding="utf-8")
    fake = {
        "owners": [
            {
                "owner": "control",
                "migrations": [
                    # 顺序被打乱 + 哈希与磁盘不符
                    {"version": "0002", "path": "control/0002_more.sql", "sha256": "0" * 64},
                    {"version": "0001", "path": "control/0001_schema.sql", "sha256": "0" * 64},
                ],
            }
        ]
    }
    problems = ledger.manifest_version_problems(REPO_ROOT, fake)
    assert any("未按升序" in item for item in problems), problems
    assert any("哈希不符" in item for item in problems), problems



def test_migration_file_names_are_well_formed():  # A01
    """命名不符 `<4位数字>_<slug>.sql` 的文件不会被任何执行器发现——
    它看起来像迁移却永不生效，因此必须显式报错而不是静默跳过。
    """
    offenders = ledger.malformed_migration_names(REPO_ROOT)
    assert not offenders, f"以下迁移文件名不合法（不会被执行）：{offenders}"


def test_version_sequence_is_continuous_and_starts_at_0001():  # A01
    problems = ledger.version_sequence_problems(REPO_ROOT)
    assert not problems, "版本序列有问题：\n  " + "\n  ".join(problems)


def test_discover_migrations_sorts_by_version(tmp_path, monkeypatch):  # A01
    """版本号决定执行顺序（不是字典序——`0010` 必须排在 `0002` 之后）。"""
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    for name in ("0002_add_index.sql", "0001_schema.sql", "0010_wide.sql"):
        (directory / name).write_text("-- x\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    versions = [item.version for item in ledger.discover_migrations(REPO_ROOT, "control")]
    assert versions == ["0001", "0002", "0010"], f"排序错误：{versions}"


def test_pending_migrations_skips_applied_versions(tmp_path, monkeypatch):  # A01
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    (directory / "0001_schema.sql").write_text("-- 0001\n", encoding="utf-8")
    (directory / "0002_more.sql").write_text("-- 0002\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    first = ledger.discover_migrations(REPO_ROOT, "control")[0]
    applied = {"0001": ledger.sha256_of_migration(REPO_ROOT, first)}
    pending = ledger.pending_migrations(REPO_ROOT, "control", applied)
    assert [item.version for item in pending] == ["0002"]


def test_pending_migrations_rejects_modified_applied_migration(tmp_path, monkeypatch):  # A01
    """执行器必须拒绝"已应用迁移被改过内容"，而不是照跑。"""
    root = tmp_path / "migrations"
    directory = root / "control"
    directory.mkdir(parents=True)
    target = directory / "0001_schema.sql"
    target.write_text("-- 原始内容\n", encoding="utf-8")
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    original_digest = ledger.sha256_text("-- 原始内容\n")
    target.write_text("-- 被改过的内容\n", encoding="utf-8")

    with pytest.raises(ledger.MigrationLedgerError, match="不得修改"):
        ledger.pending_migrations(REPO_ROOT, "control", {"0001": original_digest})


def test_pending_migrations_rejects_deleted_applied_migration(tmp_path, monkeypatch):  # A01
    """已应用迁移的文件不见了 ⇒ 必须拒绝（§13 连删除都不允许）。"""
    root = tmp_path / "migrations"
    (root / "control").mkdir(parents=True)
    monkeypatch.setattr(ledger, "migrations_root", lambda _repo: root)

    with pytest.raises(ledger.MigrationLedgerError, match="已不存在"):
        ledger.pending_migrations(REPO_ROOT, "control", {"0001": "a" * 64})


def test_ledger_ddl_is_idempotent_and_scoped_to_its_own_schema():  # A01
    """记录表 DDL：必须幂等，且放在不属于任何业务 owner 的独立 schema。"""
    ddl = ledger.render_ledger_ddl()
    assert "CREATE SCHEMA IF NOT EXISTS" in ddl
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert ledger.LEDGER_SCHEMA not in storage.OWNER_SCHEMAS.values(), (
        "记录表不得放进任何业务 owner 的 schema（否则记录迁移本身就是跨 owner 写）"
    )
    assert "PRIMARY KEY (owner, version)" in ddl, "必须有唯一键，防重复应用"

