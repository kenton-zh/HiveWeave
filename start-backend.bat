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

REM Kill any stale process on port 4000 (clean restart)
echo [HiveWeave] Killing stale backend on port 4000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":4000 " ^| findstr "LISTENING"') do (
    echo   killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

cd /d "%~dp0apps\hiveweave-py"

REM Use .venv python.exe directly (skip activate.bat to avoid TRAE sandbox
REM write restrictions that block .hiveweave dir creation in user workspace).
.venv\Scripts\python.exe -m uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000 --workers 1 ^
    --limit-concurrency 100 ^
    --backlog 2048 ^
    --timeout-keep-alive 30
