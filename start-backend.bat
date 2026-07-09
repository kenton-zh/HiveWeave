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

REM 直接使用 .venv 的 python.exe 启动 uvicorn（不经过 activate.bat）
REM activate.bat 会触发 TRAE 沙箱对工作目录树之外的写入限制，
REM 导致创建项目时无法在用户工作空间创建 .hiveweave 目录（WinError 5）。
REM 直接 python.exe 启动无此限制。
.venv\Scripts\python.exe -m uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000 --workers 1 ^
    --limit-concurrency 100 ^
    --backlog 2048 ^
    --timeout-keep-alive 30
