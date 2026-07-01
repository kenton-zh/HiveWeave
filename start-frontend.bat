@echo off
REM ============================================================
REM  HiveWeave Frontend Startup Script
REM  Usage: start-frontend.bat
REM ============================================================

echo [HiveWeave] Starting frontend...
echo [HiveWeave] Working dir: %~dp0apps\web
echo.

cd /d "%~dp0apps\web"
npm run dev
