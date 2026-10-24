"""存储所有权与隔离的 DDL 生成（上游 §13）。

上游 §13 的**硬要求**（不是建议基线）：

- 每个服务**独立 schema** + **独立 DB role**；生产不使用超级用户。
- **严禁**跨 owner 直接写表、跨服务 join、跨 owner 外键。
- 所有业务主键、唯一索引、幂等域**必须包含 tenant**。
- **RLS 作为第二道隔离**；应用层身份检查仍为必须。
- 迁移每 owner 独立目录，不改已应用迁移。

本模块只**生成 DDL 文本**并暴露**可机检的不变量**，不连接数据库——
于是"隔离是否成立"可以进 CI 门禁，而不是靠人工 review 一段 SQL。
真正执行 DDL 需要目标 PG 实例（属外部依赖，**待冻结**）。
"""

from __future__ import annotations

import re

import re
from dataclasses import dataclass, field

# 与 p0 service-manifest.json 的 owned_schema 枚举逐字一致
OWNER_SCHEMAS: dict[str, str] = {
    "security": "security-gateway",
    "control": "control-plane",
    "decision": "decision-service",
    "runtime": "hermes-runtime",
    "manufacturing": "manufacturing-core",
    "tools": "tool-gateway",
    "transaction": "transaction-service",
    "evidence": "trajectory-eval",
}

# 每 owner 的表定义：表名 → 主键列
# 约定：含 tenant 的表，主键/唯一键**必须**以 tenant_id 打头（§13）。
OWNER_TABLES: dict[str, dict[str, list[str]]] = {
    "security": {
        "classification_records": ["tenant_id", "classification_id"],
        "egress_audits": ["tenant_id", "audit_ref"],
        "token_vault_mappings": ["tenant_id", "mapping_ref"],
    },
    "control": {
        "tasks": ["tenant_id", "task_id"],
        "approvals": ["tenant_id", "approval_id"],
        "policy_evaluations": ["tenant_id", "evaluation_ref"],
        "knowledge_answers": ["tenant_id", "task_id", "trace_id"],
    },
    "decision": {
        "decisions": ["tenant_id", "decision_id"],
    },
    "runtime": {
        "runs": ["tenant_id", "run_id"],
        "checkpoints": ["tenant_id", "run_id", "version"],
    },
    "manufacturing": {
        "fact_projections": ["tenant_id", "projection_ref"],
        "source_snapshots": ["tenant_id", "snapshot_ref"],
    },
    "tools": {
        "tool_specs": ["tool_id", "version"],          # 全局目录，非租户数据
        "tool_executions": ["tenant_id", "execution_ref"],
    },
    "transaction": {
        "commands": ["tenant_id", "command_id"],
        "authority_receipts": ["tenant_id", "receipt_id"],
        "saga_steps": ["tenant_id", "saga_id", "step_id"],
        "command_idempotency": ["tenant_id", "idempotency_key"],
    },
    "evidence": {
        "outbox": ["tenant_id", "event_id"],
        "inbox": ["tenant_id", "consumer_unit", "event_id"],
        "dead_letters": ["tenant_id", "dlq_id"],
    },
}

# 显式豁免：不是租户数据的全局目录表（§13 允许，但必须显式登记，不能靠"忘了加 tenant"）。
# 键用 `owner.table` 而不是 `schema.table`——schema 名与 owner 名并不相同
# （如 owner `tools` 的 schema 是 `tool-gateway`），用 schema 名会永远匹配不上。
GLOBAL_TABLES = {"tools.tool_specs"}

# 显式豁免 RLS：非租户数据不需要行级隔离
NO_RLS_TABLES = GLOBAL_TABLES


def role_name(owner: str) -> str:
    return f"{owner}_owner"


def app_role_name(owner: str) -> str:
    return f"{owner}_app"


@dataclass
class OwnerDDL:
    owner: str
    schema: str
    statements: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.statements)


def generate_owner_ddl(owner: str) -> OwnerDDL:
    """生成单 owner 的 schema + role + 表 + RLS + 权限。

    刻意**不含**跨 owner 的 GRANT/USE：隔离靠"谁都不给别人权限"实现，
    而不是靠"给了再限制"。
    """
    if owner not in OWNER_SCHEMAS:
        raise KeyError(f"未登记的 owner: {owner}")
    schema = OWNER_SCHEMAS[owner]
    ddl = OwnerDDL(owner=owner, schema=schema)

    # 1) schema 与最小权限角色
    #
    # 两条实测教训（写成注释，避免下次再犯）：
    #   ① PostgreSQL **没有** `BYPASSRLS OFF` 这种语法——合法的关闭写法是
    #      `NOBYPASSRLS`。写成 `BYPASSRLS OFF` 在真库上会直接被语法错误拒绝
    #      （旧的判定用子串匹配，把这个错误**同时**误报成"合法"而放过了）。
    #   ② `CREATE ROLE ... IF NOT EXISTS` 只在**不存在**时生效：若角色已存在
    #      但权限被改宽，重跑迁移**不会**纠正。因此每次迁移都补一条
    #      `ALTER ROLE` 重新声明属性，让迁移真正幂等且自纠偏。
    ddl.statements.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")

    owner_role = role_name(owner)
    app_role = app_role_name(owner)
    owner_attrs = "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    app_attrs = "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"

    ddl.statements.append(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{owner_role}') "
        f"THEN CREATE ROLE {owner_role} {owner_attrs}; "
        f"END IF; END $$;"
    )
    ddl.statements.append(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{app_role}') "
        f"THEN CREATE ROLE {app_role} {app_attrs}; "
        f"END IF; END $$;"
    )
    # 自纠偏：无论角色是新建还是已存在，属性一律重声明为最小权限
    ddl.statements.append(f"ALTER ROLE {owner_role} {owner_attrs};")
    ddl.statements.append(f"ALTER ROLE {app_role} {app_attrs};")
    # 所有者不继承他人权限；应用角色隶属（可 SET ROLE 到 owner）
    ddl.statements.append(f"GRANT {owner_role} TO {app_role};")
    ddl.statements.append(f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {owner_role};")
    ddl.statements.append(f"ALTER SCHEMA {schema} OWNER TO {owner_role};")

    for table, primary_key in OWNER_TABLES[owner].items():
        qualified = f"{schema}.{table}"
        columns = _columns_for(primary_key)
        ddl.statements.append(
            f"CREATE TABLE IF NOT EXISTS {qualified} (\n    {columns},\n"
            f"    payload_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,\n"
            f"    updated_at timestamptz NOT NULL DEFAULT now(),\n"
            f"    PRIMARY KEY ({', '.join(primary_key)})\n);"
        )
        ddl.statements.append(f"ALTER TABLE {qualified} OWNER TO {role_name(owner)};")

        if f"{owner}.{table}" in NO_RLS_TABLES:
            # 全局目录表：显式登记豁免，既不加 tenant 也不开 RLS。
            # 豁免判定必须在 tenant 不变量检查**之前**，否则登记了也过不去。
            continue
        if "tenant_id" not in primary_key:
            # 不变量：租户表的隔离域必须包含 tenant
            raise AssertionError(f"{qualified} 的隔离域缺少 tenant_id")

        # RLS：第二道隔离（应用层检查仍为必须）
        ddl.statements.append(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY;")
        ddl.statements.append(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY;")
        policy = f"{table}_tenant_isolation"
        ddl.statements.append(
            f"CREATE POLICY {policy} ON {qualified} "
            f"USING (tenant_id = current_setting('app.tenant_id', true)) "
            f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true));"
        )

    # 2) 只对本 schema 授权；**不生成**任何跨 owner 的 GRANT
    ddl.statements.append(f"GRANT USAGE ON SCHEMA {schema} TO {app_role_name(owner)};")
    ddl.statements.append(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role_name(owner)};"
    )
    ddl.statements.append(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {app_role_name(owner)};"
    )
    # 显式撤销对 public 的默认权限（防止 accidentally 全局可读）
    ddl.statements.append(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")
    ddl.statements.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC;")
    return ddl


def _columns_for(primary_key: list[str]) -> str:
    parts = []
    for column in primary_key:
        if column == "version":
            parts.append("version integer NOT NULL")
        elif column == "aggregate_version":
            parts.append("aggregate_version integer NOT NULL")
        else:
            parts.append(f"{column} text NOT NULL")
    return ", ".join(parts)


def generate_all_ddl() -> dict[str, OwnerDDL]:
    return {owner: generate_owner_ddl(owner) for owner in OWNER_SCHEMAS}


# ==========================================================================
# 可机检的不变量（供门禁断言，不依赖数据库）
# ==========================================================================

def _schema_owner() -> dict[str, str]:
    """schema 名 → owner 名。schema 名与 owner 名不同，必须先建这张反向表。"""
    return {schema: owner for owner, schema in OWNER_SCHEMAS.items()}


def _role_owner() -> dict[str, str]:
    """角色名 → 它所属的 owner。`{owner}_owner` 与 `{owner}_app` 都归属该 owner。"""
    mapping: dict[str, str] = {}
    for owner in OWNER_SCHEMAS:
        mapping[role_name(owner)] = owner
        mapping[app_role_name(owner)] = owner
    return mapping


_GRANT_RE = re.compile(
    r"^GRANT\s+(?P<privs>.+?)\s+ON\s+(?P<kind>SCHEMA|ALL\s+TABLES\s+IN\s+SCHEMA)\s+"
    r"(?P<object>[A-Za-z_][\w-]*)\s+TO\s+(?P<grantee>[A-Za-z_][\w-]*)",
    re.IGNORECASE,
)


def cross_owner_grants() -> list[str]:
    """找出所有跨 owner 的授权语句。

    正确形态下**必须为空**：任何角色只被授予**自己 owner** 的 schema。

    实现要点（血泪教训）：判定必须是**确定性解析**，不能靠
    "语句里出现了别人的 schema 名 + 出现了自己的角色名"这种启发式。
    实测该启发式漏检了 `GRANT USAGE ON SCHEMA <别人 schema> TO <自己 app 角色>` ——
    最典型的越权形态反而恰好不被捕获。现在按正则拆出「被授权对象」与「被授权者」，
    再比对两者是否同属一个 owner。
    """
    offenders: list[str] = []
    schema_owner = _schema_owner()
    role_owner = _role_owner()
    for _, ddl in generate_all_ddl().items():
        for statement in ddl.statements:
            match = _GRANT_RE.match(statement.strip())
            if not match:
                continue
            target_schema = match.group("object")
            grantee = match.group("grantee")
            owner_of_schema = schema_owner.get(target_schema)
            owner_of_grantee = role_owner.get(grantee)
            if owner_of_schema is None:
                # 授权对象不属于任何已登记 owner（例如被授予了别的库/schema）
                offenders.append(f"未登记 schema 被授权: {statement}")
                continue
            if owner_of_grantee is None:
                offenders.append(f"未登记角色被授权: {statement}")
                continue
            if owner_of_schema != owner_of_grantee:
                offenders.append(
                    f"跨 owner 授权（{owner_of_schema} 的 schema → {owner_of_grantee} 的角色）: {statement}"
                )
    return offenders


def tenant_scoped_tables_missing_tenant() -> list[str]:
    """找出缺 tenant 隔离域的非全局表。"""
    missing: list[str] = []
    for owner, tables in OWNER_TABLES.items():
        for table, primary_key in tables.items():
            qualified = f"{OWNER_SCHEMAS[owner]}.{table}"
            if f"{owner}.{table}" in GLOBAL_TABLES:
                continue
            if "tenant_id" not in primary_key:
                missing.append(qualified)
    return missing


def tables_without_rls() -> list[str]:
    """找出未启用/未强制 RLS 的租户表。"""
    missing: list[str] = []
    for owner, ddl in generate_all_ddl().items():
        for table, _ in OWNER_TABLES[owner].items():
            qualified = f"{OWNER_SCHEMAS[owner]}.{table}"
            if f"{owner}.{table}" in NO_RLS_TABLES:
                continue
            if f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY;" not in ddl.statements:
                missing.append(qualified)
            if f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY;" not in ddl.statements:
                missing.append(f"{qualified} (未 FORCE)")
    return missing


def roles_with_excessive_privileges() -> list[str]:
    """超级用户 / CREATEDB / CREATEROLE / REPLICATION / BYPASSRLS 都不允许。

    实现要点（**第二次踩同一个坑**）：判定必须是**逐 token 解析**，不能按子串匹配。

    上一版按子串判 `BYPASSRLS`，于是 PostgreSQL 的**合法**关闭写法
    `NOBYPASSRLS` 也含该子串 → 把正确写法误报为过度权限。
    与 `cross_owner_grants` 那次是同一类错误：**用"字符串里有没有"代替"语法上是不是"**。

    现在把角色选项拆成 token，再判断每个选项的**极性与名字**：

    - `SUPERUSER` / `CREATEDB` / `CREATEROLE` / `REPLICATION` / `BYPASSRLS`
      只有在**开启形态**（无 `NO` 前缀）才算违规；
    - `NOSUPERUSER` / `NOBYPASSRLS` 等是合法的关闭形态；
    - 同时兼容字段式写法 `rolsuper=false` 这类（按 `=` 右侧取值判定）。
    """
    forbidden = ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS")
    bad: list[str] = []

    for _, ddl in generate_all_ddl().items():
        for statement in ddl.statements:
            if "CREATE ROLE" not in statement and "ALTER ROLE" not in statement:
                continue
            # 去掉引号与分号，避免把字符串字面量当成角色选项
            cleaned = statement.replace("'", " ").replace('"', " ").replace(";", " ")
            for raw_token in re.split(r"[\s,()]+", cleaned):
                token = raw_token.strip().upper()
                if not token:
                    continue
                # 字段式：ROLBYPASSRLS=TRUE / ROLSUPERUSER = FALSE
                if "=" in token and "ROLE" in token:
                    name, _, value = token.partition("=")
                    if name.startswith("ROLE") and value in ("TRUE", "ON", "1"):
                        bad.append(f"{name} 为开启形态: {statement[:100]}")
                    continue
                if token.startswith("ROLE"):  # ROLSUPERUSER 这类字段名（无 = 时视为字段名，跳过）
                    continue
                for name in forbidden:
                    if token == name:
                        bad.append(f"{name}: {statement[:100]}")
                    elif token == f"NO{name}":
                        pass  # 显式关闭：合法
    return bad


def foreign_keys_across_owners() -> list[str]:
    """跨 owner 外键：正确形态下不允许出现任何 REFERENCES 跨 schema。"""
    offenders: list[str] = []
    for owner, ddl in generate_all_ddl().items():
        own_schema = OWNER_SCHEMAS[owner]
        for statement in ddl.statements:
            if "REFERENCES" not in statement.upper():
                continue
            for other_owner, other_schema in OWNER_SCHEMAS.items():
                if other_owner == owner:
                    continue
                if f"REFERENCES {other_schema}." in statement:
                    offenders.append(statement)
        if f"REFERENCES {own_schema}." not in ddl.text and "REFERENCES" in ddl.text.upper():
            offenders.append(f"{owner}: 存在未限定 schema 的 REFERENCES")
    return offenders
