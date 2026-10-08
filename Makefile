.PHONY: start stop bg status logs help

help:           ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

start:          ## 一键启动(前台,API 日志输出到终端)
	@bash start.sh

bg:             ## 一键启动(后台,日志写入 .resumetrace.log)
	@bash start.sh --bg

stop:           ## 停止所有服务(RAGFlow + API)
	@bash start.sh --stop

status:         ## 查看服务状态
	@echo "── RAGFlow ──"
	@docker compose -f deploy/docker-compose.yml --profile cpu ps 2>/dev/null || echo "  未运行"
	@echo ""
	@echo "── API ──"
	@pgrep -f "uvicorn server.api.main:app" >/dev/null && echo "  PID $$(pgrep -f 'uvicorn server.api.main:app')" || echo "  未运行"

logs:           ## 查看后台 API 日志
	@tail -f .resumetrace.log
