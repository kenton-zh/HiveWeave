@echo off
REM ============================================================
REM  HiveWeave Windows EXE build (PyInstaller onedir + assets)
REM  Usage: build-exe.bat
REM  Output: apps\desktop\dist\HiveWeave\  (HiveWeave.exe + _internal
REM          + ball/ + web/ + bin/agent-browser —— frozen 口径 exe 同级)
REM  重建前必须 taskkill HiveWeave.exe（文件锁会炸 PyInstaller 清目录）。
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set NODE22=%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64
set PATH=%NODE22%;%PATH%

set OUT=apps\desktop\dist\HiveWeave
set PRESERVE=apps\desktop\dist\.hw-preserve

echo [1/5] Building frontend (pnpm --filter @hiveweave/web build)...
call pnpm --filter @hiveweave/web build
if errorlevel 1 (
  echo FRONTEND BUILD FAILED
  exit /b 1
)

echo [2/5] Syncing backend deps (uv sync --extra dev --extra desktop)...
cd apps\hiveweave-py
uv sync --extra dev --extra desktop
if errorlevel 1 (
  echo UV SYNC FAILED
  exit /b 1
)
cd ..\..

REM ── 用户数据保留（模型配置/项目登记全在 EXE 同级 data/ 与 .env）────
REM PyInstaller COLLECT 会整删 dist\HiveWeave 再重建——先把 data/ 与 .env
REM 挪到 dist\.hw-preserve，构建完成后回填。上次构建若中途失败留下
REM stranded preserve，这里先回填再继续（preserve 视作最近已知数据）。
echo [3/5] Preserving user data (data/ + .env)...
if exist "%PRESERVE%\data" (
  if exist "%OUT%\data" (
    rmdir /s /q "%PRESERVE%\data"
  ) else (
    move /y "%PRESERVE%\data" "%OUT%\data" >nul
  )
)
if exist "%PRESERVE%\.env" (
  if not exist "%OUT%\.env" copy /y "%PRESERVE%\.env" "%OUT%\.env" >nul
  del /q "%PRESERVE%\.env" >nul 2>&1
)
if exist "%OUT%\data" (
  mkdir "%PRESERVE%" 2>nul
  move /y "%OUT%\data" "%PRESERVE%\data" >nul
  if errorlevel 1 (
    echo DATA MOVE FAILED - HiveWeave.exe 仍在运行？先 taskkill /f /im HiveWeave.exe
    exit /b 1
  )
)
if exist "%OUT%\.env" (
  mkdir "%PRESERVE%" 2>nul
  copy /y "%OUT%\.env" "%PRESERVE%\.env" >nul
  if errorlevel 1 (
    echo ENV COPY TO PRESERVE FAILED - 中止构建以保护原始 .env
    exit /b 1
  )
)

echo [4/5] Running PyInstaller...
cd apps\hiveweave-py
uv run pyinstaller --noconfirm --clean ..\desktop\hiveweave.spec --distpath ..\desktop\dist --workpath ..\desktop\build
if errorlevel 1 (
  echo PYINSTALLER FAILED
  exit /b 1
)
cd ..\..

if not exist "%OUT%\HiveWeave.exe" (
  echo EXE MISSING: %OUT%\HiveWeave.exe
  exit /b 1
)

echo [5/5] Copying exe-sibling assets (ball/ web/ bin/) + restoring user data...
if not exist "apps\web\dist\index.html" (
  echo WEB DIST INCOMPLETE: apps\web\dist\index.html missing
  exit /b 1
)
robocopy apps\desktop\ball "%OUT%\ball" /MIR /NFL /NDL /NJH /NJS >> "%OUT%\build-assets.log" 2>&1
if errorlevel 8 exit /b 1
robocopy apps\web\dist "%OUT%\web" /MIR /NFL /NDL /NJH /NJS >> "%OUT%\build-assets.log" 2>&1
if errorlevel 8 exit /b 1
if not exist "%OUT%\bin" mkdir "%OUT%\bin"
copy /y apps\web\node_modules\agent-browser\bin\agent-browser-win32-x64.exe "%OUT%\bin\" >> "%OUT%\build-assets.log" 2>&1
if errorlevel 1 (
  echo AGENT-BROWSER COPY FAILED
  exit /b 1
)

REM 回填用户数据（data/ 整目录 move 回 + .env 覆盖回）。任一恢复失败
REM 立即中止且**保留 preserve 现场**——PyInstaller 已清掉原位，preserve
REM 是唯一副本，带锁重试不能删（审计 P1-1：无条件 rmdir 会销毁用户数据）。
if exist "%PRESERVE%\data" (
  move /y "%PRESERVE%\data" "%OUT%\data" >nul
  if errorlevel 1 (
    echo DATA RESTORE FAILED - 用户数据保留在 %PRESERVE%\data，请关占用后重试
    exit /b 1
  )
)
if exist "%PRESERVE%\.env" (
  copy /y "%PRESERVE%\.env" "%OUT%\.env" >nul
  if errorlevel 1 (
    echo ENV RESTORE FAILED - 用户 .env 保留在 %PRESERVE%\.env，请手动回填
    exit /b 1
  )
)
if exist "%PRESERVE%" rmdir /s /q "%PRESERVE%"

echo.
echo ============================================================
echo  DONE: %OUT%\HiveWeave.exe
echo  User data preserved: %OUT%\data + .env
echo  Smoke: "%OUT%\HiveWeave.exe" --selfcheck
echo  Run  : "%OUT%\HiveWeave.exe"
echo ============================================================
endlocal
