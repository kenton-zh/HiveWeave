@echo off
REM ============================================================
REM  HiveWeave Full Stack Startup Script
REM  Starts both backend (port 4000) and frontend (port 5173)
REM
REM  Usage: start-all.bat
REM  Backend:  http://localhost:4000
REM  Frontend: http://localhost:5173
REM
REM  This script kills any process bound to 4000/5173 FIRST,
REM  then starts fresh instances. Ensures code changes take effect.
REM ============================================================

echo [HiveWeave] Starting full stack (clean restart)...
echo.

REM --- Kill stale backend (anything on port 4000) ---
echo [HiveWeave] Killing stale backend on port 4000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":4000 " ^| findstr "LISTENING"') do (
    echo   killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

REM --- Kill stale frontend (by PID file, not all node.exe) ---
REM NOTE: Killing all node.exe would also kill project game servers started via
REM start_dev_server. Use the PID file maintained by start-frontend.bat instead.
echo [HiveWeave] Killing stale frontend by PID file...
set "FRONTEND_PID_FILE=%~dp0apps\web\frontend.pid"
if exist "%FRONTEND_PID_FILE%" (
    set /p OLD_FRONTEND_PID=<"%FRONTEND_PID_FILE%"
    echo   killing frontend PID %OLD_FRONTEND_PID%
    taskkill /F /PID %OLD_FRONTEND_PID% >nul 2>&1
    del "%FRONTEND_PID_FILE%" >nul 2>&1
)

REM Brief pause to let OS release ports
timeout /t 2 /nobreak >nul

REM --- Start backend in a new window ---
start "HiveWeave Backend" cmd /c "%~dp0start-backend.bat"

REM Wait for backend to be ready
echo [HiveWeave] Waiting for backend to start...
timeout /t 10 /nobreak >nul

REM --- Start frontend in a new window ---
start "HiveWeave Frontend" cmd /c "%~dp0start-frontend.bat"

echo.
echo [HiveWeave] Both services starting:
echo   Backend:  http://localhost:4000
echo   Frontend: http://localhost:5173
echo.
echo Close the backend/frontend windows to stop the services.
