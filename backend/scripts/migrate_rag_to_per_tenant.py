"""
scripts/migrate_rag_to_per_tenant.py —— 共享集合 -> 每租户独立集合 迁移

背景：旧版 RAG 把全租户向量存在单一共享集合 company_docs（靠 payload.company_id 过滤）。
本脚本将其按 company_id 拆分到 company_docs_{company_id}（物理隔离）。

安全约束（务必遵守）：
  1. 默认只迁移、不删旧集合。必须显式 --confirm-delete 才会删除旧集合。
  2. 删旧集合前逐租户校验「新集合点数 >= 旧点数」，任一失败则中止删除，防数据丢失。
  3. 迁移期间旧集合仍可能被写入（upload 实时走旧集合），会造成增量丢失。
     -> 请在维护态（暂停 upload）下运行，或运行后再做一次末次增量补同步。

运行（在 backend/ 目录下）：
  python scripts/migrate_rag_to_per_tenant.py            # 仅迁移 + 校验，保留旧集合
  python scripts/migrate_rag_to_per_tenant.py --confirm-delete   # 校验通过后删旧集合
"""
import argparse

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config import QDRANT_HOST, QDRANT_PORT, QDRANT_COLLECTION

OLD = QDRANT_COLLECTION
PREFIX = QDRANT_COLLECTION


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-delete", action="store_true",
                    help="校验全部通过后删除旧集合（默认仅迁移不删除）")
    ap.add_argument("--batch", type=int, default=500,
                    help="scroll 每批条数")
    args = ap.parse_args()

    c = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # 1) 旧集合必须存在
    try:
        c.get_collection(OLD)
    except Exception:
        print(f"[skip] 旧集合 {OLD} 不存在，无需迁移")
        return

    # 2) 滚动读取 + 按 company_id 分桶
    buckets = {}
    offset = None
    while True:
        pts, offset = c.scroll(collection_name=OLD, offset=offset, limit=args.batch)
        if not pts:
            break
        for p in pts:
            cid = p.payload.get("company_id") if p.payload else None
            if cid is None:
                continue
            buckets.setdefault(cid, []).append(p)
        if offset is None:
            break

    if not buckets:
        print(f"[skip] 旧集合 {OLD} 无带 company_id 的向量，无需迁移")
        return

    # 3) 逐租户迁移
    report = {}
    for cid, group in buckets.items():
        name = f"{PREFIX}_{cid}"
        try:
            c.get_collection(name)
        except Exception:
            dim = len(group[0].vector)
            c.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
            )
        c.upsert(collection_name=name, points=group)
        new_count = c.count(name).count
        report[cid] = (len(group), new_count)
        ok = new_count >= len(group)
        print(f"[{'OK' if ok else 'FAIL'}] company {cid}: migrated {len(group)} -> {new_count}")

    total = len(report)
    all_ok = all(nc >= oc for oc, nc in report.values())

    # 4) 删除旧集合（仅确认且全量校验通过）
    if not args.confirm_delete:
        print(f"[verify-only] 共迁移 {total} 个租户；旧集合 {OLD} 保留。"
              f"确认无误后加 --confirm-delete 删除。")
        return
    if not all_ok:
        print("[ABORT] 存在校验失败租户，拒绝删除旧集合以防数据丢失")
        return
    c.delete_collection(OLD)
    print(f"[done] 旧集合 {OLD} 已删除；{total} 个租户迁移完成")


if __name__ == "__main__":
    main()
