#!/usr/bin/env python3
"""
SQLite → PostgreSQL 数据迁移脚本（制造业 AI 员工平台）

用法（用户自己终端执行）：
  1. 先确保 Postgres 库已建好（docker compose up -d postgres 或用已有 PG）
  2. 在 backend/.env 设好 DATABASE_URL=postgresql://...
  3. 确保依赖: pip install psycopg2-binary
  4. 执行: python migrate_to_postgres.py

脚本只负责"数据迁移"——表结构已由后端启动的 create_all 自动创建，
此脚本 TRUNCATE 目标表后逐表导入 SQLite 数据。
"""

import os
import sys
import json
import argparse
import sqlite3
from datetime import datetime, date

# ======================= 配置 =======================

SQLITE_DB = os.getenv(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "industrial_ai.db"),
)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://industrial:industrial123@localhost:5432/industrial_ai",
)

# ======================= 迁移顺序（按外键依赖） =======================
TABLE_ORDER = [
    "companies",
    "permissions",
    "roles",
    "users",
    "ai_agents",
    "agent_packages",
    "api_keys",
    "agent_permissions",
    "agent_memory",
    "tool_permissions",
    "customers",
    "quotations",
    "conversations",
    "leads",
    "production_orders",
    "quality_reports",
    "bills",
    "audit_logs",
    "email_logs",
    "documents",
    "integration_configs",
    "site_contents",
    "channel_configs",
    "company_package_installs",
    "member_invites",
    "sms_codes",
    "platform_llm_config",
]

# ======================= 列类型映射 =======================
# JSON 列（SQLite 存 TEXT → Postgres 需要 json.loads）
JSON_COLUMNS = {
    "agent_packages": ["agents", "tools"],
    "integration_configs": ["extra"],
    "leads": ["enriched"],
    "site_contents": [
        "hero", "about", "features", "customers", "contact",
        "pricing", "social", "translations",
    ],
    "platform_llm_config": ["config"],
}

# BOOLEAN 列（SQLite 存 0/1 → Postgres True/False）
BOOLEAN_COLUMNS = {
    "companies": ["license_verified", "mobile_verified"],
    "api_keys": ["enabled"],
    "production_orders": ["delay"],
    "leads": ["opened", "converted"],
    "integration_configs": ["enabled"],
    "sms_codes": ["used"],
}

# ======================= 辅助 =======================

def connect_pg():
    try:
        import psycopg2
    except ImportError:
        print("❌ 缺少 psycopg2-binary，请先安装: pip install psycopg2-binary")
        sys.exit(1)
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def convert_value(table: str, col: str, val):
    """把 SQLite 值转换为适合 Postgres 的 Python 值"""
    if val is None:
        return None

    # JSON 列：SQLite TEXT → Python dict/list
    if table in JSON_COLUMNS and col in JSON_COLUMNS[table]:
        if isinstance(val, str):
            if val.strip() == "":
                return {}
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val  # 无法解析时原样保留
        return val

    # BOOLEAN 列：SQLite 0/1 → Python bool
    if table in BOOLEAN_COLUMNS and col in BOOLEAN_COLUMNS[table]:
        if isinstance(val, int):
            return bool(val)
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes", "on")
        return val

    # FLOAT 列：确保是 float
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str) and val.strip() == "":
        return None

    return val


# ======================= 主逻辑 =======================

def main(dry_run=False):
    # 1. 连接 SQLite
    if not os.path.exists(SQLITE_DB):
        print(f"❌ SQLite 数据库不存在: {SQLITE_DB}")
        sys.exit(1)
    sqlite_con = sqlite3.connect(SQLITE_DB)
    sqlite_con.row_factory = sqlite3.Row
    sqlite_cur = sqlite_con.cursor()
    print(f"📂 SQLite 源: {SQLITE_DB}")

    # 2. 连接 Postgres（dry-run 则只连 SQLite）
    pg_con = None
    if dry_run:
        print("🔍 DRY-RUN 模式（仅检查 SQLite，不写 Postgres）\n")
    else:
        print(f"🗄️  Postgres 目标: {DATABASE_URL}")
        pg_con = connect_pg()
        pg_con.autocommit = True  # TRUNCATE 在事务外执行
        pg_cur = pg_con.cursor()

    # 3. 逐表迁移
    total_rows = 0
    skipped_empty = 0

    for t in TABLE_ORDER:
        # 检查 SQLite 是否有这张表
        sqlite_cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (t,),
        )
        if not sqlite_cur.fetchone():
            if not dry_run:
                print(f"  ⏭ {t}: SQLite 中无此表，跳过")
            else:
                print(f"  ⏭ {t}: 不存在")
            continue

        # 读列信息
        sqlite_cur.execute(f"PRAGMA table_info([{t}])")
        cols_info = sqlite_cur.fetchall()
        col_names = [c["name"] for c in cols_info]

        # 读数据
        sqlite_cur.execute(f"SELECT * FROM [{t}]")
        rows = sqlite_cur.fetchall()

        if not rows:
            skipped_empty += 1
            if not dry_run:
                print(f"  ⏭ {t}: 0 行数据，跳过")
            else:
                print(f"  ⏭ {t}: 0 行")
            continue

        row_count = len(rows)
        total_rows += row_count

        if dry_run:
            # 预览几行
            preview = min(3, row_count)
            sample_cols = col_names[:6]
            print(f"  ✓  {t}: {row_count} 行 → 列 {sample_cols}")
            for i in range(preview):
                r = rows[i]
                vals = {k: r[k] for k in sample_cols}
                print(f"     [{i}] {vals}")
            if row_count > preview:
                print(f"     ... 还有 {row_count - preview} 行")
            continue

        # ---- 实际写入 ----
        print(f"  📤 {t}: {row_count} 行 → TRUNCATE + INSERT", end="", flush=True)

        # TRUNCATE（清空目标表）
        try:
            pg_cur.execute(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE')
        except Exception as e:
            print(f"\n     ⚠ TRUNCATE 失败 ({e})，尝试 DELETE")
            try:
                pg_cur.execute(f'DELETE FROM "{t}"')
            except Exception as e2:
                print(f"     ❌ DELETE 也不行: {e2}")
                continue

        # 构建 INSERT
        placeholders = ", ".join(["%s"] * len(col_names))
        cols_quoted = ", ".join(f'"{c}"' for c in col_names)
        insert_sql = f'INSERT INTO "{t}" ({cols_quoted}) VALUES ({placeholders})'

        # 批量插入（每行做类型转换）
        success = 0
        errors = 0
        for row in rows:
            try:
                values = [
                    convert_value(t, col_names[i], row[col_names[i]])
                    for i in range(len(col_names))
                ]
                pg_cur.execute(insert_sql, values)
                success += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    pk_val = row[col_names[0]] if col_names else "?"
                    print(f"\n     ⚠ 行 {pk_val} 插入失败: {e}")

        # 修复自增序列（让 Postgres 的 SERIAL 从 max(id)+1 继续）
        if "id" in col_names and success > 0:
            try:
                pg_cur.execute(f'SELECT setval(pg_get_serial_sequence(\'{t}\', \'id\'), (SELECT MAX(id) FROM "{t}"))')
            except Exception:
                # 不是 SERIAL 列则忽略
                pass

        if errors:
            print(f"\n     ⚠ {success}/{row_count} 行成功，{errors} 行失败")
        else:
            print(f" ✓")

    sqlite_con.close()
    if pg_con:
        pg_con.close()

    print(f"\n{'='*50}")
    print(f"总计: {total_rows} 行 (含 {skipped_empty} 张空表跳过)")
    if dry_run:
        print("🔍 以上为 DRY-RUN 预览，未实际写入数据库。")
        print(f"   确认无误后执行: python migrate_to_postgres.py")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
