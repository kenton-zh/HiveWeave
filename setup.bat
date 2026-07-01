@echo off
REM ============================================================
REM  HiveWeave Environment Setup (first-time)
REM  Run this once after cloning the repo.
REM ============================================================

echo ============================================
echo   HiveWeave Environment Setup
echo ============================================
echo.

REM --- Check Erlang ---
where erl >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Erlang/OTP not found in PATH.
    echo.
    echo Please install Erlang/OTP 26 from:
    echo   https://www.erlang.org/downloads
    echo.
    echo After installing, add the bin folder to your PATH.
    echo Example: set PATH=C:\Program Files\erl-26.0\bin;%%PATH%%
    echo.
    pause
    exit /b 1
)
echo [OK] Erlang found

REM --- Check Elixir ---
where mix >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Elixir not found in PATH.
    echo.
    echo Please install Elixir from:
    echo   https://elixir-lang.org/install.html
    echo.
    pause
    exit /b 1
)
echo [OK] Elixir found

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

REM --- Install Elixir dependencies ---
echo.
echo [INFO] Installing Elixir dependencies...
cd /d "%~dp0apps\hiveweave"
mix deps.get
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Elixir deps.
    pause
    exit /b 1
)
echo [OK] Elixir dependencies installed

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
