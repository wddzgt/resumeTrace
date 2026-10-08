#!/bin/bash
# ResumeTrace 一键启动:RAGFlow(Docker) + 补丁注入 + API 服务
# 用法:
#   ./start.sh          # 前台启动(API 日志输出到终端,Ctrl+C 停止)
#   ./start.sh --bg     # 后台启动(API 日志写入 .resumetrace.log)
#   ./start.sh --stop   # 停止所有服务
set -e
cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[RT]${NC} $*"; }
warn()  { echo -e "${YELLOW}[RT]${NC} $*"; }
error() { echo -e "${RED}[RT]${NC} $*" >&2; }

# ── stop ─────────────────────────────────────────────────────────
if [ "$1" = "--stop" ]; then
    info "停止 API 服务..."
    pkill -f "uvicorn server.api.main:app" 2>/dev/null && info "API 已停止" || warn "API 未在运行"
    info "停止 RAGFlow 容器..."
    (cd deploy && docker compose --profile cpu stop)
    info "全部已停止"
    exit 0
fi

BG=false
[ "$1" = "--bg" ] && BG=true

# ── 1. 检查依赖 ─────────────────────────────────────────────────
for cmd in docker python3; do
    command -v $cmd &>/dev/null || { error "缺少依赖: $cmd"; exit 1; }
done
[ -f requirements.txt ] || { error "找不到 requirements.txt,请在项目根目录运行"; exit 1; }

# ── 2. 启动 RAGFlow ────────────────────────────────────────────
if docker compose -f deploy/docker-compose.yml --profile cpu ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -q 'running'; then
    info "RAGFlow 已在运行"
else
    info "启动 RAGFlow (首次启动需拉取镜像,请耐心等待)..."
    (cd deploy && docker compose --profile cpu up -d)
fi

# ── 3. 等待 RAGFlow 就绪 ───────────────────────────────────────
info "等待 RAGFlow 就绪..."
RAGFLOW_URL="${RAGFLOW_BASE_URL:-http://localhost:9380}"
for i in $(seq 1 60); do
    if curl -sf "$RAGFLOW_URL/v1/health" -o /dev/null 2>/dev/null || \
       curl -sf "$RAGFLOW_URL" -o /dev/null 2>/dev/null; then
        info "RAGFlow 就绪 (${i}s)"
        break
    fi
    [ "$i" -eq 60 ] && { error "RAGFlow 启动超时(60s),请检查 docker logs deploy-ragflow-cpu-1"; exit 1; }
    sleep 1
done

# ── 4. 注入补丁 ────────────────────────────────────────────────
CONTAINER=$(docker compose -f deploy/docker-compose.yml --profile cpu ps --format '{{.Name}}' 2>/dev/null | head -1)
if [ -z "$CONTAINER" ]; then
    error "找不到 RAGFlow 容器"
    exit 1
fi
info "注入补丁到容器 $CONTAINER ..."
docker cp ragflow-fork/rag/app/resume.py          "$CONTAINER":/ragflow/rag/app/resume.py
docker cp ragflow-fork/rag/llm/embedding_model.py  "$CONTAINER":/ragflow/rag/llm/embedding_model.py
docker cp ragflow-fork/rag/llm/chat_model.py       "$CONTAINER":/ragflow/rag/llm/chat_model.py
(cd deploy && docker compose --profile cpu restart ragflow-cpu)
sleep 3
info "补丁已注入(0000~0003)"

# ── 5. 安装 Python 依赖 ────────────────────────────────────────
info "检查 Python 依赖..."
pip install -q -r requirements.txt

# ── 6. 启动 API ────────────────────────────────────────────────
PORT=${RESUMETRACE_PORT:-8000}
if $BG; then
    LOGFILE=".resumetrace.log"
    nohup python3 -m uvicorn server.api.main:app --host 0.0.0.0 --port "$PORT" > "$LOGFILE" 2>&1 &
    echo $! > .resumetrace.pid
    info "API 后台启动 (PID $(cat .resumetrace.pid), 端口 $PORT, 日志 $LOGFILE)"
    info "打开 http://localhost:$PORT/ui/"
else
    info "启动 API (端口 $PORT),Ctrl+C 停止..."
    info "打开 http://localhost:$PORT/ui/"
    python3 -m uvicorn server.api.main:app --host 0.0.0.0 --port "$PORT"
fi
