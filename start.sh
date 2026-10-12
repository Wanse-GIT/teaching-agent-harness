#!/bin/bash
# ============================================================
# 智能导学 Agent (Harness) 启动脚本
# ============================================================

set -e

cd "$(dirname "$0")"

echo "======================================"
echo "  智能导学 Agent (Harness) 启动"
echo "======================================"

# 1. 检查 .env
if [ ! -f .env ]; then
    echo "⚠️  未找到 .env，正在从模板创建..."
    cp .env.example .env
    echo "   请编辑 .env 填入 LLM_API_KEY 后重新运行"
    echo "   当前将以『未配置LLM』模式启动（仅展示界面）"
fi

# 2. 可选：启动 PostgreSQL（记忆持久化）
if command -v docker &> /dev/null; then
    echo "▶ 启动 PostgreSQL（记忆持久化）..."
    docker compose up -d 2>/dev/null || docker-compose up -d || echo "   ⚠️ Docker 启动失败，将使用纯内存模式"
else
    echo "⚠️  未安装 Docker，三层记忆将使用纯内存模式"
fi

# 3. 安装依赖（如需）
if [ "$1" == "--install" ]; then
    echo "▶ 安装依赖..."
    pip install -r requirements.txt
fi

# 4. 启动服务
echo "▶ 启动 FastAPI 服务 (http://localhost:8000)..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
