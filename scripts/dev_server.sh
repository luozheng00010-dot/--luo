#!/usr/bin/env bash
# 本地开发/运行启动脚本（计划 P1-01：README 即可启动，无需改源码）
# 用法：scripts/dev_server.sh [端口号]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-8765}"

# 启动本地 Worker（独立进程，计划 3.2）
cd "$ROOT/backend"
AUTOEDITOR_PORT="$PORT" uv run python -m app.workers.worker &
WORKER_PID=$!

AUTOEDITOR_PORT="$PORT" uv run python run_server.py &
SERVER_PID=$!

trap 'kill $SERVER_PID $WORKER_PID 2>/dev/null || true' EXIT
wait "$SERVER_PID"
