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
uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000
