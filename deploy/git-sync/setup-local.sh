#!/bin/bash
# ============================================================
# 本地端脚本：初始化 git（如需要）+ 首次提交 + 关联远程 + 首推
#
# 用法：
#   bash deploy/git-sync/setup-local.sh            # 仅 init + 首次提交（不加远程）
#   bash deploy/git-sync/setup-local.sh prod root@<PROD_DOMAIN_REMOVED>:/www/wwwroot/<站点目录>.git
#
# 说明：
#   - 仅后端代码进入自动同步；前端不在此范围（前端走 deploy-prod.sh 独立发布）。
#   - 首次推送会触发生产 post-receive 自动 checkout + 重启后端。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REMOTE="${1:-prod}"
REMOTE_URL="${2:-}"

cd "$REPO_ROOT"

# 1) 初始化 git 仓库（如不存在）
if [ ! -d .git ]; then
  echo "==> git init"
  git init -q
fi

# 2) 确保根 .gitignore 存在（缺省从模板复制）
if [ ! -f .gitignore ]; then
  echo "==> 创建根 .gitignore（来自模板）"
  cp "$SCRIPT_DIR/root.gitignore" .gitignore
fi

# 3) 首次提交（git add -A，受 .gitignore 约束）
if [ -z "$(git log --oneline -1 2>/dev/null)" ]; then
  echo "==> 首次提交（git add -A，已按 .gitignore 排除 .env/venv/*.db 等）"
  git add -A
  git commit -q -m "init: 制造业AI员工平台（含RAG 10项修复）"
  echo "    ✅ 本地初始提交完成"
else
  echo "==> 已有提交，跳过 init commit"
fi

# 4) 统一主分支名为 main
git branch -M main 2>/dev/null || true

# 5) 关联远程并首推（未提供 URL 时只完成本地初始化）
if [ -z "$REMOTE_URL" ]; then
  echo ""
  echo "ℹ️  未提供远程地址，仅完成本地初始化。"
  echo "    服务器执行 setup-prod.sh 后，再运行："
  echo "    bash deploy/git-sync/setup-local.sh $REMOTE root@<host>:/www/wwwroot/<站点目录>.git"
  exit 0
fi

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote add "$REMOTE" "$REMOTE_URL"
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> 推送 $REMOTE/$BRANCH（将触发生产自动部署）"
git push -u "$REMOTE" "$BRANCH"
echo "✅ 推送完成，生产端 post-receive 会自动 checkout + 重启后端。"
