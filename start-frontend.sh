#!/bin/bash
# start-frontend.sh — Linux/macOS 版前端启动脚本
# 行为对齐 start-frontend.bat: kill node 残留 + 启动 vite

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)/apps/hiveweave-ts"
LOG_FILE="$PROJECT_DIR/frontend.log"
PID_FILE="$PROJECT_DIR/frontend.pid"

echo "[start-frontend.sh] Killing any vite/node processes ..."
pkill -f "vite" 2>/dev/null || true
pkill -f "node.*5173" 2>/dev/null || true

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start-frontend.sh] Killing old PID from pidfile: $OLD_PID"
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

sleep 1

cd "$PROJECT_DIR"
echo "[start-frontend.sh] Starting vite dev server ..."
nohup npm run dev > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"
echo "[start-frontend.sh] Started (PID $NEW_PID). Log: $LOG_FILE"
