#!/bin/bash
# ============================================================
# 生产端一次性部署脚本：创建 bare 仓库 + 安装 post-receive 钩子
#
# 用法：
#   bash setup-prod.sh [CODE_DIR] [BRANCH]
#   例： bash setup-prod.sh /www/wwwroot/<站点目录> main
#
# 前置（务必先执行，见 GIT_SYNC_GUIDE.md）：
#   1) 本地已能 SSH 免密登录到本机（root@<PROD_DOMAIN_REMOVED>）
#   2) 已整盘备份现有生产代码：
#      cp -a /www/wwwroot/<站点目录> /www/wwwroot/<站点目录>.bak.<时间戳>
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${1:-/www/wwwroot/<站点目录>}"
GIT_DIR="$CODE_DIR.git"
BRANCH="${2:-main}"
PY="$CODE_DIR/backend/venv/bin/python"
SERVICES="ai-factory-backend ai-factory-worker"

echo "==> 创建 bare 仓库: $GIT_DIR"
mkdir -p "$GIT_DIR"
git init --bare "$GIT_DIR" >/dev/null

echo "==> 安装 post-receive 钩子"
cp "$SCRIPT_DIR/post-receive" "$GIT_DIR/hooks/post-receive"
chmod +x "$GIT_DIR/hooks/post-receive"

echo "==> 写入部署配置: $GIT_DIR/deploy-config.sh"
cat > "$GIT_DIR/deploy-config.sh" <<EOF
WORK_TREE=$CODE_DIR
GIT_DIR=$GIT_DIR
BRANCH=$BRANCH
PY=$PY
SERVICES="$SERVICES"
EOF

echo ""
echo "✅ 生产 git 同步已就绪。"
echo "   回到本地开发机执行（替换为你真实的服务器地址）："
echo "   bash deploy/git-sync/setup-local.sh prod root@<PROD_DOMAIN_REMOVED>:$GIT_DIR"
