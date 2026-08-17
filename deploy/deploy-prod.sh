#!/bin/bash
# ============================================================
# 工业 AI 员工平台 - 生产部署脚本
# 用法: 在服务器部署根目录（默认 /opt/industrial-ai）执行
#   bash deploy/deploy-prod.sh
# 前置: 已安装 Docker + docker compose 插件，且代码已就位
# ============================================================
set -e

# 部署根目录（绝对路径，按你服务器实际位置修改）
DEPLOY_ROOT="/opt/industrial-ai"
cd "$DEPLOY_ROOT"

echo "🚀 [1/6] 拉取最新代码"
git pull --ff-only 2>/dev/null || echo "（无 git 或已是最新，跳过）"

echo "🚀 [2/6] 准备生产环境变量 (.env)"
if [ ! -f "$DEPLOY_ROOT/.env" ]; then
  cp deploy/prod.env "$DEPLOY_ROOT/.env"
  echo "⚠️  已生成 $DEPLOY_ROOT/.env，请编辑其中 <占位符> 项（尤其是 SECRET_KEY / LLM_API_KEY）后重新运行本脚本"
  exit 1
fi
# 简单校验：SECRET_KEY 仍为占位符则中止，避免用弱密钥上线
if grep -q "SECRET_KEY=<" "$DEPLOY_ROOT/.env"; then
  echo "❌ 错误：SECRET_KEY 仍是占位符，请先编辑 $DEPLOY_ROOT/.env"
  exit 1
fi

echo "🚀 [3/6] 构建并启动服务"
docker compose build
docker compose up -d

echo "🚀 [4/6] 等待后端健康 (最多 90s)"
for i in $(seq 1 45); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health || true)
  if [ "$code" = "200" ]; then
    echo "✅ 后端就绪 (HTTP 200)"
    break
  fi
  sleep 2
done
[ "$code" = "200" ] || { echo "❌ 后端未在预期时间内就绪，请查看: docker compose logs backend"; exit 1; }

echo "🚀 [5/6] 同步 Alembic 迁移基线（幂等）"
docker compose exec -T backend alembic upgrade head || echo "（alembic 已在启动时执行，跳过）"

echo "🚀 [6/6] 部署完成 ✅"
echo "   前端容器: http://127.0.0.1:3000   (由宿主 Nginx 反代到域名)"
echo "   后端 API: http://127.0.0.1:8000"
echo "   API 文档: http://127.0.0.1:8000/docs"
echo ""
echo "下一步: 配置宿主 Nginx (deploy/nginx.conf) + SSL，并将域名解析到本机"
