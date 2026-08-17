#!/bin/bash
# 数据备份脚本（PostgreSQL + Qdrant 卷）
set -e
BACKUP_DIR="./backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "📦 备份数据库..."
docker compose exec postgres pg_dump -U industrial industrial_ai > "$BACKUP_DIR/db.sql"
echo "✅ 备份完成: $BACKUP_DIR"
