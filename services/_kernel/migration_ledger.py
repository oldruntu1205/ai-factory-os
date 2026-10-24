"""P2 迁移台账：把 `storage.py` 生成的 DDL 固化成**每 owner 可审计的迁移文件**。

## 为什么要有这一层

`storage.py` 已能生成 DDL 并暴露可机检的不变量，但那是**内存里的字符串**：
- 没人能 review "实际要执行什么"；
- 迁移一旦执行就不可改，但没有东西**冻结**已发布的文本；
- 生成器被人改动后，无从知道历史迁移是否被动过。

因此把生成结果落成文件 + 清单（含内容哈希）。清单是**唯一真相的冻结凭据**：
门禁每轮从生成器**重新生成**并与文件逐字节比对，不一致即报红。

## 三条硬规则（上游 §13）

1. **已应用的迁移不得修改，只能新增**——由清单里的 `sha256` 保证；
2. **每 owner 独立目录**，不得写别人的目录——目录即边界，生成时按 owner 落盘；
3. **不得跨 owner 建外键/授权**——由 `storage.py` 的不变量 + 本模块的迁移级复核共同保证。

真实执行 DDL 需要目标 PG 实例（**待冻结**），故本模块只做"生成 + 冻结 + 校验"。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any

from . import storage

# 迁移版本号：格式 NNNN。首次落地为 0001。
# 规则：**不得**就地修改已发布版本的内容；变更必须新增 0002 并在其中写差异。
INITIAL_MIGRATION_VERSION = "0001"

MIGRATIONS_DIR_NAME = "migrations"
MANIFEST_NAME = "MANIFEST.json"


def migrations_root(repo_root: pathlib.Path) -> pathlib.Path:
    return repo_root / MIGRATIONS_DIR_NAME


# ==========================================================================
# 版本发现与顺序（§13：不改已应用迁移，只能新增）
# ==========================================================================

# 迁移文件名：`<4 位数字>_<描述>.sql`。数字决定执行顺序。
MIGRATION_FILE_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<slug>[a-z0-9_]+)\.sql$")

# 已应用迁移的记录表：真实执行面用它判断"哪些已应用"。
# 放在**独立的 schema** 里——不属于任何业务 owner，
# 否则"记录迁移"本身就变成一次跨 owner 写。
LEDGER_SCHEMA = "v3_migration"
LEDGER_TABLE = f"{LEDGER_SCHEMA}.schema_migrations"


def render_ledger_ddl() -> str:
    """生成迁移记录表的 DDL（幂等，可重复执行）。"""
    return "\n".join(
        [
            f"CREATE SCHEMA IF NOT EXISTS {LEDGER_SCHEMA};",
            f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (",
            "    owner text NOT NULL,",
            "    version text NOT NULL,",
            "    file_name text NOT NULL,",
            "    sha256 text NOT NULL,",
            "    applied_at timestamptz NOT NULL DEFAULT now(),",
            "    applied_by text NOT NULL DEFAULT current_user,",
            "    PRIMARY KEY (owner, version)",
            ");",
            "-- 只追加：已应用的行不得修改（「不改已应用迁移」的账本侧保障）",
            "",
        ]
    )


@dataclass(frozen=True)
class MigrationFile:
    owner: str
    version: str
    slug: str
    relative_path: str

    @property
    def is_initial(self) -> bool:
        return self.version == INITIAL_MIGRATION_VERSION


def discover_migrations(repo_root: pathlib.Path, owner: str) -> list[MigrationFile]:
    """发现某 owner 目录下的全部迁移，**按版本号升序**返回。

    这是"多版本递增"的入口：新增 `0002_xxx.sql` 即自动被发现，
    不需要改任何代码或清单逻辑。
    """
    directory = migrations_root(repo_root) / owner_dir_name(owner)
    if not directory.exists():
        return []
    found: list[MigrationFile] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILE_PATTERN.match(path.name)
        if not match:
            # 命名不符的 .sql **不静默忽略**——它是"看起来像迁移但不会被执行"的陷阱
            found.append(
                MigrationFile(owner=owner, version="", slug=path.name, relative_path=f"{owner}/{path.name}")
            )
            continue
        found.append(
            MigrationFile(
                owner=owner,
                version=match.group("version"),
                slug=match.group("slug"),
                relative_path=f"{owner}/{path.name}",
            )
        )
    return sorted(found, key=lambda item: item.version)


def malformed_migration_names(repo_root: pathlib.Path) -> list[str]:
    """文件名不符合 `<4位数字>_<slug>.sql` 的迁移。

    必须显式报错而不是跳过：命名不符的文件**不会被执行**，但看起来像迁移——
    "以为加了迁移、其实没生效"是最危险的静默失效。
    """
    offenders: list[str] = []
    for owner in sorted(storage.OWNER_SCHEMAS):
        for item in discover_migrations(repo_root, owner):
            if not item.version:
                offenders.append(item.relative_path)
    return offenders


def version_sequence_problems(repo_root: pathlib.Path) -> list[str]:
    """版本序列问题：重复版本号、缺失初始版本、版本不连续。

    规则（§13「不改已应用迁移，只能新增」）：
    - 每个 owner 必须从 `0001` 开始（不能凭空从 0002 起）；
    - 版本号**不得重复**（重复意味着两个迁移争同一个序号）；
    - 版本号必须**连续**（跳号多半是误删了某个已发布迁移）。
    """
    problems: list[str] = []
    for owner in sorted(storage.OWNER_SCHEMAS):
        items = [item for item in discover_migrations(repo_root, owner) if item.version]
        versions = [item.version for item in items]
        if not versions:
            problems.append(f"{owner}: 没有任何迁移")
            continue
        if versions[0] != INITIAL_MIGRATION_VERSION:
            problems.append(f"{owner}: 首个版本是 {versions[0]}，必须从 {INITIAL_MIGRATION_VERSION} 开始")
        duplicates = sorted({v for v in versions if versions.count(v) > 1})
        if duplicates:
            problems.append(f"{owner}: 版本号重复 {duplicates}")
        expected = [f"{i:04d}" for i in range(1, len(versions) + 1)]
        if versions != expected:
            problems.append(f"{owner}: 版本序列不连续，实际 {versions}，期望 {expected}")
    return problems


def migration_file_name(version: str) -> str:
    return f"{version}_schema.sql"


def owner_dir_name(owner: str) -> str:
    """owner → 迁移目录名。

    `tools` owner 的目录名是 `tools`（不是 schema 名 `tool-gateway`）：
    目录按 **owner** 组织，因为 §13 的隔离边界是 owner（谁能写哪张表），
    而 schema 名是部署细节。用 schema 名会让"目录名 = owner 名"这条
    简单规则失效，进而让人分不清谁该写哪个目录。
    """
    return owner


def render_owner_migration(owner: str, *, version: str) -> str:
    """渲染单个 owner 的迁移文件内容（含头部注释 + 生成区标记）。"""
    ddl = storage.generate_owner_ddl(owner)
    schema = storage.OWNER_SCHEMAS[owner]
    header = [
        f"-- owner: {owner}",
        f"-- schema: {schema}",
        f"-- 角色: {storage.role_name(owner)} / {storage.app_role_name(owner)}",
        f"-- version: {version}",
        "--",
        "-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。",
        "-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。",
        "-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。",
        "--",
        f"-- 隔离要点（上游 §13）：本 schema 只授权给 {owner} 自己的角色；",
        "-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。",
        "",
    ]
    return "\n".join(header) + ddl.text + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MigrationLedgerError(RuntimeError):
    """迁移台账的一致性问题（已应用迁移被删或改）。"""


def sha256_of_migration(repo_root: pathlib.Path, item: MigrationFile) -> str:
    path = migrations_root(repo_root) / item.relative_path
    return sha256_text(path.read_text(encoding="utf-8"))


def pending_migrations(
    repo_root: pathlib.Path,
    owner: str,
    applied: dict[str, str],
) -> list[MigrationFile]:
    """算出某 owner 的**待应用**迁移。

    `applied` = `{version: sha256}`，来自真实执行面的 `schema_migrations` 记录表。

    三条判定（§13「不改已应用迁移，只能新增」在**代码侧**的落实）：
    1. 已应用但**文件已不存在** ⇒ 抛错：少了一个已发布迁移（删除更不允许）；
    2. 已应用但**哈希不一致** ⇒ 抛错：已发布迁移被改过，必须人工介入；
    3. 其余未应用的按版本升序执行。
    """
    available = {item.version: item for item in discover_migrations(repo_root, owner) if item.version}

    vanished = sorted(set(applied) - set(available))
    if vanished:
        raise MigrationLedgerError(
            f"{owner}: 以下已应用迁移的文件已不存在（不得删除已发布迁移）：{vanished}"
        )

    for version in sorted(applied):
        item = available[version]
        current = sha256_of_migration(repo_root, item)
        if current != applied[version]:
            raise MigrationLedgerError(
                f"{owner}: 已应用迁移 {item.relative_path} 的内容与记录哈希不一致"
                f"（记录 {applied[version][:12]}…，当前 {current[:12]}…）——已发布迁移不得修改"
            )

    return [item for version, item in sorted(available.items()) if version not in applied]



def manifest_version_problems(repo_root: pathlib.Path, manifest: dict[str, Any]) -> list[str]:
    """校验清单登记的版本与磁盘**逐项对齐**（独立判据，供门禁使用）。

    为什么不让门禁直接用 `discover_migrations` 去对比清单：那样两边用的是**同一套
    排序/过滤逻辑**，一旦这套逻辑被改坏，两边会一起坏、互相印证"通过"。
    （实测：把 `discover_migrations` 的排序改成按文件名字典序，原先的门禁照样全绿。）

    这里改用**独立判据**：不依赖 `discover_migrations` 的顺序，
    自己算集合与升序，并逐项复核 sha256。
    """
    problems: list[str] = []
    for entry in manifest["owners"]:
        owner = entry["owner"]
        registered = [item["version"] for item in entry["migrations"]]

        # ① 必须升序（版本号决定执行顺序，乱序即错）
        if registered != sorted(registered):
            problems.append(f"{owner}: 清单版本未按升序登记 {registered}")

        # ② 必须从 0001 起且连续（跳号 = 少了已发布迁移）
        if registered:
            expected = [f"{i:04d}" for i in range(1, len(registered) + 1)]
            if registered != expected:
                problems.append(f"{owner}: 清单版本序列应为 {expected}，实际 {registered}")

        # ③ 必须与磁盘上的**文件名集合**一致（只取集合，不依赖排序函数）
        directory = migrations_root(repo_root) / owner_dir_name(owner)
        on_disk: set[str] = set()
        if directory.exists():
            for path in directory.glob("*.sql"):
                match = MIGRATION_FILE_PATTERN.match(path.name)
                if match:
                    on_disk.add(match.group("version"))
        missing_on_disk = sorted(set(registered) - on_disk)
        missing_in_manifest = sorted(on_disk - set(registered))
        if missing_on_disk:
            problems.append(f"{owner}: 清单登了但磁盘没有的版本 {missing_on_disk}")
        if missing_in_manifest:
            problems.append(f"{owner}: 磁盘有但清单未登记的版本 {missing_in_manifest}")

        # ④ 每个版本的 sha256 必须与实际文件一致
        for item in entry["migrations"]:
            path = migrations_root(repo_root) / item["path"]
            if not path.exists():
                problems.append(f"{owner}: 迁移文件缺失 {item['path']}")
                continue
            actual = sha256_text(path.read_text(encoding="utf-8"))
            if item["sha256"] != actual:
                problems.append(
                    f"{owner}: {item['path']} 哈希不符"
                    f"（清单 {item['sha256'][:12]}…，实际 {actual[:12]}…）"
                )
    return problems


def build_manifest(repo_root: pathlib.Path, *, version: str = INITIAL_MIGRATION_VERSION) -> dict[str, Any]:
    """构造完整清单（不落盘）。

    `owners[].migrations` 覆盖该 owner **全部已发布版本**（不只是当前生成的那版）。

    为什么这么设计：如果清单只冻结 `0001`，那么等 `0002` 出现后，
    门禁就再也看不见 `0001` 是否被改过——**冻结只对"最近一版"有效**，
    而 §13 要求的恰恰是"已应用迁移（含所有历史版本）永不可改"。
    因此清单按 owner 汇总全部版本的 sha256：新增版本不削弱对旧版本的冻结。
    """
    entries: list[dict[str, Any]] = []
    for owner in sorted(storage.OWNER_SCHEMAS):
        # 磁盘上已存在的版本（新增版本后这里自动包含历史版本）
        existing = [item for item in discover_migrations(repo_root, owner) if item.version]
        # 本次要生成的版本也可能尚未落盘，一并纳入
        versions = {item.version: item.relative_path for item in existing}
        versions.setdefault(version, f"{owner_dir_name(owner)}/{migration_file_name(version)}")

        migrations: list[dict[str, Any]] = []
        for item_version in sorted(versions):
            relative = versions[item_version]
            disk_path = migrations_root(repo_root) / relative
            if disk_path.exists():
                digest = sha256_text(disk_path.read_text(encoding="utf-8"))
            elif item_version == version:
                digest = sha256_text(render_owner_migration(owner, version=item_version))
            else:
                # 清单里登记了却没有文件 ⇒ 由 drift_report 的 missing 分支报出
                digest = ""
            migrations.append({"version": item_version, "path": relative, "sha256": digest})

        entries.append(
            {
                "owner": owner,
                "schema": storage.OWNER_SCHEMAS[owner],
                "path": f"{owner_dir_name(owner)}/{migration_file_name(version)}",
                "version": version,
                "sha256": sha256_text(render_owner_migration(owner, version=version)),
                "statement_count": len(
                    [line for line in storage.generate_owner_ddl(owner).statements]
                ),
                "table_count": len(storage.OWNER_TABLES[owner]),
                "migrations": migrations,
            }
        )
    return {
        "generator": "services/_kernel/storage.py",
        "note": (
            "由 scripts/p2_migrations.py 生成；"
            "owners[].migrations 冻结该 owner **全部已发布版本**的 sha256"
            "（§13：已应用迁移不得修改，只能新增）"
        ),
        "ledger_table": LEDGER_TABLE,
        "owners": entries,
    }


def render_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write_migrations(repo_root: pathlib.Path, *, version: str = INITIAL_MIGRATION_VERSION) -> list[str]:
    """写出全部 owner 的迁移文件与 MANIFEST。返回写入的相对路径列表。"""
    root = migrations_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    for owner in sorted(storage.OWNER_SCHEMAS):
        directory = root / owner_dir_name(owner)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / migration_file_name(version)
        path.write_text(render_owner_migration(owner, version=version), encoding="utf-8", newline="")
        written.append(str(path.relative_to(repo_root)).replace("\\", "/"))

    manifest_path = root / MANIFEST_NAME
    manifest_path.write_text(render_manifest(build_manifest(repo_root, version=version)),
                             encoding="utf-8", newline="")
    written.append(str(manifest_path.relative_to(repo_root)).replace("\\", "/"))
    return written


# ==========================================================================
# 门禁用的校验（不写盘）
# ==========================================================================

def drift_report(repo_root: pathlib.Path, *, version: str = INITIAL_MIGRATION_VERSION) -> dict[str, list[str]]:
    """比对"生成器当前输出"与"磁盘上的已发布迁移"。

    返回四类漂移：
    - `missing`：清单里有、磁盘上没有；
    - `unexpected`：磁盘的 owner 目录里有清单未登记的文件；
    - `content_drift`：文件内容与清单 sha256 不符（**被人手改**）；
    - `manifest_drift`：清单本身与生成器当前输出不符。
    """
    root = migrations_root(repo_root)
    expected = build_manifest(repo_root, version=version)
    report: dict[str, list[str]] = {
        "missing": [],
        "unexpected": [],
        "content_drift": [],
        "manifest_drift": [],
    }

    # 逐个版本校验（不只校验"本次生成的那一版"）：
    # 否则 0002 出现后，0001 被动过就再也查不出来。
    for entry in expected["owners"]:
        for item in entry["migrations"]:
            path = root / item["path"]
            if not path.exists():
                report["missing"].append(item["path"])
                continue
            actual = sha256_text(path.read_text(encoding="utf-8"))
            if actual != item["sha256"]:
                report["content_drift"].append(
                    f"{item['path']}（清单 {item['sha256'][:12]}… ≠ 实际 {actual[:12]}…）"
                )

    # 磁盘上多出来的 .sql（清单未登记）
    registered = {
        item["path"] for entry in expected["owners"] for item in entry["migrations"]
    }
    for owner in sorted(storage.OWNER_SCHEMAS):
        directory = root / owner_dir_name(owner)
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.sql")):
            relative = f"{owner_dir_name(owner)}/{path.name}"
            if relative not in registered:
                report["unexpected"].append(relative)

    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        report["missing"].append(MANIFEST_NAME)
    else:
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
        if on_disk != expected:
            report["manifest_drift"].append(
                f"{MANIFEST_NAME} 与生成器输出不一致（磁盘 owners={len(on_disk.get('owners', []))}，"
                f"生成 {len(expected['owners'])}）"
            )
        # 独立判据：逐项复核版本集合、顺序与哈希（不依赖 discover_migrations 的排序）
        for problem in manifest_version_problems(repo_root, on_disk):
            report["manifest_drift"].append(problem)
    return report


def owner_directories_are_disjoint(repo_root: pathlib.Path) -> list[str]:
    """校验每 owner 目录只含自己的迁移（§13：不得写别人的目录）。

    判定方式：目录名必须等于某个已登记 owner；且该目录内每个 `.sql` 的头部
    `-- owner:` 必须与目录名一致。头部与目录名不符 = 把别人的迁移放进了本目录。
    """
    root = migrations_root(repo_root)
    offenders: list[str] = []
    if not root.exists():
        return offenders

    known_owners = set(storage.OWNER_SCHEMAS)
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if directory.name.startswith("__") or directory.name == "adr":
            continue
        if directory.name not in known_owners:
            # P0 遗留的 `migrations/<owner>/README.md` 目录名与 owner 同名；
            # 若目录名不是 owner，则必须显式登记，否则视为越界目录。
            offenders.append(f"未登记 owner 的迁移目录: {directory.name}")
            continue
        for path in sorted(directory.glob("*.sql")):
            head = path.read_text(encoding="utf-8").splitlines()[:3]
            declared = [line for line in head if line.startswith("-- owner:")]
            if not declared or declared[0].split(":", 1)[1].strip() != directory.name:
                offenders.append(f"{directory.name}/{path.name}: 文件声明 owner 与目录不符")
    return offenders
