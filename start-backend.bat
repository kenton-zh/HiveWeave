@echo off
REM ============================================================
REM  HiveWeave Backend Startup Script
REM  Usage: start-backend.bat
REM
REM  This script sets up Erlang/Elixir PATH automatically.
REM  Erlang/Elixir are installed at non-standard locations:
REM    - Erlang/OTP 26: C:\Users\99744\otp26\bin
REM    - Elixir:        C:\Users\99744\elixir\bin
REM ============================================================

set ERLANG_HOME=C:\Users\99744\otp26
set ELIXIR_HOME=C:\Users\99744\elixir
set PATH=%ERLANG_HOME%\bin;%ELIXIR_HOME%\bin;%PATH%

rem Read OPENCODE_API_KEY from apps/server/.env
for /f "tokens=1,* delims==" %%a in ('type "%~dp0apps\server\.env" ^| findstr /b "OPENCODE_API_KEY"') do set OPENCODE_API_KEY=%%b

echo [HiveWeave] Starting backend...
echo [HiveWeave] Erlang: %ERLANG_HOME%\bin
echo [HiveWeave] Elixir: %ELIXIR_HOME%\bin
echo [HiveWeave] Working dir: %~dp0apps\hiveweave
echo.

cd /d "%~dp0apps\hiveweave"
mix phx.server
