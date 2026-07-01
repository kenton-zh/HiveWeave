@echo off
REM ============================================================
REM  HiveWeave Full Stack Startup Script
REM  Starts both backend (port 4000) and frontend (port 5173)
REM
REM  Usage: start-all.bat
REM  Backend: http://localhost:4000
REM  Frontend: http://localhost:5173
REM ============================================================

echo [HiveWeave] Starting full stack...
echo.

REM Start backend in a new window
start "HiveWeave Backend" cmd /c "%~dp0start-backend.bat"

REM Wait for backend to be ready
echo [HiveWeave] Waiting for backend to start...
timeout /t 10 /nobreak >nul

REM Start frontend in a new window
start "HiveWeave Frontend" cmd /c "%~dp0start-frontend.bat"

echo.
echo [HiveWeave] Both services starting:
echo   Backend:  http://localhost:4000
echo   Frontend: http://localhost:5173
echo.
echo Close the backend/frontend windows to stop the services.
