#!/bin/bash
# start-frontend.sh — Linux/macOS frontend starter (HiveWeave UI on :5173)
# Does NOT globally pkill vite/node — that kills project app servers.

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$ROOT/apps/web"
LOG_FILE="$PROJECT_DIR/frontend.log"
PID_FILE="$PROJECT_DIR/frontend.pid"

if [ ! -d "$PROJECT_DIR" ]; then
  echo "[start-frontend.sh] ERROR: $PROJECT_DIR not found"
  exit 1
fi

# Kill only our previous HiveWeave vite (pidfile), never all node/vite
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start-frontend.sh] Stopping previous HiveWeave frontend PID $OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

cd "$PROJECT_DIR"
echo "[start-frontend.sh] Starting HiveWeave Vite on port 5173 ..."
nohup npm run dev > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"
echo "[start-frontend.sh] Started (PID $NEW_PID). Log: $LOG_FILE"
