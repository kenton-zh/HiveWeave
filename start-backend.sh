#!/bin/bash
# start-backend.sh — Linux/macOS 版后端启动脚本
# 行为对齐 start-backend.bat: kill 4000 端口残留进程 + 启动 uvicorn

set -e

PORT=4000
APP_DIR="$(cd "$(dirname "$0")" && pwd)/apps/hiveweave-py"
LOG_FILE="$APP_DIR/backend.log"
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
echo "[start-backend.sh] Starting uvicorn at port $PORT ..."
nohup uv run uvicorn hiveweave.main:app --host 0.0.0.0 --port $PORT \
  > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"
echo "[start-backend.sh] Started (PID $NEW_PID). Log: $LOG_FILE"
