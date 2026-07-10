@echo off
REM ============================================================
REM  HiveWeave Frontend Startup Script
REM  Usage: start-frontend.bat
REM ============================================================

REM Node 22 required (>=22 <24). System default may be Node 24 which is incompatible.
set "NODE22=%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64"
if exist "%NODE22%\node.exe" set "PATH=%NODE22%;%PATH%"

echo [HiveWeave] Starting frontend...
echo [HiveWeave] Working dir: %~dp0apps\web
node --version
echo.

REM Kill any stale vite/node processes from previous runs.
REM Prevents port exhaustion (5173+ all occupied by orphan instances).
echo [HiveWeave] Cleaning up stale node processes...
taskkill /F /IM node.exe >nul 2>&1
timeout /t 1 /nobreak >nul

cd /d "%~dp0apps\web"
npm run dev
