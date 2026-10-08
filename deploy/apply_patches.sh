#!/bin/bash
# 把 ragflow-fork 的补丁文件注入运行中的 ragflow 容器并重启。
# 容器一旦 recreate(改 compose/镜像)就必须重跑本脚本。
set -e
cd "$(dirname "$0")/.."
CONTAINER=${1:-deploy-ragflow-cpu-1}
docker cp ragflow-fork/rag/app/resume.py        "$CONTAINER":/ragflow/rag/app/resume.py
docker cp ragflow-fork/rag/llm/embedding_model.py "$CONTAINER":/ragflow/rag/llm/embedding_model.py
docker cp ragflow-fork/rag/llm/chat_model.py      "$CONTAINER":/ragflow/rag/llm/chat_model.py
echo "补丁已注入 $CONTAINER,重启中..."
cd deploy && docker compose -f docker-compose.yml restart ragflow-cpu
echo "完成。patches: 0000-baseline-repair-llmbundle, 0001-embedding-batch-configurable, 0002-evidence-mode, 0003-kimi-k3-temperature"
