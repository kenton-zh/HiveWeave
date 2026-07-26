#!/bin/bash
# start-backend.sh — Linux/macOS 版后端启动脚本
# 行为对齐 start-backend.bat: kill 4000 端口残留进程 + 启动 uvicorn
# TEST21 M10: PYTHONUNBUFFERED + HIVEWEAVE_LOG_FILE durable tee

set -e

PORT=4000
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$ROOT_DIR/apps/hiveweave-py"
TASKS_DIR="$ROOT_DIR/tasks"
mkdir -p "$TASKS_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$TASKS_DIR/backend-$TS.output"
PID_FILE="$APP_DIR/backend.pid"

echo "[start-backend.sh] Killing any process on port $PORT ..."
# lsof or fuser, whichever is available
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti tcp:$PORT || true)
elif command -v fuser >/dev/null 2>&1; then
  PIDS=$(fuser $PORT/tcp 2>/dev/null || true)
else
  PIDS=""
fi

if [ -n "$PIDS" ]; then
  echo "[start-backend.sh] Killing PIDs: $PIDS"
  kill -9 $PIDS 2>/dev/null || true
fi

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start-backend.sh] Killing old PID from pidfile: $OLD_PID"
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

sleep 1

cd "$APP_DIR"
export PYTHONUNBUFFERED=1
export HIVEWEAVE_LOG_JSON=1
export HIVEWEAVE_LOG_LEVEL=INFO
export HIVEWEAVE_LOG_FILE="$LOG_FILE"

echo "[start-backend.sh] Starting uvicorn at port $PORT ..."
echo "[start-backend.sh] Log: $LOG_FILE"
# Durable log via HIVEWEAVE_LOG_FILE (flushing tee). Do not also >> the same
# path — that duplicates every structlog line. stdout discarded; uvicorn uses
# the logging config that tees into HIVEWEAVE_LOG_FILE.
nohup uv run python -u -m uvicorn hiveweave.main:app --host 0.0.0.0 --port $PORT \
  >/dev/null 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"
echo "[start-backend.sh] Started (PID $NEW_PID). Log: $LOG_FILE"
