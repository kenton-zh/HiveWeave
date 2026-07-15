@echo off
REM ============================================================
REM  HiveWeave Frontend Startup Script
REM  Usage: start-frontend.bat
REM  Does NOT taskkill all node.exe (that kills project game servers).
REM ============================================================

set "NODE22=%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64"
if exist "%NODE22%\node.exe" set "PATH=%NODE22%;%PATH%"

echo [HiveWeave] Starting frontend...
echo [HiveWeave] Working dir: %~dp0apps\web
node --version
echo.

set "PID_FILE=%~dp0apps\web\frontend.pid"
if exist "%PID_FILE%" (
  set /p OLD_PID=<"%PID_FILE%"
  echo [HiveWeave] Stopping previous HiveWeave frontend PID %OLD_PID% if running...
  taskkill /F /PID %OLD_PID% >nul 2>&1
  del "%PID_FILE%" >nul 2>&1
)

cd /d "%~dp0apps\web"
echo [HiveWeave] Starting Vite on port 5173 (HiveWeave UI reserved port)...
echo [HiveWeave] Project apps must use start_dev_server / port 3000+ — never 5173.
npm run dev
