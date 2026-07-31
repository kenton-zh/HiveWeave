#!/bin/bash
# start-frontend.sh — Linux/macOS frontend starter (HiveWeave UI on :5173)
# Kills by PORT (not global pkill vite/node — that kills project app servers).
# vite.config.ts has strictPort:true, so if 5173 can't be freed this script
# fails fast instead of silently creeping to 5174/5175/...

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$ROOT/apps/web"
LOG_FILE="$PROJECT_DIR/frontend.log"

if [ ! -d "$PROJECT_DIR" ]; then
  echo "[start-frontend.sh] ERROR: $PROJECT_DIR not found"
  exit 1
fi

# Kill any stale process LISTENING on port 5173 (clean restart).
# Port-based kill is reliable; the old pidfile approach left orphan Vite
# instances piling up on 5174/5175/... when the previous vite crashed or
# was kill -9'd without cleaning the pidfile.
echo "[start-frontend.sh] Killing stale frontend on port 5173..."
if command -v lsof >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -ti:5173 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    for p in $OLD_PIDS; do
      echo "  killing PID $p"
      kill -9 "$p" 2>/dev/null || true
    done
    sleep 1
  fi
elif command -v fuser >/dev/null 2>&1; then
  # fuser fallback (some Linux distros without lsof).
  # fuser prints "5173/tcp: <pids>"; parsing is fragile (a naive tr -d ' '
  # would merge multiple PIDs into one garbage token), so use -k to kill
  # directly. -k sends SIGKILL on most platforms; port gets freed either way.
  if fuser -k 5173/tcp 2>/dev/null; then
    sleep 1
  fi
else
  echo "[start-frontend.sh] WARNING: neither lsof nor fuser found; cannot kill stale 5173"
fi

cd "$PROJECT_DIR"
echo "[start-frontend.sh] Starting HiveWeave Vite on port 5173 (strictPort — will fail if port busy)..."
nohup npm run dev > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "[start-frontend.sh] Started (PID $NEW_PID). Log: $LOG_FILE"
