@echo off
REM ============================================================
REM  HiveWeave Backend Startup Script (Python/FastAPI)
REM  Usage: start-backend.bat
REM  Backend: http://localhost:4000
REM
REM  Reads HIVEWEAVE_OPENCODE_API_KEY from apps/hiveweave-py/.env
REM  Logs to tasks\backend-YYYYMMDD-HHMMSS.output (JSON lines)
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

REM Log directory at repo root (CLAUDE.md expects tasks/*.output)
if not exist "%~dp0tasks" mkdir "%~dp0tasks"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set TS=%%i
set "LOGFILE=%~dp0tasks\backend-%TS%.output"
echo [HiveWeave] Logging to %LOGFILE%

cd /d "%~dp0apps\hiveweave-py"

set HIVEWEAVE_LOG_JSON=1
set HIVEWEAVE_LOG_LEVEL=INFO

REM Use .venv python.exe directly (skip activate.bat to avoid TRAE sandbox
REM write restrictions that block .hiveweave dir creation in user workspace).
.venv\Scripts\python.exe -m uvicorn hiveweave.main:app --host 127.0.0.1 --port 4000 --workers 1 ^
    --limit-concurrency 100 ^
    --backlog 2048 ^
    --timeout-keep-alive 30 >> "%LOGFILE%" 2>&1
