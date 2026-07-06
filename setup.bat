@echo off
REM ============================================================
REM  HiveWeave Environment Setup (first-time)
REM  Run this once after cloning the repo.
REM ============================================================

echo ============================================
echo   HiveWeave Environment Setup
echo ============================================
echo.

REM --- Check Python ---
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH.
    echo.
    echo Please install Python 3.12+ from:
    echo   https://www.python.org/downloads/
    echo.
    echo After installing, ensure Python is in your PATH.
    echo.
    pause
    exit /b 1
)
echo [OK] Python found

REM --- Check uv ---
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing uv...
    pip install uv
)
echo [OK] uv ready

REM --- Check Node.js ---
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js not found.
    echo Please install Node.js 22 from https://nodejs.org/
    pause
    exit /b 1
)
echo [OK] Node.js found

REM --- Install pnpm if missing ---
where pnpm >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing pnpm...
    npm install -g pnpm
)
echo [OK] pnpm ready

REM --- Install Python backend dependencies ---
echo.
echo [INFO] Installing Python backend dependencies...
cd /d "%~dp0apps\hiveweave-py"
uv sync
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python deps.
    pause
    exit /b 1
)
echo [OK] Python backend dependencies installed

REM --- Install frontend dependencies ---
echo.
echo [INFO] Installing frontend dependencies...
cd /d "%~dp0apps\web"
pnpm install
if %errorlevel% neq 0 (
    echo [WARN] pnpm install failed, trying npm...
    npm install
)
echo [OK] Frontend dependencies installed

REM --- Build frontend ---
echo.
echo [INFO] Building frontend...
pnpm build 2>nul || npm run build 2>nul
echo [OK] Frontend built

echo.
echo ============================================
echo   Setup Complete!
echo ============================================
echo.
echo To start the project, run: start-all.bat
echo   Backend:  http://localhost:4000
echo   Frontend: http://localhost:5173
echo.
pause
