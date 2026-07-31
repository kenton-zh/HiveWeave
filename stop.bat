@echo off
REM ============================================================
REM  HiveWeave Service Stopper
REM  Usage: stop.bat
REM  Kills any process LISTENING on 4000 (backend) and 5173 (frontend).
REM  Port-based kill is reliable across npm/cmd/node wrapping layers.
REM  Does NOT kill all node.exe (that would also kill project game servers
REM  started via start_dev_server).
REM ============================================================

echo [HiveWeave] Stopping services on ports 4000 (backend) and 5173 (frontend)...

set "FOUND=0"

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":4000 " ^| findstr "LISTENING"') do (
    echo   killing backend PID %%a
    taskkill /F /PID %%a >nul 2>&1
    set "FOUND=1"
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do (
    echo   killing frontend PID %%a
    taskkill /F /PID %%a >nul 2>&1
    set "FOUND=1"
)

if "%FOUND%"=="0" echo   nothing was listening on 4000 or 5173.
echo [HiveWeave] Done.
