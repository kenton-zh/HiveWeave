@echo off
REM ============================================================
REM  HiveWeave Full Stack Startup Script (silent mode)
REM  Starts both backend (port 4000) and frontend (port 5173) in HIDDEN
REM  windows - no popup, no console flicker. All output is tee'd to
REM  files under tasks\ so the caller can tail them.
REM
REM  Usage: start-all.bat
REM    Backend:  http://localhost:4000  log: tasks\backend-YYYYMMDD-HHMMSS.output
REM    Frontend: http://localhost:5173  log: tasks\frontend-YYYYMMDD-HHMMSS.log
REM
REM  Stop:   stop.bat   (port-based kill of 4000 and 5173)
REM
REM  The visible-mode .bat files (start-backend.bat / start-frontend.bat)
REM  are unchanged - run them directly from an IDE terminal to watch the
REM  live stream of uvicorn / Vite output.
REM ============================================================

REM --- Kill stale services (port-based) ---
echo [HiveWeave] Stopping any stale services on 4000 / 5173...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":4000 " ^| findstr "LISTENING"') do (
    echo   killing backend PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do (
    echo   killing frontend PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

REM --- Prepare log directory ---
if not exist "%~dp0tasks" mkdir "%~dp0tasks"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set TS=%%i
set "BACKEND_LOG=%~dp0tasks\backend-%TS%.output"
set "FRONTEND_LOG=%~dp0tasks\frontend-%TS%.log"

REM --- Build the hidden backend command line as ONE variable ---
REM Build then pass as a single quoted arg to wscript - keeps BAT's
REM &/&&/pipe parsing out of the picture. Path is fully expanded so
REM cmd /c inside the hidden window doesn't depend on cwd.
set "BE_CMD=cmd /c set PYTHONUNBUFFERED=1&& set HIVEWEAVE_LOG_JSON=1&& set HIVEWEAVE_LOG_LEVEL=INFO&& set HIVEWEAVE_LOG_FILE=%BACKEND_LOG%&& cd /d %~dp0apps\hiveweave-py&& .venv\Scripts\python.exe -u -m uvicorn hiveweave.main:app --host 127.0.0.1 --port 4000 --workers 1 --limit-concurrency 100 --backlog 2048 --timeout-keep-alive 30"

REM --- Launch backend silently ---
echo [HiveWeave] Starting backend silently  ^> %BACKEND_LOG%
wscript.exe //nologo "%~dp0silent-run.vbs" "%BE_CMD%"

REM --- Wait for backend to come up ---
REM Why no polling loop: PowerShell-launched cmd inherits a stdin pipe
REM from the host, and `netstat` (and other CONOUT$-style tools) refuses
REM that as "Input redirection is not supported". So we can't poll.
REM Pragmatic alternative: uvicorn takes 2-5s to bind 4000 from cold start;
REM sleep long enough that it's almost certainly up, then do ONE final
REM netstat check. If check fails, surface a warning with the log path so
REM the user can `tail` it.
echo [HiveWeave] Waiting 8s for backend to bind 4000...
ping -n 9 127.0.0.1 >nul
netstat -ano | findstr ":4000 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [HiveWeave] WARNING: backend did not bind 4000 within 8s - check %BACKEND_LOG%
) else (
    echo [HiveWeave] Backend is up.
)

REM --- Prepend Node 22 to PATH (system has Node 24 globally; HiveWeave needs 22) ---
set "NODE22=%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64"
if exist "%NODE22%\node.exe" set "PATH=%NODE22%;%PATH%"

REM --- Build the hidden frontend command line as ONE variable ---
set "FE_CMD=cmd /c cd /d %~dp0apps\web&& npm run dev 1> "%FRONTEND_LOG%" 2>&1"

echo [HiveWeave] Starting frontend silently ^> %FRONTEND_LOG%
wscript.exe //nologo "%~dp0silent-run.vbs" "%FE_CMD%"

REM --- Wait for frontend to come up ---
REM Same stdin-pipe constraint as backend wait - just sleep + one check.
echo [HiveWeave] Waiting 6s for frontend to bind 5173...
ping -n 7 127.0.0.1 >nul
netstat -ano | findstr ":5173 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [HiveWeave] WARNING: frontend did not bind 5173 within 6s - check %FRONTEND_LOG%
) else (
    echo [HiveWeave] Frontend is up.
)

echo.
echo [HiveWeave] Both services running silently:
echo   Backend:  http://localhost:4000  log: %BACKEND_LOG%
echo   Frontend: http://localhost:5173  log: %FRONTEND_LOG%
echo.
echo   stop.bat   - kill both services
echo   tail -f    - watch logs in another terminal
