"""迁移执行器（P2）：按版本顺序应用待迁移，并写 `schema_migrations` 账本。

## 为什么用「可注入的 DB 适配器」而不是直接连 psycopg

真实执行需要目标 PostgreSQL（属外部依赖）。但**执行顺序、幂等、账本记账、失败处理**
这些逻辑与具体驱动无关，且正是最容易出错的部分。把驱动抽成一个小协议后：

- 用内存适配器即可**完整验证执行序与账本语义**（可重复、无外部依赖）；
- 真实 PG 适配器只是一层薄壳，换驱动不影响已被测试锁定的语义。

这与 `storage.py`（只生成 DDL 文本、把不变量变成可机检断言）是同一思路。

## 硬约束（§13）

- **已应用迁移不得重跑，也不得被改**：哈希不符或文件消失 ⇒ 抛错，拒绝继续；
- **顺序由版本号决定**，不由文件名字典序；
- **单条迁移失败即停止**，不继续应用后续版本（避免半截状态被后续迁移误判）；
- 账本写在自己版本的同一批操作里，**只追加**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from . import migration_ledger as ledger
from . import storage


class MigrationExecutionError(RuntimeError):
    """迁移执行过程中出现不可继续的错误。"""


@runtime_checkable
class DatabaseAdapter(Protocol):
    """执行迁移所需的最小数据库能力。

    刻意只有两个方法：迁移执行只需要"按顺序执行 SQL"与"读账本"。
    能力面越小，真实驱动越好实现、替换成本越低。
    """

    def execute(self, sql: str) -> None:
        """执行一段 SQL（不返回结果）。"""

    def fetch_all(self, sql: str) -> list[tuple]:
        """执行查询并返回全部行。"""


@dataclass
class InMemoryAdapter:
    """内存适配器：记录所有执行过的语句，并模拟 `schema_migrations` 账本。

    仅用于验证执行序与账本语义——它**不是**数据库模拟器，
    不解析 SQL、不保证事务，只忠实地"记录收到了什么、按什么顺序"。
    """

    executed: list[str] = field(default_factory=list)
    ledger_rows: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    fail_on: str | None = None  # 含该子串的语句执行失败 → 用于测"失败即停"

    def execute(self, sql: str) -> None:
        if self.fail_on and self.fail_on in sql:
            raise MigrationExecutionError(f"注入的失败：{sql[:60]}")
        self.executed.append(sql)

    def fetch_all(self, sql: str) -> list[tuple]:
        if "schema_migrations" not in sql:
            return []
        return [
            (owner, version, row["file_name"], row["sha256"])
            for (owner, version), row in sorted(self.ledger_rows.items())
        ]

    # ---- 测试辅助：模拟"某迁移已被应用" ----
    def mark_applied(self, owner: str, version: str, full_path: str, digest: str) -> None:
        self.ledger_rows[(owner, version)] = {
            "file_name": full_path,
            "sha256": digest,
        }


@dataclass
class ApplyResult:
    owner: str
    ledger_created: bool = False
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed_version: str | None = None
    error: str | None = None


def _split_statements(sql: str) -> list[str]:
    """把迁移文件内容拆成可逐条执行的语句。

    处理要点：
    - 注释行（`--`）整行丢弃——但**保留 `DO $$ ... $$;` 里的内容**，
      因为函数体里也可能出现 `--`（本项目的角色创建语句就是 `DO $$ ... $$;`）；
    - `DO $$ ... $$;` 是**一条**语句，里面的分号不能当分隔符。

    实现方式：按 `$$` 出现次数判断是否处于 dollar-quoted 体内，只有在体外才按 `;` 断句。
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_dollar_quote = False

    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not in_dollar_quote:
            if not line or line.startswith("--"):
                continue

        # 逐字符扫描，正确处理同一行里可能出现的 $$ 与 ;
        index = 0
        while index < len(line):
            if line.startswith("$$", index):
                in_dollar_quote = not in_dollar_quote
                buffer.append("$$")
                index += 2
                continue
            char = line[index]
            buffer.append(char)
            index += 1
            if char == ";" and not in_dollar_quote:
                statement = "".join(buffer).strip()
                if statement and statement != ";":
                    statements.append(statement)
                buffer = []
        buffer.append("\n")

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s.strip() not in ("", ";")]


def apply_owner_migrations(
    repo_root,
    owner: str,
    adapter: DatabaseAdapter,
    *,
    applied: dict[str, str] | None = None,
    executed_by: str = "migration-runner",
) -> ApplyResult:
    """应用单个 owner 的待迁移。

    `applied` 若为 None，则从适配器的账本读取——真实场景就该这么做；
    显式传入只用于测试。
    """
    if owner not in storage.OWNER_SCHEMAS:
        raise MigrationExecutionError(f"未登记的 owner: {owner}")

    result = ApplyResult(owner=owner)

    if applied is None:
        applied = {
            version: digest
            for _owner, version, _file_name, digest in adapter.fetch_all(
                f"SELECT owner, version, file_name, sha256 FROM {ledger.LEDGER_TABLE}"
            )
            if _owner == owner
        }

    if not applied and _ledger_needs_creation(adapter):
        adapter.execute(ledger.render_ledger_ddl())
        # 账本建表与业务迁移**分开记账**：两者混在一个列表里，
        # 调用方无法区分"我建了账本"和"我应用了一个迁移"。
        result.ledger_created = True

    try:
        pending = ledger.pending_migrations(repo_root, owner, applied)
    except ledger.MigrationLedgerError as error:
        raise MigrationExecutionError(str(error)) from error

    available = [item for item in ledger.discover_migrations(repo_root, owner) if item.version]
    result.skipped = [item.relative_path for item in available if item.version in applied]

    for item in pending:
        path = ledger.migrations_root(repo_root) / item.relative_path
        sql = path.read_text(encoding="utf-8")
        try:
            for statement in _split_statements(sql):
                adapter.execute(statement)
        except Exception as error:  # noqa: BLE001 - 逐条失败必须显式停下并报出
            result.failed_version = item.version
            result.error = f"{item.relative_path}: {error}"
            # **失败即停**：不继续应用后续版本，避免半截状态
            raise MigrationExecutionError(result.error) from error

        digest = ledger.sha256_text(sql)
        adapter.execute(
            f"INSERT INTO {ledger.LEDGER_TABLE} (owner, version, file_name, sha256, applied_by) "
            f"VALUES ('{owner}', '{item.version}', '{item.relative_path}', '{digest}', '{executed_by}')"
        )
        if isinstance(adapter, InMemoryAdapter):
            adapter.mark_applied(owner, item.version, item.relative_path, digest)
        result.applied.append(item.relative_path)

    return result


def _ledger_needs_creation(adapter: DatabaseAdapter) -> bool:
    """判断账本表是否还不存在。

    内存适配器没有"表不存在"的概念，故一律视为需要创建；
    真实适配器应由驱动报 `undefined_table` 来驱动这一步——
    这里只做保守判断：**账本为空就建**（DDL 本身幂等）。
    """
    try:
        rows = adapter.fetch_all(f"SELECT owner, version, file_name, sha256 FROM {ledger.LEDGER_TABLE}")
    except Exception:  # noqa: BLE001 - 表不存在
        return True
    return not rows


def apply_all_owners(repo_root, adapter: DatabaseAdapter, *, executed_by: str = "migration-runner") -> list[ApplyResult]:
    """按 owner 顺序应用全部待迁移；**任一 owner 失败即整体停止**。"""
    results: list[ApplyResult] = []
    for owner in sorted(storage.OWNER_SCHEMAS):
        results.append(apply_owner_migrations(repo_root, owner, adapter, executed_by=executed_by))
    return results


# ==========================================================================
# 真实 PostgreSQL 适配器（薄壳）
# ==========================================================================
#
# 只做两件事：把 SQL 丢给驱动、把查询结果取回来。所有顺序/幂等/账本语义
# 都在上面的 `apply_owner_migrations` 里，已由内存适配器完整锁定——
# 于是这一层即使出问题，也不可能悄悄改掉执行语义（它没有那个能力）。


class Psycopg2Adapter:
    """基于 psycopg2 的适配器。

    **凭据只从调用方传入的连接串取**，本模块不读环境变量、不落盘、不打印。
    每个 `execute` 用独立事务：迁移是"逐条推进、逐条记账"的，
    把整批放进一个事务会掩盖"哪一条失败了"。
    """

    def __init__(self, dsn: str, *, autocommit: bool = True) -> None:
        import psycopg2  # 延迟导入：没装驱动的环境仍可 import 本模块

        self._connection = psycopg2.connect(dsn)
        self._connection.autocommit = autocommit

    def execute(self, sql: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql)

    def fetch_all(self, sql: str) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Psycopg2Adapter":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
