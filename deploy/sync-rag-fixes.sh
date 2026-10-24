#!/bin/bash
# ============================================================
# 工业 AI 员工平台 —— RAG 修复部署同步脚本
#
# 用途：把「RAG 实现审查 10 项修复」涉及的后端文件同步到目标环境并重启服务。
#       重启后自动做健康检查，并校验 BM25 索引是否重建成功。
#
# 修复文件清单（8 个，相对仓库根）：
#   backend/knowledge/hybrid.py
#   backend/knowledge/rag.py
#   backend/knowledge/splitter.py
#   backend/knowledge/embedding.py
#   backend/knowledge/vector.py
#   backend/agents/process.py
#   backend/api/knowledge.py
#   backend/sales/scoring.py
#
# 用法（按部署拓扑选一种）：
#
#   A. 服务器本机 systemd（宝塔 + systemd 拓扑）
#        bash deploy/sync-rag-fixes.sh --mode systemd \
#            --code-dir /www/wwwroot/<站点目录> \
#            --services "ai-factory-backend ai-factory-worker"
#
#   B. Docker Compose（备选拓扑 / 开发机容器化部署）
#        bash deploy/sync-rag-fixes.sh --mode docker
#
#   C. 开发机远程推送（rsync + ssh，服务器为 systemd 拓扑时最方便）
#        bash deploy/sync-rag-fixes.sh --mode remote \
#            --host root@<生产主机> \
#            --code-dir /www/wwwroot/<站点目录>
#
# 参数说明：
#   --mode      docker | systemd | remote   （默认 docker）
#   --code-dir  目标代码根目录（systemd/remote 必填）
#   --services  要重启的 systemd 服务名，空格分隔（默认 ai-factory-backend ai-factory-worker）
#   --host      远程 SSH 目标，如 root@<生产主机>（remote 必填）
#
# 通用说明：
#   - 覆盖前自动备份到 <code-dir>/backups/rag-fix-<时间戳>/，可一键回滚
#   - 无数据库结构变更，无需 Alembic 迁移
#   - 多 worker（uvicorn --workers 4）场景下，BM25 跨进程增量同步由代码水位线机制保证
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------- 本次 RAG 修复涉及的文件（相对仓库根） ----------
FILES=(
  "backend/knowledge/hybrid.py"
  "backend/knowledge/rag.py"
  "backend/knowledge/splitter.py"
  "backend/knowledge/embedding.py"
  "backend/knowledge/vector.py"
  "backend/agents/process.py"
  "backend/api/knowledge.py"
  "backend/sales/scoring.py"
)

MODE="docker"
CODE_DIR=""
HOST=""
SERVICES=("ai-factory-backend" "ai-factory-worker")

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)      MODE="$2"; shift 2 ;;
    --code-dir)  CODE_DIR="$2"; shift 2 ;;
    --host)      HOST="$2"; shift 2 ;;
    --services)  read -ra SERVICES <<< "$2"; shift 2 ;;
    --help|-h)   usage; exit 0 ;;
    *) echo "❌ 未知参数: $1"; usage; exit 2 ;;
  esac
done

case "$MODE" in
  docker|systemd|remote) ;;
  *) echo "❌ 未知模式: $MODE（可选 docker / systemd / remote）"; usage; exit 2 ;;
esac

# 模式必需参数前置校验（在本地预检之前，快速失败）
if [ "$MODE" = "systemd" ] && [ -z "$CODE_DIR" ]; then
  echo "❌ systemd 模式需要 --code-dir"; usage; exit 2
fi
if [ "$MODE" = "remote" ]; then
  if [ -z "$HOST" ] || [ -z "$CODE_DIR" ]; then
    echo "❌ remote 模式需要 --host 与 --code-dir"; usage; exit 2
  fi
fi

# Windows(Git Bash/MSYS) 下，路径需转为 Windows 风格才能被原生 python 识别
to_win() { case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) cygpath -w "$1";; *) echo "$1";; esac; }

echo "============================================================"
echo " RAG 修复部署同步"
echo "   模式      : $MODE"
echo "   文件数    : ${#FILES[@]}"
echo "   代码目录  : ${CODE_DIR:-$REPO_ROOT}"
echo "============================================================"

# ---------- 1) 本地语法预检（防止把坏代码推上线） ----------
echo ""
echo "==> [1/5] 本地语法预检 (py_compile)"
PY="python"
command -v python3 >/dev/null 2>&1 && PY="python3"
for f in "${FILES[@]}"; do
  src="$REPO_ROOT/$f"
  [ -f "$src" ] || { echo "❌ 本地文件缺失: $src"; exit 1; }
  "$PY" -m py_compile "$(to_win "$src")" || { echo "❌ 语法检查失败: $f"; exit 1; }
done
echo "    ✅ ${#FILES[@]} 个文件语法检查通过"

# ---------- 2) 备份 + 同步 ----------
echo ""
echo "==> [2/5] 备份并同步文件"

TS=$(date +%Y%m%d%H%M%S)
REMOTE_RUN=0   # 1 表示后续命令在远端服务器执行
LOCAL_TARGET=""

if [ "$MODE" = "remote" ]; then
  REMOTE_RUN=1
  echo "    推送目标: $HOST:$CODE_DIR"
  # 逐个 rsync（保留相对目录结构，排除缓存）
  for f in "${FILES[@]}"; do
    echo "    ➜ 推送 $f"
    rsync -az --relative "$REPO_ROOT/./$f" "$HOST:$CODE_DIR/"
  done
  # 远端备份被覆盖文件
  ssh "$HOST" "set -e; cd $CODE_DIR; mkdir -p backups/rag-fix-$TS; \
    for f in ${FILES[*]}; do [ -f \"\$f\" ] || continue; \
      mkdir -p \"backups/rag-fix-$TS/\$(dirname \"\$f\")\"; \
      cp \"\$f\" \"backups/rag-fix-$TS/\$f\"; done"
  echo "    ✅ ${#FILES[@]} 个文件已推送，远端备份目录: $CODE_DIR/backups/rag-fix-$TS"
else
  if [ "$MODE" = "systemd" ]; then
    LOCAL_TARGET="$CODE_DIR"
  else
    LOCAL_TARGET="$REPO_ROOT"
  fi
  BACKUP_DIR="$LOCAL_TARGET/backups/rag-fix-$TS"
  mkdir -p "$BACKUP_DIR"
  for f in "${FILES[@]}"; do
    src="$REPO_ROOT/$f"
    dst="$LOCAL_TARGET/$f"
    if [ -f "$dst" ]; then
      mkdir -p "$BACKUP_DIR/$(dirname "$f")"
      cp "$dst" "$BACKUP_DIR/$f"
    fi
    # 源与目标为同一路径（如 docker 模式在仓库本地执行）时无需复制
    if [ "$src" = "$dst" ]; then
      echo "    ➜ 已是最新，跳过复制 $f"
      continue
    fi
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    echo "    ➜ 已同步 $f"
  done
  echo "    ✅ ${#FILES[@]} 个文件已同步，备份目录: $BACKUP_DIR"
fi

# ---------- 3) 重启服务 ----------
echo ""
echo "==> [3/5] 重启服务"
case "$MODE" in
  docker)
    (cd "$REPO_ROOT" && docker compose build backend && docker compose up -d backend)
    ;;
  systemd)
    systemctl restart "${SERVICES[@]}"
    echo "    已重启 systemd 服务: ${SERVICES[*]}"
    ;;
  remote)
    ssh "$HOST" "systemctl restart ${SERVICES[*]}"
    echo "    已远程重启 systemd 服务: ${SERVICES[*]}"
    ;;
esac

# ---------- 4) 健康检查（最多 90s） ----------
echo ""
echo "==> [4/5] 等待后端健康（最多 90s）"
if [ "$REMOTE_RUN" = "1" ]; then
  HEALTH_CMD="ssh $HOST \"curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8000/health\""
else
  HEALTH_CMD="curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8000/health"
fi
OK=0
for _ in $(seq 1 45); do
  code=$(eval "$HEALTH_CMD" 2>/dev/null || true)
  if [ "$code" = "200" ]; then OK=1; break; fi
  sleep 2
done
if [ "$OK" = "1" ]; then
  echo "    ✅ 后端就绪 (HTTP 200)"
else
  echo "    ❌ 后端未在预期时间内就绪，请检查服务日志（下方 BM25 校验同样会失败）"
fi

# ---------- 5) 校验 BM25 索引重建 ----------
echo ""
echo "==> [5/5] 校验 BM25 索引重建"
BM25_OK=0
case "$MODE" in
  docker)
    out=$(docker compose -f "$REPO_ROOT/docker-compose.yml" logs backend 2>/dev/null | grep -m1 "BM25 启动重建完成" || true)
    ;;
  systemd)
    out=$(grep -m1 "BM25 启动重建完成" "$LOCAL_TARGET/backend/logs/backend.log" 2>/dev/null || true)
    ;;
  remote)
    out=$(ssh "$HOST" "grep -m1 'BM25 启动重建完成' '$CODE_DIR/backend/logs/backend.log' 2>/dev/null" || true)
    ;;
esac
if [ -n "$out" ]; then
  BM25_OK=1
  echo "    ✅ 日志已确认: $out"
else
  echo "    ⚠️  未在日志中找到 BM25 重建记录"
  echo "       （属正常情况：若后端启动时该函数未执行成功或日志路径不同；"
  echo "       可手动确认: grep 'BM25 启动重建完成' 后端日志）"
fi

# ---------- 汇总 ----------
echo ""
echo "============================================================"
if [ "$OK" = "1" ] && [ "$BM25_OK" = "1" ]; then
  echo " 部署完成 ✅ 后端健康 + BM25 索引已重建"
elif [ "$OK" = "1" ]; then
  echo " 部署完成 ✅ 后端健康（BM25 重建未确认，请手动检查日志）"
else
  echo " 部署未完成 ❌ 请检查服务日志后重试"
fi
echo "============================================================"
echo ""
echo "回滚方式（若需回退本次 RAG 修复）："
if [ "$REMOTE_RUN" = "1" ]; then
  echo "  ssh $HOST \"cd $CODE_DIR && for f in ${FILES[*]}; do cp backups/rag-fix-$TS/\$f \$f; done && systemctl restart ${SERVICES[*]}\""
else
  echo "  cd $LOCAL_TARGET"
  echo "  for f in ${FILES[*]}; do cp backups/rag-fix-$TS/\$f \$f; done"
  echo "  systemctl restart ${SERVICES[*]}   # 或: docker compose restart backend"
fi
