@echo off
REM ============================================================
REM  HiveWeave Backend Startup Script (Python/FastAPI)
REM  Usage: start-backend.bat
REM  Backend: http://localhost:4000
REM
REM  Reads HIVEWEAVE_OPENCODE_API_KEY from apps/hiveweave-py/.env
REM ============================================================

echo [HiveWeave] Starting backend (Python/FastAPI)...
echo [HiveWeave] Working dir: %~dp0apps\hiveweave-py
echo.

cd /d "%~dp0apps\hiveweave-py"

REM Activate virtualenv (uvicorn installed inside)
call .venv\Scripts\activate.bat

REM Start FastAPI via uvicorn (port 4000, matches frontend proxy)
REM BUG-035 fix: increase concurrency limits to prevent 502 under LLM streaming load.
REM Single-worker architecture (agents share in-memory state), so we tune
REM connection handling rather than spawning multiple processes.
REM IMPORTANT: do NOT add --limit-max-requests here. That flag terminates the
REM process after N requests, which silently kills the backend mid-run.
REM The single worker keeps agents' in-memory state alive for the whole
REM server lifetime.
uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000 --workers 1 ^
    --limit-concurrency 100 ^
    --backlog 2048 ^
    --timeout-keep-alive 30
