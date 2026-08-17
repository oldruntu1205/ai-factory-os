#!/bin/bash
# 工业 AI 员工平台 - 生产部署脚本（宝塔/Docker）
set -e

echo "🚀 工业 AI 员工平台 - 启动脚本"
cd "$(dirname "$0")/.."

# 1. 检查 .env
if [ ! -f .env ]; then
  echo "⚠️  未找到 .env，从示例复制（请编辑 LLM_API_KEY）"
  cp backend/.env.example .env
fi

# 2. 构建并启动
docker compose up -d --build

# 3. 等待健康检查
sleep 5
curl -s http://localhost:8000/health && echo "" && echo "✅ 部署完成"
echo "   前端: http://localhost:3000"
echo "   API:  http://localhost:8000/docs"
