@echo off
REM ============================================================
REM  HiveWeave Frontend Startup Script
REM  Usage: start-frontend.bat
REM  Kills by PORT (not all node.exe — that would kill project game servers).
REM  vite.config.ts has strictPort:true, so if 5173 can't be freed this
REM  script fails fast instead of silently creeping to 5174/5175/...
REM ============================================================

set "NODE22=%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64"
if exist "%NODE22%\node.exe" set "PATH=%NODE22%;%PATH%"

echo [HiveWeave] Starting frontend...
echo [HiveWeave] Working dir: %~dp0apps\web
node --version
echo.

REM Kill any stale process LISTENING on port 5173 (clean restart).
REM Port-based kill is reliable; the old frontend.pid approach never wrote
REM the file and left orphan Vite instances piling up on 5174/5175/...
echo [HiveWeave] Killing stale frontend on port 5173...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do (
    echo   killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
REM Brief pause so OS releases the socket before Vite rebinds.
timeout /t 1 /nobreak >nul

cd /d "%~dp0apps\web"
echo [HiveWeave] Starting Vite on port 5173 (strictPort — will fail if port busy)...
echo [HiveWeave] Project apps must use start_dev_server / port 3000+ — never 5173.
npm run dev
