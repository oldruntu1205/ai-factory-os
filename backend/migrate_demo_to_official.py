"""
migrate_demo_to_official.py —— 将「演示/运行库」的 CMS 多语言数据落到「正式库」

目的：
  演示库（当前运行的后台 demo）已包含多语言 overlay（translations）与打磨过的文案，
  但正式库（默认 industrial_ai.db）仍是旧 schema（无 translations 列）且轮播图是 3 张好图。
  本脚本把演示库的 translations + 各内容字段合并进正式库，同时保留正式库原有的 3 张轮播图。

用法（在可写数据库的环境中执行，例如你自己的终端）：
  python backend/migrate_demo_to_official.py
  或自定义路径：
  DEMO_DB=/path/site_verify.db OFFICIAL_DB=/path/industrial_ai.db \
      python backend/migrate_demo_to_official.py
  仅预览不写入：
  python backend/migrate_demo_to_official.py --dry-run

说明：
  - 会自动给正式库 ALTER TABLE 增加 translations 列（若不存在）。
  - hero 字段：采用演示库文案，但若正式库轮播图更多/演示库为空，则保留正式库轮播图。
  - 其余字段（about/features/customers/contact/pricing/social）整列采用演示库值。
"""
import os
import sys
import json
import sqlite3
import argparse
import datetime as _dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEMO_DB = os.getenv("DEMO_DB", "C:/Users/Administrator/.workbuddy/site_verify.db")
OFFICIAL_DB = os.getenv(
    "OFFICIAL_DB",
    os.path.join(BASE_DIR, "industrial_ai.db"),
)

CONTENT_COLS = ["hero", "about", "features", "customers", "contact", "pricing", "social"]


def load_row(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM site_contents WHERE id=1")
    row = cur.fetchone()
    con.close()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def ensure_translations_col(con):
    cur = con.cursor()
    cur.execute("PRAGMA table_info(site_contents)")
    cols = [c[1] for c in cur.fetchall()]
    if "translations" not in cols:
        cur.execute("ALTER TABLE site_contents ADD COLUMN translations JSON DEFAULT '{}'")
        con.commit()
        return True
    return False


def merge_hero(demo_hero, official_hero):
    """演示库文案 + 尽量保留更多轮播图（优先正式库的 slides）。"""
    try:
        demo = json.loads(demo_hero) if demo_hero else {}
    except Exception:
        demo = {}
    try:
        off = json.loads(official_hero) if official_hero else {}
    except Exception:
        off = {}
    demo_slides = demo.get("slides") or []
    off_slides = off.get("slides") or []
    # 保留轮播图更多的一方；都为空则留空
    if len(off_slides) >= len(demo_slides):
        demo["slides"] = off_slides
    # 若演示库完全没有 slides 键，补回正式库
    if "slides" not in demo and off_slides:
        demo["slides"] = off_slides
    return json.dumps(demo, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不写入")
    args = ap.parse_args()

    if not os.path.exists(DEMO_DB):
        print(f"[ERROR] 演示库不存在: {DEMO_DB}")
        sys.exit(1)
    if not os.path.exists(OFFICIAL_DB):
        print(f"[ERROR] 正式库不存在: {OFFICIAL_DB}")
        sys.exit(1)

    demo = load_row(DEMO_DB)
    official = load_row(OFFICIAL_DB)
    if demo is None:
        print("[ERROR] 演示库 site_contents 无数据")
        sys.exit(1)

    demo_translations = demo.get("translations")
    try:
        demo_trans = json.loads(demo_translations) if demo_translations else {}
    except Exception:
        demo_trans = {}

    print(f"演示库: {DEMO_DB}")
    print(f"正式库: {OFFICIAL_DB}")
    print(f"演示库 translations 语言: {list(demo_trans.keys())}")

    # 计算合并后的字段
    merged = {}
    for col in CONTENT_COLS:
        if col == "hero":
            off_hero = official.get("hero") if official else None
            merged[col] = merge_hero(demo.get("hero"), off_hero)
        else:
            merged[col] = demo.get(col)

    # 预览
    for col in CONTENT_COLS:
        try:
            j = json.loads(merged[col]) if merged[col] else None
        except Exception:
            j = merged[col]
        if col == "hero" and isinstance(j, dict):
            print(f"  -> {col}: slides={len(j.get('slides') or [])}")
        elif isinstance(j, list):
            print(f"  -> {col}: list[{len(j)}]")
        elif isinstance(j, dict):
            print(f"  -> {col}: keys={list(j.keys())[:8]}")
        else:
            print(f"  -> {col}: {str(merged[col])[:40]}")
    print(f"  -> translations: langs={list(demo_trans.keys())}")

    if args.dry_run:
        print("\n[DRY-RUN] 未写入。去掉 --dry-run 以执行。")
        return

    con = sqlite3.connect(OFFICIAL_DB)
    cur = con.cursor()
    added = ensure_translations_col(con)
    if added:
        print("[INFO] 已为正式库增加 translations 列")

    # 生成 SET 子句
    set_cols = CONTENT_COLS + ["translations", "updated_at"]
    set_sql = ", ".join(f"{c}=?" for c in set_cols)
    values = [merged[c] for c in CONTENT_COLS] + [
        json.dumps(demo_trans, ensure_ascii=False),
        _dt.datetime.now(_dt.timezone.utc).isoformat(),
    ]

    if official is None:
        cols_sql = ", ".join(["id"] + set_cols)
        placeholders = ", ".join(["1"] + ["?"] * len(set_cols))
        cur.execute(f"INSERT INTO site_contents ({cols_sql}) VALUES ({placeholders})", values)
        print("[INFO] 正式库无记录，已插入")
    else:
        cur.execute(f"UPDATE site_contents SET {set_sql} WHERE id=1", values)
        print("[INFO] 已更新正式库 site_contents(id=1)")

    con.commit()
    con.close()
    print("[OK] 迁移完成。")


if __name__ == "__main__":
    main()
