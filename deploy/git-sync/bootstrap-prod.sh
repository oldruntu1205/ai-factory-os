#!/bin/bash
# ============================================================
# 生产端一键引导（本地开发机执行）：
#   1) 把本 git-sync 目录上传到服务器
#   2) 在服务器上自动执行 setup-prod.sh（建 bare 仓库 + 装 post-receive 钩子）
#
# 用法：
#   bash bootstrap-prod.sh <user@host> [CODE_DIR] [BRANCH]
#   例： bash bootstrap-prod.sh root@<生产主机> /www/wwwroot/<站点目录> main
#
# 前置：先在服务器整盘备份生产代码（必做，见 README_SYNC.md 快速开始）：
#   cp -a /www/wwwroot/<站点目录> /www/wwwroot/<站点目录>.bak.$(date +%s)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${1:?用法: bash bootstrap-prod.sh <user@host> [CODE_DIR] [BRANCH]}"
CODE_DIR="${2:-/www/wwwroot/<站点目录>}"
BRANCH="${3:-main}"
REMOTE_DIR="/root/git-sync"

echo "==> [1/2] 上传 git-sync 脚本到 $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR"
scp -r "$SCRIPT_DIR/." "$HOST:$REMOTE_DIR/"

echo "==> [2/2] 远端执行 setup-prod.sh $CODE_DIR $BRANCH"
ssh "$HOST" "bash $REMOTE_DIR/setup-prod.sh '$CODE_DIR' '$BRANCH'"

echo ""
echo "✅ 生产端已就绪。回到本地开发机继续："
echo "   bash deploy/git-sync/setup-local.sh prod $HOST:$CODE_DIR.git"
echo "   bash deploy/git-sync/start-watch.sh start   # 可选，方案③"
