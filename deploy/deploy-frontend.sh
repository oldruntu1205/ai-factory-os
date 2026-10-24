#!/bin/bash
# ============================================================
# 前端构建 + 部署（拓扑 A：宝塔 + systemd / Nginx 静态托管）
#
# 用法：
#   bash deploy/deploy-frontend.sh root@<公网IP>             # 指定主机（必填）
#   bash deploy/deploy-frontend.sh root@<公网IP> /www/wwwroot/<站点目录>/frontend/dist
#
# 说明：
#   - 本地执行 npm run build，产出 frontend/dist/
#   - 仅上传 index.html + assets/（避开宝塔 dist/.user.ini 的 chattr +i 锁，无需先解锁）
#   - 上传工具：优先 rsync（增量 + --delete 清理旧 hashed 资源），否则回退 scp
#   - 本脚本在**本地开发机**执行（沙箱环境无法出站 SSH 到生产机）
#   - 前端不在 git 自动同步范围（git-sync 只管 backend），改前端必须手动跑本脚本
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# 生产主机与站点目录不入库，运行时通过参数或环境变量传入：
#   PROD_HOST=root@1.2.3.4 PROD_WEB_DIR=/www/wwwroot/<站点>/frontend/dist bash deploy/deploy-frontend.sh
HOST="${1:-${PROD_HOST:-}}"
REMOTE_DIR="${2:-${PROD_WEB_DIR:-/www/wwwroot/<站点目录>/frontend/dist}}"

if [ -z "$HOST" ]; then
  echo "错误：未指定部署主机。" >&2
  echo "用法：bash deploy/deploy-frontend.sh root@<公网IP> [远程目录]" >&2
  echo "  或：PROD_HOST=root@<公网IP> bash deploy/deploy-frontend.sh" >&2
  exit 1
fi
FRONT="$REPO_ROOT/frontend"
DIST="$FRONT/dist"

echo "============================================================"
echo " 前端构建 + 部署"
echo "   主机    : $HOST"
echo "   目标目录: $REMOTE_DIR"
echo "============================================================"

# ---------- 1) 构建 ----------
echo ""
echo "==> [1/3] npm run build"
if [ ! -d "$FRONT" ]; then
  echo "❌ 未找到 frontend 目录: $FRONT"; exit 1
fi
( cd "$FRONT" && npm run build )

[ -f "$DIST/index.html" ] || { echo "❌ dist/index.html 缺失，构建异常"; exit 1; }
[ -d "$DIST/assets" ]     || { echo "❌ dist/assets 缺失，构建异常"; exit 1; }
echo "   ✅ 构建产物就绪"
echo "   index.html: $(du -h "$DIST/index.html" | cut -f1)"
echo "   assets    : $(ls "$DIST/assets" | wc -l) 个文件 / $(du -sh "$DIST/assets" | cut -f1)"

# ---------- 2) 上传（仅 index.html + assets/，避开 .user.ini 锁） ----------
echo ""
echo "==> [2/3] 上传到 $HOST:$REMOTE_DIR"
if command -v rsync >/dev/null 2>&1; then
  echo "   使用 rsync（增量 + assets --delete 清理旧资源）"
  # index.html 不带 --delete（源是单文件，不能删远端其他内容）
  rsync -az "$DIST/index.html" "$HOST:$REMOTE_DIR/"
  # assets/ 带 --delete：远端只保留本次构建的 hashed 资源
  rsync -az --delete "$DIST/assets/" "$HOST:$REMOTE_DIR/assets/"
else
  echo "   使用 scp（未检测到 rsync）"
  scp "$DIST/index.html" "$HOST:$REMOTE_DIR/index.html"
  scp -r "$DIST/assets/." "$HOST:$REMOTE_DIR/assets/"
fi
echo "   ✅ 上传完成"

# ---------- 3) 验证 ----------
echo ""
echo "==> [3/3] 验证"
NEW_BUNDLE=$(ssh "$HOST" "grep -l '工艺/图纸助手' '$REMOTE_DIR/assets/'*.js 2>/dev/null | head -1")
if [ -n "$NEW_BUNDLE" ]; then
  echo "   ✅ 新构建已生效：$(basename "$NEW_BUNDLE") 含「工艺/图纸助手」角色"
else
  echo "   ⚠️  未在远端 assets 中找到「工艺/图纸助手」——请检查构建是否包含该角色"
fi
HTTP=$(ssh "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/ -H 'Host: ${PROD_DOMAIN:-localhost}'")
echo "   Nginx 首页响应: ${HTTP:-无}（期望 200）"

echo ""
echo "完成 ✅  若浏览器未更新，强制刷新（Ctrl+F5）即可。"
